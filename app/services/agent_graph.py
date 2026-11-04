"""Bounded LangGraph workflow for one SupportIQ worker attempt.

The database queue remains the durable workflow boundary. This graph owns
only the in-process decision flow: generate, validate, then resolve or hand
off to a human. It has no database or email side effects.
"""

from dataclasses import dataclass
import logging
import re
from typing import Callable, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.services.ai_service import (
    AIServiceError,
    CustomerAnswer,
    INTENT_LABELS,
    answer_customer_question_once,
)


logger = logging.getLogger(__name__)
MAX_QUESTION_CHARS = 10_000
MAX_CONTEXT_CHARS = 50_000
MAX_ANSWER_CHARS = 5_000
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_UNSAFE_OUTPUT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"<\s*(?:tool_call|function_call)\b",
        r"\b(?:tool|function)[_-]?(?:name|call)\s*[:=]",
        r"[\"'](?:action|tool|function|operation)[\"']\s*:",
        r"\b(?:execute|invoke)\s*\(",
        r"\b(?:db|database|query)\.(?:commit|execute|update|delete)\b",
        r"\b(?:set_status|send_email|update_order|cancel_order)\s*\(",
        r"\b(?:UPDATE|DELETE|INSERT)\s+(?:orders?|support_queries|email_outbox)\b",
    )
)
_GROUNDING_STOPWORDS = frozenset(
    "a an and are as at be by for from has have i in is it of on or our that "
    "the their this to was we were will with your you".split()
)

_PROMPT_INJECTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"\bignore\s+(?:all\s+|any\s+|previous\s+|above\s+)?(?:instructions|rules)\b",
        r"\b(?:disclose|reveal|print)\s+(?:the\s+)?(?:system|hidden|developer)\s+(?:prompt|instructions)\b",
        r"\byou\s+are\s+(?:now|actually)\s+(?:an?\s+)?(?:unrestricted|different)",
        r"<\s*(?:system|developer|admin)\b",
        r"\b(?:override|bypass)\s+(?:safety|security|authorization)\b",
    )
)


GraphOutcome = Literal["resolved", "needs_human"]
IdentityScope = Literal[
    "authenticated_customer",
    "server_scoped_customer",
    "unverified_customer",
]
PlanAction = Literal[
    "answer_with_grounding",
    "answer_with_order_facts",
    "human_review",
]


class SupportGraphState(TypedDict, total=False):
    customer_question: str
    approved_policy_context: str | None
    verified_order_facts: str | None
    customer_identity_scope: IdentityScope
    detected_intent: str
    draft_answer: str
    validation_result: Literal["valid", "invalid"]
    human_handoff_reason: str | None
    plan_action: PlanAction
    outcome: GraphOutcome


@dataclass(frozen=True)
class SupportGraphResult:
    """Small result contract consumed by the worker state machine."""

    outcome: GraphOutcome
    intent: str
    answer_text: str | None = None
    plan_action: PlanAction = "human_review"
    validation_error: str | None = None
    validation_result: Literal["valid", "invalid"] = "invalid"
    human_handoff_reason: str | None = None


class GraphExecutionError(RuntimeError):
    """Unexpected graph failure, deliberately free of user/provider details."""


AnswerFn = Callable[..., CustomerAnswer]


def _generate(state: SupportGraphState, *, answer_fn: AnswerFn) -> SupportGraphState:
    # Only server-approved context is passed to the provider adapter. The
    # identity scope is intentionally an enum, never a JWT or raw credential.
    result = answer_fn(
        state["customer_question"],
        policy_context=state.get("approved_policy_context"),
        situation=state.get("verified_order_facts"),
    )
    return {
        "detected_intent": result.intent,
        "draft_answer": result.answer_text,
    }


def _validate(state: SupportGraphState) -> SupportGraphState:
    """Validate model output without a second provider call."""
    intent = state.get("detected_intent", "unknown")
    raw_answer = state.get("draft_answer", "")
    answer_text = raw_answer.strip() if isinstance(raw_answer, str) else ""
    plan_action = state.get("plan_action", "human_review")
    if intent not in INTENT_LABELS:
        return {
            "outcome": "needs_human",
            "validation_result": "invalid",
            "human_handoff_reason": "model returned an unknown intent",
        }
    if plan_action == "human_review":
        return {
            "outcome": "needs_human",
            "validation_result": "invalid",
            "human_handoff_reason": "intent requires human review",
        }
    if not answer_text:
        return {
            "outcome": "needs_human",
            "validation_result": "invalid",
            "human_handoff_reason": "model returned an empty answer",
        }
    question = state.get("customer_question", "")
    for pattern in _PROMPT_INJECTION_PATTERNS:
        if pattern.search(question) or pattern.search(answer_text):
            return {
                "outcome": "needs_human",
                "validation_result": "invalid",
                "human_handoff_reason": "prompt injection or instruction override detected",
            }
    for source_text in (
        state.get("approved_policy_context"),
        state.get("verified_order_facts"),
    ):
        if source_text and any(
            pattern.search(source_text) for pattern in _PROMPT_INJECTION_PATTERNS
        ):
            return {
                "outcome": "needs_human",
                "validation_result": "invalid",
                "human_handoff_reason": "untrusted source contains instruction override content",
            }
    if len(answer_text) > MAX_ANSWER_CHARS:
        return {
            "outcome": "needs_human",
            "validation_result": "invalid",
            "human_handoff_reason": "model answer exceeded the 5000 character limit",
        }
    for pattern in _UNSAFE_OUTPUT_PATTERNS:
        if pattern.search(answer_text):
            return {
                "outcome": "needs_human",
                "validation_result": "invalid",
                "human_handoff_reason": "model output contained an unsafe action or tool call",
            }
    grounding_error = _grounding_error(
        answer_text,
        policy_context=state.get("approved_policy_context"),
        verified_order_facts=state.get("verified_order_facts"),
    )
    if grounding_error is not None:
        return {
            "outcome": "needs_human",
            "validation_result": "invalid",
            "human_handoff_reason": grounding_error,
        }
    return {"outcome": "resolved", "validation_result": "valid"}


