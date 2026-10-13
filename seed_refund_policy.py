"""Seed a refund-policy draft into the knowledge base.

Run once (afterwards this will be replaced by proper API/alembic flows):
    python seed_refund_policy.py

Creates one KnowledgeAnswer plus version 1 marked approved + current, so
POST /queries can use it as the source of truth for refund questions.
"""

from datetime import datetime, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.models import KnowledgeAnswer, KnowledgeAnswerVersion

REFUND_POLICY = """\
Refund Policy: Refunds are available for purchases made within the last \
30 days. To request a refund, contact our support team at \
support@example.com or through the Help Center portal, and include your \
order number and the reason for the request. Eligible refunds are issued \
to the original payment method: credit card refunds appear within 5-10 \
business days, and wallet/bank-transfer refunds may take up to 15 business \
days depending on your bank. Shipping fees are non-refundable, and items \
marked as final sale or personalized are not eligible for refunds. If your \
order arrived damaged or defective, contact us within 7 days of delivery \
and we will replace it or refund it in full, including shipping. Refund \
status can be tracked in your account under Orders > Refunds, where each \
request shows pending, approved, or completed."""


def seed() -> None:
    db = SessionLocal()
    try:
        existing = db.execute(
            select(KnowledgeAnswer).where(
                KnowledgeAnswer.canonical_question == "What is the refund policy?"
            )
        ).scalar_one_or_none()
        if existing:
            print("Refund policy already seeded, skipping.")
            return

        knowledge = KnowledgeAnswer(
            canonical_question="What is the refund policy?",
            source_url="https://example.com/terms/refund-policy",
            is_active=True,
        )
        db.add(knowledge)
        db.flush()  # get knowledge.id without committing yet

        now = datetime.now(timezone.utc)
        version = KnowledgeAnswerVersion(
            knowledge_answer_id=knowledge.id,
            version_number=1,
            answer_text=REFUND_POLICY,
            status="approved",  # CHECK: is_current=True requires approved
            is_current=True,    # CHECK: approved requires these two fields
            approved_by="seed-script",
            approved_at=now,
        )
        db.add(version)
        db.commit()
        print(f"Seeded refund policy: knowledge_answer_id={knowledge.id}, "
              f"version_id={version.id}")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
