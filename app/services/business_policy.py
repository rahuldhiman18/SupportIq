"""Deterministic customer-support policy decisions.

The model may explain these decisions, but it must not invent or override
them.  Payment movement is deliberately not implemented here; this module
only decides eligibility and the next safe step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.models.tables import Order


REFUND_WINDOW_DAYS = 30
REFUND_POLICY_VERSION = "refund-v1"
CANCELLATION_POLICY_VERSION = "cancellation-v1"


@dataclass(frozen=True)
class PolicyDecision:
    decision: str
    reason_code: str
    customer_message: str
    requires_human: bool = False
    requires_confirmation: bool = False
    policy_version: str = REFUND_POLICY_VERSION


def _now(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def evaluate_refund(
    order: Order,
    *,
    requested_at: datetime | None = None,
    reason: str | None = None,
) -> PolicyDecision:
    """Return a refund eligibility decision without charging or paying money."""
    now = _now(requested_at)
    placed_at = order.placed_at
    if placed_at.tzinfo is None:
        placed_at = placed_at.replace(tzinfo=timezone.utc)
    age = (now - placed_at).days

    if age < 0:
        return PolicyDecision(
            "human_review", "invalid_order_date",
            "I cannot verify this order timeline yet, so a support specialist must review it.",
            requires_human=True,
        )
    if age > REFUND_WINDOW_DAYS:
        return PolicyDecision(
            "not_eligible", "refund_window_expired",
            f"This order is outside the {REFUND_WINDOW_DAYS}-day refund window.",
        )
    if order.current_stage == "delivered":
        return PolicyDecision(
            "eligible_for_review", "within_refund_window",
            "This order is within the refund window. Submit the item and reason for review; payment will not be issued automatically.",
            requires_confirmation=True,
        )
    if order.current_stage in {"shipped", "out_for_delivery"}:
        return PolicyDecision(
            "human_review", "shipment_in_transit",
            "The order is already in transit. A support specialist must review the refund request before any decision.",
            requires_human=True,
        )
    return PolicyDecision(
        "eligible_for_review", "not_dispatched",
        "The order has not been dispatched and is within the refund window. Confirm the request so support can review it.",
        requires_confirmation=True,
    )


def evaluate_cancellation(
    order: Order,
    *,
    confirmed: bool = False,
) -> PolicyDecision:
    """Return a cancellation decision; no order state is changed."""
    stage = order.current_stage
    if stage == "delivered":
        return PolicyDecision(
            "not_allowed", "already_delivered",
            "This order has already been delivered and cannot be cancelled. You can request a refund review instead.",
            policy_version=CANCELLATION_POLICY_VERSION,
        )
    if stage in {"shipped", "out_for_delivery"}:
        return PolicyDecision(
            "human_review", "already_dispatched",
            "This order has been dispatched. A support specialist must check whether cancellation is still possible.",
            requires_human=True,
            policy_version=CANCELLATION_POLICY_VERSION,
        )
    if not confirmed:
        return PolicyDecision(
            "confirmation_required", "customer_confirmation_required",
            "Cancellation is available before dispatch. Please confirm that you want to cancel this order.",
            requires_confirmation=True,
            policy_version=CANCELLATION_POLICY_VERSION,
        )
    return PolicyDecision(
        "approved_for_cancellation", "not_dispatched",
        "This order is eligible for cancellation. The cancellation still must be applied by the order service.",
        policy_version=CANCELLATION_POLICY_VERSION,
    )


# Curated coverage matrix used by tests, documentation, and policy seeding.
# Answers are intentionally short; operational truth still comes from the
# order service and approved tenant knowledge.
POLICY_FAQ: tuple[tuple[str, str, str], ...] = (
    ("refund", "What is the refund window?", "Refund review is available within 30 days of order placement."),
    ("refund", "Can I get a refund after delivery?", "Delivered orders within 30 days may be submitted for refund review."),
    ("refund", "Can I refund a shipped order?", "Shipped orders require support review before a refund decision."),
    ("refund", "Can I refund an undelivered order?", "Undelivered orders may be eligible, but the order state must be verified."),
    ("refund", "Is a late refund automatic?", "No. A late request is not automatically approved."),
    ("refund", "Does refund approval send money immediately?", "No. Approval for review does not move money automatically."),
    ("refund", "Can support review my refund reason?", "Yes. Provide the order number and reason for support review."),
    ("refund", "What if my order date is wrong?", "An inconsistent order timeline requires human review."),
    ("refund", "Can I refund a delivered order?", "A delivered order within the window can be submitted for review."),
    ("refund", "Can I refund a cancelled order?", "A cancelled order requires support to verify payment status."),
    ("refund", "Can I get a partial refund?", "Partial refunds require support review and are not guaranteed."),
    ("refund", "Can I refund a duplicate order?", "Duplicate-order claims require support verification."),
    ("refund", "Can I refund a wrong item?", "Wrong-item claims can be submitted for review with the order number."),
    ("refund", "Can I refund damaged goods?", "Damaged-goods claims require evidence and support review."),
    ("refund", "Can I refund without an order number?", "An order number or verified account is required to inspect an order."),
    ("refund", "Can another person request my refund?", "Only the verified account owner or an authorized staff member may request it."),
    ("refund", "Can a refund be denied?", "Yes. Requests outside policy or without verifiable facts may be denied."),
    ("refund", "Can I appeal a refund decision?", "Ask support for a human review of the decision."),
    ("refund", "How long does a refund take?", "The money-back timeline depends on the payment provider after approval."),
    ("refund", "Where is my refund?", "Support must verify the refund and payment-provider status."),
    ("refund", "Can I change my refund request?", "Changes require support review before processing."),
    ("refund", "Can I cancel a refund request?", "Support must verify whether the refund has already been processed."),
    ("refund", "What if I was charged twice?", "Duplicate-charge claims require payment verification and human review."),
    ("refund", "What if payment failed but money was debited?", "Payment discrepancies require payment-provider verification."),
    ("refund", "Can a refund be sent to another account?", "Refunds can only return through the approved payment path."),
    ("cancel", "Can I cancel before dispatch?", "Orders not yet dispatched may be eligible for cancellation after confirmation."),
    ("cancel", "Can I cancel after shipping?", "Shipped orders require human review; cancellation is not guaranteed."),
    ("cancel", "Can I cancel after delivery?", "Delivered orders cannot be cancelled."),
    ("cancel", "Does asking to cancel cancel the order?", "No. A request is not complete until the order service confirms it."),
    ("cancel", "Do I need confirmation to cancel?", "Yes. Cancellation requires explicit customer confirmation."),
    ("cancel", "Can support cancel my order?", "Authorized support can review and apply an eligible cancellation."),
    ("cancel", "Can another customer cancel my order?", "No. Order ownership must be verified."),
    ("cancel", "Can I cancel one item only?", "Partial cancellation requires support review."),
    ("cancel", "Can I cancel an express order?", "Express orders follow the same dispatch-state rule."),
    ("cancel", "What happens after cancellation?", "The order service confirms cancellation and payment handling separately."),
    ("cancel", "Can I cancel because the address is wrong?", "Request review immediately; dispatched orders may require human help."),
    ("cancel", "Can I cancel a duplicate order?", "Verify both orders before cancelling either one."),
    ("tracking", "Where is my order?", "Tracking uses the latest verified order event."),
    ("tracking", "What is my order status?", "The current status comes from the latest verified order event."),
    ("tracking", "When will my order arrive?", "The system provides an ETA window from verified shipping facts."),
    ("tracking", "Why has tracking not changed?", "A stale shipment may require a carrier investigation."),
    ("tracking", "What does shipped mean?", "Shipped means the latest verified event is shipped."),
    ("tracking", "What does out for delivery mean?", "Out for delivery means the carrier reports the parcel in the delivery stage."),
    ("tracking", "What if tracking says delivered?", "A delivered status comes from the latest verified delivery event."),
    ("tracking", "Can you track another person's order?", "No. Order ownership must be verified."),
    ("tracking", "Can I track without signing in?", "A verified order number may still require sign-in before private facts are shown."),
    ("tracking", "Can I change my delivery address?", "Address changes require verification and may need human review."),
    ("tracking", "Can I change the carrier?", "Carrier data comes from the order service and is not changed by the agent."),
    ("tracking", "Why is the ETA a range?", "Delivery estimates are windows, not guaranteed promises."),
    ("tracking", "Does a weekend change the ETA?", "The ETA calculation uses business days."),
    ("tracking", "What if my order has no scans?", "No-scan orders are reported as unavailable or may be escalated for review."),
    ("tracking", "Can I get the exact delivery time?", "The system provides an ETA window, not an exact time."),
    ("tracking", "Can you contact the carrier?", "A carrier investigation may be opened by authorized support."),
    ("tracking", "Can I see the order destination?", "Only the verified account may view destination details."),
    ("tracking", "Can I share my tracking details?", "Do not share private order details with unverified people."),
    ("tracking", "What if my order is missing?", "Report the missing order with its number for human investigation."),
    ("tracking", "What if the order is delayed?", "A delayed shipment may be escalated when verified events are stale."),
    ("refund", "Can I request a refund by chat?", "Chat can collect the request, but eligibility and payment handling remain policy-controlled."),
    ("cancel", "Can I cancel before payment capture?", "A pre-dispatch cancellation still requires order-service confirmation."),
    ("tracking", "Can tracking facts be changed by the assistant?", "No. Tracking facts come only from verified order events."),
)


def faq_context(question: str, *, limit: int = 6) -> str | None:
    """Return a small topic-matched policy block for installations without KB rows."""
    terms = set((question or "").lower().split())
    scored = []
    for topic, faq_question, answer in POLICY_FAQ:
        score = len(terms.intersection(set(faq_question.lower().split())))
        if topic in terms or (topic == "refund" and terms.intersection({"refund", "money", "payment"})):
            score += 2
        if score:
            scored.append((score, topic, faq_question, answer))
    scored.sort(reverse=True)
    if not scored:
        return None
    return "\n\n".join(
        f"Topic: {topic}\nQuestion: {faq_question}\nPolicy: {answer}"
        for _, topic, faq_question, answer in scored[:limit]
    )