def _grounding_tokens(value: str) -> set[str]:
    return {
        token
        for token in _TOKEN_RE.findall(value.lower())
        if token not in _GROUNDING_STOPWORDS and len(token) > 1
    }


def _grounding_error(
    answer_text: str,
    *,
    policy_context: str | None,
    verified_order_facts: str | None,
) -> str | None:
    """Require meaningful overlap with server-approved source text.

    This is intentionally conservative: lexical overlap is not a proof of
    factual correctness, but absence of overlap is strong evidence that the
    answer is unsupported. Such answers go to a human instead of being sent.
    """
    sources = [source for source in (policy_context, verified_order_facts) if source]
    if not sources:
        return "no approved policy or order context available for grounding"
    source_tokens = _grounding_tokens(" ".join(sources))
    answer_tokens = _grounding_tokens(answer_text)
    if not answer_tokens.intersection(source_tokens):
        return "model answer was not grounded in approved policy or order facts"
    return None


def _plan(state: SupportGraphState) -> SupportGraphState:
    """Map intent to a constrained plan without executing any side effect."""
    intent = state.get("detected_intent", "unknown")
    if intent in {"order_tracking", "delivery_eta"}:
        return {"plan_action": "answer_with_order_facts"}
    if intent == "unknown":
        return {"plan_action": "human_review"}
    return {"plan_action": "answer_with_grounding"}


def _route_after_validation(state: SupportGraphState) -> str:
    return state.get("outcome", "needs_human")


def _input_error(
    question: object,
    policy_context: object,
    verified_order_facts: object,
    customer_identity_scope: object,
) -> str | None:
    """Return a safe validation message without echoing customer content."""
    if not isinstance(question, str) or not question.strip():
        return "customer question is missing"
    if len(question) > MAX_QUESTION_CHARS:
        return "customer question exceeds the maximum length"
    if policy_context is not None and (
        not isinstance(policy_context, str) or len(policy_context) > MAX_CONTEXT_CHARS
    ):
        return "policy context is invalid or too large"
    if verified_order_facts is not None and (
        not isinstance(verified_order_facts, str)
        or len(verified_order_facts) > MAX_CONTEXT_CHARS
    ):
        return "verified order facts are invalid or too large"
    if customer_identity_scope not in {
        "authenticated_customer",
        "server_scoped_customer",
        "unverified_customer",
    }:
        return "customer identity scope is invalid"
    return None


def _build_graph(answer_fn: AnswerFn):
    graph = StateGraph(SupportGraphState)
    graph.add_node("generate", lambda state: _generate(state, answer_fn=answer_fn))
    graph.add_node("plan", _plan)
    graph.add_node("validate", _validate)
    graph.add_edge(START, "generate")
    graph.add_edge("generate", "plan")
    graph.add_edge("plan", "validate")
    graph.add_conditional_edges(
        "validate",
        _route_after_validation,
        {"resolved": END, "needs_human": END},
    )
    return graph.compile()


def run_support_graph(
    question: str,
    *,
    policy_context: str | None = None,
    verified_order_facts: str | None = None,
    customer_identity_scope: IdentityScope = "server_scoped_customer",
    # Compatibility alias for callers from the first graph slice. It is
    # normalized into verified_order_facts before entering graph state.
    situation: str | None = None,
    answer_fn: AnswerFn = answer_customer_question_once,
) -> SupportGraphResult:
    """Run one graph execution with exactly one provider attempt."""
    if verified_order_facts is None:
        verified_order_facts = situation
    input_error = _input_error(
        question,
        policy_context,
        verified_order_facts,
        customer_identity_scope,
    )
    if input_error is not None:
        return SupportGraphResult(
            outcome="needs_human",
            intent="unknown",
            validation_error=input_error,
            human_handoff_reason=input_error,
        )

    try:
        state = _build_graph(answer_fn).invoke({
            "customer_question": question,
            "approved_policy_context": policy_context,
            "verified_order_facts": verified_order_facts,
            "customer_identity_scope": customer_identity_scope,
        })
    except AIServiceError:
        # Provider failures retain their typed identity for worker retry logic.
        raise
    except Exception as exc:
        # Do not leak prompts, provider payloads, or internal tracebacks into
        # customer-visible state. The worker records this typed failure and
        # applies its existing retry/reaper policy.
        error_type = type(exc).__name__
        logger.error("support graph execution failed (%s)", error_type)
        raise GraphExecutionError(
            f"support graph execution failed ({error_type})"
        ) from exc
    return SupportGraphResult(
        outcome=state.get("outcome", "needs_human"),
        intent=state.get("detected_intent", "unknown"),
        answer_text=state.get("draft_answer"),
        plan_action=state.get("plan_action", "human_review"),
        validation_error=state.get("human_handoff_reason"),
        validation_result=state.get("validation_result", "invalid"),
        human_handoff_reason=state.get("human_handoff_reason"),
    )


__all__ = ["GraphExecutionError", "SupportGraphResult", "run_support_graph"]
