"""Bounded LangGraph workflow for one SupportIQ worker attempt.

The database queue remains the durable workflow boundary. This graph owns
only the in-process decision flow: generate, validate, then resolve or hand
off to a human. It has no database or email side effects.
"""

from dataclasses import dataclass
import logging
from typing import Callable, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.services.ai_service import (
    AIServiceError,
    CustomerAnswer,
    answer_customer_question_once,
)


logger = logging.getLogger(__name__)
MAX_QUESTION_CHARS = 10_000
MAX_CONTEXT_CHARS = 50_000


GraphOutcome = Literal["resolved", "needs_human"]


class SupportGraphState(TypedDict, total=False):
    question: str
    policy_context: str | None
    situation: str | None
    intent: str
    answer_text: str
    outcome: GraphOutcome
    validation_error: str


@dataclass(frozen=True)
class SupportGraphResult:
    """Small result contract consumed by the worker state machine."""

    outcome: GraphOutcome
    intent: str
    answer_text: str | None = None
    validation_error: str | None = None


class GraphExecutionError(RuntimeError):
    """Unexpected graph failure, deliberately free of user/provider details."""


AnswerFn = Callable[..., CustomerAnswer]


def _generate(state: SupportGraphState, *, answer_fn: AnswerFn) -> SupportGraphState:
    result = answer_fn(
        state["question"],
        policy_context=state.get("policy_context"),
        situation=state.get("situation"),
    )
    return {"intent": result.intent, "answer_text": result.answer_text}


def _validate(state: SupportGraphState) -> SupportGraphState:
    """Validate model output without a second provider call."""
    intent = state.get("intent", "unknown")
    answer_text = state.get("answer_text", "").strip()
    if intent == "unknown":
        return {
            "outcome": "needs_human",
            "validation_error": "model returned an unknown intent",
        }
    if not answer_text:
        return {
            "outcome": "needs_human",
            "validation_error": "model returned an empty answer",
        }
    if len(answer_text) > 5000:
        return {
            "outcome": "needs_human",
            "validation_error": "model answer exceeded the 5000 character limit",
        }
    return {"outcome": "resolved"}


def _route_after_validation(state: SupportGraphState) -> str:
    return state.get("outcome", "needs_human")


def _input_error(
    question: object,
    policy_context: object,
    situation: object,
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
    if situation is not None and (
        not isinstance(situation, str) or len(situation) > MAX_CONTEXT_CHARS
    ):
        return "customer situation is invalid or too large"
    return None


def _build_graph(answer_fn: AnswerFn):
    graph = StateGraph(SupportGraphState)
    graph.add_node("generate", lambda state: _generate(state, answer_fn=answer_fn))
    graph.add_node("validate", _validate)
    graph.add_edge(START, "generate")
    graph.add_edge("generate", "validate")
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
    situation: str | None = None,
    answer_fn: AnswerFn = answer_customer_question_once,
) -> SupportGraphResult:
    """Run one graph execution with exactly one provider attempt."""
    input_error = _input_error(question, policy_context, situation)
    if input_error is not None:
        return SupportGraphResult(
            outcome="needs_human",
            intent="unknown",
            validation_error=input_error,
        )

    try:
        state = _build_graph(answer_fn).invoke({
            "question": question,
            "policy_context": policy_context,
            "situation": situation,
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
        intent=state.get("intent", "unknown"),
        answer_text=state.get("answer_text"),
        validation_error=state.get("validation_error"),
    )


__all__ = ["GraphExecutionError", "SupportGraphResult", "run_support_graph"]
