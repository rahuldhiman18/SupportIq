"""Customer-visible answer behavior enforced before and after AI generation.

This is intentionally deterministic: prompts guide the model, while this
module decides whether the model may be called and whether its text is safe to
store or show to a customer.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


AnswerOutcome = Literal["answered", "refused", "needs_human"]

_UNSAFE_REQUEST = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\bignore\s+(?:all\s+|previous\s+)?instructions\b",
    r"\b(?:reveal|show|print|disclose)\b.{0,40}\b(?:system|developer|hidden)\b",
    r"\b(?:bypass|override)\b.{0,40}\b(?:security|authorization|policy)\b",
    r"<\s*(?:system|developer|tool_call|function_call)\b",
))
_UNSAFE_ANSWER = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"<\s*(?:tool_call|function_call)\b",
    r"\b(?:UPDATE|DELETE|INSERT|DROP)\s+(?:TABLE|FROM|INTO)\b",
    r"\b(?:set_status|send_email|update_order|cancel_order)\s*\(",
    r"\b(?:api[_ -]?key|authorization:\s*bearer|jwt[_ -]?secret)\b",
))
MAX_CUSTOMER_ANSWER_CHARS = 5_000


@dataclass(frozen=True)
class AnswerBehavior:
    outcome: AnswerOutcome
    answer_text: str | None
    next_step: str
    requires_human: bool


_GENERAL_POLICY_RE = re.compile(
    r"\b(?:policy|window|eligible|eligibility|how long|refund rule|"
    r"cancel(?:lation)? rule|return(?:s)? accepted)\b",
    re.IGNORECASE,
)


def _is_general_policy_question(question: str) -> bool:
    return bool(_GENERAL_POLICY_RE.search(question))


def preflight_customer_question(
    question: str,
    *,
    situation: str | None = None,
    policy_context: str | None = None,
) -> AnswerBehavior | None:
    """Apply customer-facing clarification and security rules before the provider."""
    if any(pattern.search(question) for pattern in _UNSAFE_REQUEST):
        return AnswerBehavior(
            outcome="refused",
            answer_text=(
                "I can’t provide internal instructions or bypass security controls. "
                "I can help with a support question about an order, refund, delivery, or account."
            ),
            next_step="Ask a customer-support question without internal-system instructions.",
            requires_human=False,
        )

    if situation:
        if "ORDER DETAILS ARE UNAVAILABLE" in situation:
            return AnswerBehavior(
                outcome="answered",
                answer_text="Please sign in so I can verify the order and show its private details.",
                next_step="Sign in to the account used for the purchase, then ask again.",
                requires_human=False,
            )

        if "CUSTOMER HAS " in situation and " OPEN ORDERS:" in situation:
            identifiers = list(dict.fromkeys(
                match.upper() for match in re.findall(
                    r"\b[A-Z]{1,4}-?\d{4,8}\b", situation
                )
            ))
            if identifiers:
                choices = ", ".join(identifiers)
                return AnswerBehavior(
                    outcome="answered",
                    answer_text=f"I found multiple orders: {choices}. Which order do you mean?",
                    next_step="Provide one of the listed order numbers.",
                    requires_human=False,
                )

        if (
            "NO ORDERS FOUND" in situation
            and not _is_general_policy_question(question)
        ):
            return AnswerBehavior(
                outcome="answered",
                answer_text="Please provide the order number (for example, ORD-1234) so I can check it.",
                next_step="Provide the order number from the purchase confirmation.",
                requires_human=False,
            )

        if (
            "CUSTOMER IS NOT LOGGED IN" in situation
            and not _is_general_policy_question(question)
        ):
            return AnswerBehavior(
                outcome="answered",
                answer_text="Please provide the order number (for example, ORD-1234) and sign in so I can verify it.",
                next_step="Sign in and provide the relevant order number.",
                requires_human=False,
            )

    if (
        policy_context is not None
        and "no approved policy" in policy_context.lower()
        and not _is_general_policy_question(question)
    ):
        return AnswerBehavior(
            outcome="needs_human",
            answer_text=None,
            next_step="I cannot confirm a matching policy. A support specialist will review this request.",
            requires_human=True,
        )
    return None


def validate_generated_answer(answer_text: object) -> AnswerBehavior:
    """Never expose unsafe or unbounded model output to a customer."""
    text = answer_text.strip() if isinstance(answer_text, str) else ""
    if not text or len(text) > MAX_CUSTOMER_ANSWER_CHARS:
        return _handoff()
    if any(pattern.search(text) for pattern in _UNSAFE_ANSWER):
        return _handoff()
    return AnswerBehavior(
        outcome="answered",
        answer_text=text,
        next_step="Use the policy guidance above, or contact support if your situation differs.",
        requires_human=False,
    )


def _handoff() -> AnswerBehavior:
    return AnswerBehavior(
        outcome="needs_human",
        answer_text=None,
        next_step="A support specialist will review this request.",
        requires_human=True,
    )
