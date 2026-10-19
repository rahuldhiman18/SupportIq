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

# Topic gate: order resolution only runs when the question looks order-related
# (tracking, cancel, refund status, payment...). "How do I reset my password"
# must never trigger an order lookup or an order-number nag.
_ORDER_TOPIC_RE = re.compile(
    r"\b(order|deliver(?:y|ed)?|track(?:ing)?|ship(?:ping|ped)?"
    r"|cancel(?:lation|led)?|refund(?:ed)?|address|payment)s?\b",
    re.I,
)


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
    # Agent-style resolution (approved): the order the server auto-matched for
    # a logged-in customer with exactly one open order. Persisted on the query
    # at enqueue time so queued workers rebuild the same facts without a JWT.
    resolved_order_number: str | None = None


def build_prompt_context(
    db: Session,
    *,
    question: str,
    customer_email: str,
    order_number: str | None = None,
    purchase_date: date | None = None,
    order_status: str | None = None,
    auth_email: str | None = None,
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
    resolved_order_number = None
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
        else:
            # Referenced but not found for this email: tell the AI so it can
            # ask them to double-check instead of inventing facts.
            situation_parts.append(
                f"ORDER {order_ref.strip().upper()} NOT FOUND for this email"
            )
    elif _ORDER_TOPIC_RE.search(question):
        # Agent-style resolution (approved, single-call design): the server
        # resolves what it can BEFORE the one Gemini call and INFORMS the AI,
        # which decides to answer, list, or ask -- never invent.
        if auth_email and auth_email == customer_email:
            customer_orders = db.execute(
                select(Order)
                .where(Order.customer_email == customer_email)
                .order_by(Order.placed_at.desc())
                .limit(5)
            ).scalars().all()
            open_orders = [o for o in customer_orders
                           if o.current_stage != "delivered"]
            if len(open_orders) == 1:
                # Exactly one open order -> auto-resolve (the magic case).
                situation_parts.append(open_orders[0].tracking_facts())
                stalled = open_orders[0].is_stalled()
                resolved_order_number = open_orders[0].order_number
            elif len(open_orders) > 1:
                listing = ", ".join(
                    f"{o.order_number} ({o.current_stage}, to {o.destination_city})"
                    for o in open_orders
                )
                situation_parts.append(
                    f"CUSTOMER HAS {len(open_orders)} OPEN ORDERS: {listing}. "
                    "Ask which order they mean; do not pick one silently."
                )
            elif customer_orders:
                latest = customer_orders[0]
                situation_parts.append(
                    f"NO OPEN ORDERS. Most recent: {latest.tracking_facts()}. "
                    "If they ask about a current order, confirm this is the "
                    "one or ask for their order number."
                )
            else:
                situation_parts.append(
                    "NO ORDERS FOUND for this customer's email. If they ask "
                    "about a specific order, ask for the order number and "
                    "remind them to check the email used at purchase."
                )
        else:
            # Anonymous: body email is spoofable -> no auto-lookup (approved
            # auth rule). The AI asks politely instead of being psychic.
            situation_parts.append(
                "CUSTOMER IS NOT LOGGED IN and gave no order number. If the "
                "answer needs a specific order (tracking, cancellation, refund "
                "status, payment issue), ask for their order number (e.g. "
                "ORD-1234) and offer to check. If it is a general policy "
                "question, answer directly without asking."
            )

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
        resolved_order_number=resolved_order_number,
    )