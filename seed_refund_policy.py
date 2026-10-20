"""Seed ALL policy/FAQ articles into the knowledge base.

Safe to re-run any time a draft changes (versioning history is preserved):
    python seed_refund_policy.py

Each article becomes a KnowledgeAnswer with an approved + current version;
POST /queries combines them (labeled by topic) into the single AI prompt.
"""

from datetime import datetime, timezone

from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models.tables import KnowledgeAnswer, KnowledgeAnswerVersion

# Single combined draft used by every API call: structured like the big
# marketplaces (Amazon returns + Google Play digital refunds) but written in
# our own words, and kept compact so it stays cheap and fast in the prompt.
REFUND_POLICY = """\
REFUND POLICY (physical items and digital purchases)

1. Physical items - Request a refund within 30 days of delivery. Items must be
   unused and in original packaging; final-sale and personalized items are not
   eligible. Shipping fees are non-refundable unless the item arrived damaged
   or defective.
2. Damaged or defective items - Report within 7 days of delivery for a free
   replacement or a full refund including shipping.
3. Digital purchases - In-app purchases and digital content can be refunded if
   requested within 48 hours of purchase; subscriptions can be cancelled any
   time with a pro-rata refund of the unused portion where required. Donations
   and gift cards are non-refundable.
4. Unauthorized charges - Report a charge you did not make within 120 days of
   the transaction; confirmed unauthorized charges are refunded in full.
5. How to request - Email support@example.com or use the Help Center portal
   with your order number and reason. Track progress under Orders > Refunds
   (pending / approved / completed).
6. How you get your money back - Refunds go to the original payment method.
   Once approved, the money is returned within 3-4 business days for credit
   card, wallet, and bank transfer alike.
"""

SHIPPING_POLICY = """\
SHIPPING POLICY
- Dispatch: orders are packed and dispatched within 1 business day.
- Standard: 3-5 business days after dispatch, 4.99 (free over 50).
- Express: 1-2 business days after dispatch, 9.99.
- A tracking link is emailed as soon as the parcel ships.
"""

PASSWORD_RESET = """\
PASSWORD RESET
1. On the login page click "Forgot password".
2. Enter your registered email - the reset link arrives within minutes
   and expires 30 minutes later. Check spam/junk if it is not there.
3. Set a new password (8+ characters, letter + number).
4. Nothing after 10 minutes? Request once more, then contact support.
Our agents will NEVER ask for your password, reset link, or OTP.
"""

ACCOUNT_MANAGEMENT = """\
ACCOUNT MANAGEMENT (change details / delete account)
- Change email or details: Profile > Settings > Edit - applied instantly.
- Delete account: Settings > Delete account, then confirm the email we
  send. Access is disabled immediately and personal data is erased within
  30 days (invoices are kept only where the law requires).
- Locked out or no longer own the email? Contact support - we verify you
  via your last order number before any change.
"""

PAYMENT_ISSUES = """\
PAYMENT DEBITED BUT ORDER NOT COMPLETED
1. Do NOT pay again - the debit either settles (the order appears) or the
   bank reverses it automatically.
2. Auto-reversal usually lands within 5-7 business days on the same
   payment method (the bank controls this timeline).
3. Still missing after 7 business days? Send support the transaction
   reference (UTR), date, amount, and payment method - we escalate to the
   payment gateway the same day.
"""

WRONG_PURCHASE = """\
WRONG PURCHASE (bought the wrong item)
- Not dispatched: cancel instantly under Orders > Cancel; the refund
  follows the refund policy (3-4 business days).
- Already shipped: refuse the parcel, or request a return within 30 days
  under the refund policy.
- Wrong variant (size/color): request an exchange within 30 days - we
  ship the replacement once the original is scanned at our warehouse.
- Always include your order number so we can act in one reply.
"""

WRONG_ADDRESS = """\
WRONG DELIVERY ADDRESS - HOW TO FIX AND HOW LONG IT TAKES
- Not dispatched: edit it yourself under Orders > Edit address -
  effective immediately (cut-off is 1 hour after ordering).
- Dispatched but not out for delivery: contact support right away; we ask
  the carrier to reroute and confirm the fix within 1-2 business days.
- Already out for delivery: changes are no longer possible - refuse the
  parcel (we refund per the refund policy) or receive and return it.
- Every address-change outcome is confirmed by email within 1 business
  day.
"""

