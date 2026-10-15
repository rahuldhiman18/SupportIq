"""Shared prompt-context builder (Step 3b).

Both serving paths build the EXACT same prompt inputs through this module:
- inline mode (QUERY_MODE=inline, Phase A): inside the HTTP request
- queued mode (QUERY_MODE=queued, Step 3b): inside the worker, from the
  request extras persisted on SupportQuery at enqueue time

Extracted verbatim from main.create_query so the two paths can never drift
apart (same policy join, same first-policy FK choice, same situation math).
"""
import re
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import KnowledgeAnswer, KnowledgeAnswerVersion, Order

# ** Order-number extractor from free text: "status for ORD-9002" / "#A12345"
_ORDER_NUMBER_RE = re.compile(r"\b#?([A-Za-z]{1,4}-?\d{4,8})\b")


def _extract_order_number(question: str) -> str | None:
    match = _ORDER_NUMBER_RE.search(question)
    return match.group(1).upper() if match else None


@dataclass
class PromptContext:
    """Everything needed for exactly one grounded AI call."""
    policy_context: str | None
    situation: str | None
    knowledge_version_id: UUID | None
    stalled: bool


def build_prompt_context(
    db: Session,
    *,
    question: str,
    customer_email: str,
    order_number: str | None = None,
    purchase_date: date | None = None,
    order_status: str | None = None,
) -> PromptContext:
    """Build policy + situation context exactly as inline mode always did."""
    # ** Find ALL current approved knowledge versions (source of truth), each
    #    labeled with its topic so the AI grounds in the RIGHT policy.
    #    (The old code used .first() which picked ONE arbitrary policy once
    #    several existed -- answers could come from the wrong document.)
    current_policies = db.execute(
        select(KnowledgeAnswerVersion, KnowledgeAnswer.canonical_question)
        .join(
            KnowledgeAnswer,
            KnowledgeAnswerVersion.knowledge_answer_id == KnowledgeAnswer.id,
        )
        .where(KnowledgeAnswerVersion.is_current.is_(True))
        .order_by(KnowledgeAnswer.canonical_question)
    ).all()

    # schema allows one FK per query: first topic (alphabetical) is primary
    knowledge_version_id = current_policies[0][0].id if current_policies else None

    # One combined, labeled policy block keeps the single AI call fast.
    policy_context = "\n\n".join(
        f"Topic - {canonical_question}:\n{version.answer_text}"
        for version, canonical_question in current_policies
    ) or None

    # !Generate an intent-aware Ai response, grounded in the knowledge base
    # ** Phase 1: LIVE order facts (location algo) - real data beats guessing.
    #    Scoped to the requester's email: other people's orders never load.
    situation_parts = []
    stalled = False
    order_ref = order_number or _extract_order_number(question)
    if order_ref:
        order = db.execute(
            select(Order).where(
                Order.order_number == order_ref.strip().upper(),
                Order.customer_email == customer_email,
            )
        ).scalars().first()
        if order:
            situation_parts.append(order.tracking_facts())
            stalled = order.is_stalled()

    if purchase_date:
        days_ago = (date.today() - purchase_date).days
        situation_parts.append(
            f"purchased {days_ago} days ago (on {purchase_date.isoformat()})"
        )
    if order_status:
        situation_parts.append(f"order status: {order_status}")
    situation = "; ".join(situation_parts) or None

    return PromptContext(
        policy_context=policy_context,
        situation=situation,
        knowledge_version_id=knowledge_version_id,
        stalled=stalled,
    )