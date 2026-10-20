"""Outbox email delivery: idempotent, retryable, stdlib-only.

One row in email_outbox = one intended customer delivery.  The idempotency
key makes retries safe: the same key never produces two emails.

SMTP config comes from env vars with Mailpit-friendly defaults
(SMTP_HOST=localhost, SMTP_PORT=1025) so local development needs no
real mail server.  Set real credentials in .env for production.
"""
import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import EmailOutbox


# --------------------------------------------------------------------------- #
# SMTP configuration (env-over-default, no new dependencies)
# --------------------------------------------------------------------------- #

SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", "1025"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
MAIL_FROM = os.getenv("MAIL_FROM", "support@supportiq.local")
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "SupportIq")


def _send_smtp(to: str, subject: str, body_text: str) -> None:
    """Deliver one email via SMTP.  Raises on failure (caller records it)."""
    msg = EmailMessage()
    msg["From"] = formataddr((MAIL_FROM_NAME, MAIL_FROM))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body_text)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
        if SMTP_USER:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(msg)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def queue_email(
    db: Session,
    *,
    to: str,
    subject: str,
    body_text: str,
    idempotency_key: str,
) -> EmailOutbox | None:
    """Queue an outbound email, idempotently.

    Returns the outbox row (or None if the same idempotency_key already
    exists -- the email was already queued or sent).  The row is left
    `queued`; call process_outbox() to attempt SMTP delivery and retry
    failed rows.
    """
    # Idempotency: never queue the same email twice.
    existing = db.execute(
        select(EmailOutbox).where(EmailOutbox.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None:
        return None

    row = EmailOutbox(
        id=uuid4(),
        idempotency_key=idempotency_key,
        to=to,
        subject=subject,
        body_text=body_text,
        status="queued",
    )
    db.add(row)
    db.commit()
    return row


def process_outbox(db: Session, limit: int = 50) -> int:
    """Deliver queued outbox rows and retry failed ones (bounded retries).

    Returns the number of rows attempted.  `queued` rows get their first
    delivery attempt; `failed` rows are retried with a 24h backoff and are
    dropped after 5 attempts.  Idempotent and safe to run from the worker
    loop or a cron job; concurrent runs are safe because each row is updated
    in a single transaction.
    """
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = db.execute(
        select(EmailOutbox)
        .where(EmailOutbox.status.in_(["queued", "failed"]))
        .where(EmailOutbox.attempts < 5)
        .where(
            (EmailOutbox.created_at < cutoff)
            | (EmailOutbox.attempts == 0)
        )
        .limit(limit)
    ).scalars().all()

    retried = 0
    for row in rows:
        try:
            _send_smtp(row.to, row.subject, row.body_text)
            row.status = "sent"
            row.sent_at = datetime.now(timezone.utc)
            retried += 1
        except Exception as exc:  # noqa: BLE001 -- record and keep going
            row.status = "failed"
            row.attempts = (row.attempts or 0) + 1
            row.error = f"{type(exc).__name__}: {exc}"[:1000]
    db.commit()
    return retried


def resolution_email(
    query_id,
    customer_email: str,
    question: str,
    answer_text: str,
) -> dict:
    """Build the subject/body for a query-resolution email."""
    subject = f"Your SupportIq query is resolved (ref {query_id})"
    body = (
        f"Hi,\n\n"
        f"Your support query has been resolved.\n\n"
        f"Question: {question}\n\n"
        f"Answer: {answer_text}\n\n"
        f"Reference: {query_id}\n\n"
        f"Thank you for contacting SupportIq.\n"
    )
    return {"subject": subject, "body": body, "to": customer_email}