ORDER_TRACKING = """\
WHERE IS MY ORDER (TRACKING)
- Track any time under Orders > Track Order or the tracking link in your
  shipping email.
- Stages: Order placed > Packed > Shipped > Out for delivery > Delivered.
  Carrier scans can take a few hours to appear.
- No status change for 3+ business days? Contact support with your order
  number - we open a carrier investigation the same day.
"""

DELIVERY_ETA = """\
WHEN WILL MY ORDER ARRIVE
- Arrival = dispatch (within 1 business day) + Standard 3-5 business
  days, or Express 1-2 business days.
- Business days exclude weekends and public holidays.
- Your exact estimate lives on Orders > Track Order and in the shipping
  confirmation email - treat that date as the commitment.
- If we foresee a delay we email you as soon as it is known.
"""

# canonical_question -> (answer draft, source url) : the single source of
# truth POST /queries feeds to the AI (labeled by topic).
ARTICLES = {
    "What is the refund policy?": (
        REFUND_POLICY, "https://example.com/terms/refund-policy"),
    "What is the shipping policy?": (
        SHIPPING_POLICY, "https://example.com/terms/shipping"),
    "How do I reset a forgotten password?": (
        PASSWORD_RESET, "https://example.com/help/reset-password"),
    "How do I manage or delete my account?": (
        ACCOUNT_MANAGEMENT, "https://example.com/help/account"),
    "My payment was debited but the order failed. What do I do?": (
        PAYMENT_ISSUES, "https://example.com/help/payment-issues"),
    "I bought the wrong item. What now?": (
        WRONG_PURCHASE, "https://example.com/help/wrong-purchase"),
    "How do I fix a wrong delivery address?": (
        WRONG_ADDRESS, "https://example.com/help/change-address"),
    "Where is my order?": (
        ORDER_TRACKING, "https://example.com/help/track-order"),
    "When will my order be delivered?": (
        DELIVERY_ETA, "https://example.com/help/delivery-times"),
}


def _ensure_article(db, canonical_question: str, answer_text: str,
                    source_url: str) -> str:
    """Create the article, or publish a new approved version if it changed."""
    knowledge = db.execute(
        select(KnowledgeAnswer).where(
            KnowledgeAnswer.canonical_question == canonical_question
        )
    ).scalar_one_or_none()

    current = None
    if knowledge:
        current = db.execute(
            select(KnowledgeAnswerVersion).where(
                KnowledgeAnswerVersion.knowledge_answer_id == knowledge.id,
                KnowledgeAnswerVersion.is_current.is_(True),
            )
        ).scalars().first()
        if current and current.answer_text == answer_text:
            return f"up to date : {canonical_question}"
        knowledge.is_active = True
        knowledge.source_url = source_url
    else:
        knowledge = KnowledgeAnswer(
            canonical_question=canonical_question,
            source_url=source_url,
            is_active=True,
        )
        db.add(knowledge)
        db.flush()

    if current:
        current.is_current = False  # supersede the old source of truth
    max_version = (
        db.execute(
            select(func.max(KnowledgeAnswerVersion.version_number)).where(
                KnowledgeAnswerVersion.knowledge_answer_id == knowledge.id
            )
        ).scalar()
        or 0
    )
    version = KnowledgeAnswerVersion(
        knowledge_answer_id=knowledge.id,
        version_number=max_version + 1,
        answer_text=answer_text,
        status="approved",
        is_current=True,
        approved_by="seed-script",
        approved_at=datetime.now(timezone.utc),
    )
    db.add(version)
    db.commit()
    return f"seeded v{version.version_number}: {canonical_question}"


def seed() -> None:
    db = SessionLocal()
    try:
        for canonical_question, (answer_text, source_url) in ARTICLES.items():
            print(_ensure_article(db, canonical_question, answer_text, source_url))
    finally:
        db.close()


if __name__ == "__main__":
    seed()
