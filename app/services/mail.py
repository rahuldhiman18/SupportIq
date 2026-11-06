"""Outbox email delivery: idempotent, retryable, stdlib-only.

One row in email_outbox = one intended customer delivery. The idempotency key
prevents duplicate queue entries, while durable delivery leases prevent
concurrent workers from sending the same row at the same time. SMTP remains
at-least-once across a crash after SMTP accepts a message, so a stable
Message-ID is also sent to downstream mail infrastructure.

SMTP config comes from env vars with Mailpit-friendly defaults
(SMTP_HOST=localhost, SMTP_PORT=1025) so local development needs no
real mail server.  Set real credentials in .env for production.
"""
import os
import hashlib
import socket
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.tables import EmailOutbox


# --------------------------------------------------------------------------- #
# SMTP configuration (env-over-default, no new dependencies)
# --------------------------------------------------------------------------- #

SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", "1025"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true" if SMTP_USER else "false").lower() == "true"
SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
_APP_ENV = os.getenv("APP_ENV", "development").lower()
SMTP_REQUIRE_TLS = os.getenv(
    "SMTP_REQUIRE_TLS",
    "true" if _APP_ENV in {"prod", "production"} else "false",
).lower() == "true"
MAIL_FROM = os.getenv("MAIL_FROM", "support@supportiq.local")
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "SupportIq")
DELIVERY_LEASE_SECONDS = int(os.getenv("EMAIL_DELIVERY_LEASE_SECONDS", "120"))
MAX_DELIVERY_ATTEMPTS = 5
WORKER_ID = os.getenv("WORKER_ID") or f"mail-{socket.gethostname()}"


def _send_smtp(
    to: str,
    subject: str,
    body_text: str,
    *,
    idempotency_key: str | None = None,
) -> None:
    """Deliver one email via SMTP.  Raises on failure (caller records it)."""
    if SMTP_REQUIRE_TLS and not (SMTP_USE_TLS or SMTP_USE_SSL):
        raise RuntimeError("TLS is required for SMTP in production")
    if SMTP_USE_TLS and SMTP_USE_SSL:
        raise RuntimeError("Configure either SMTP_USE_TLS or SMTP_USE_SSL, not both")

    tls_context = ssl.create_default_context()
    msg = EmailMessage()
    msg["From"] = formataddr((MAIL_FROM_NAME, MAIL_FROM))
    msg["To"] = to
    msg["Subject"] = subject
    if idempotency_key:
        # Stable Message-ID lets downstream mail infrastructure collapse a
        # retry after a worker crash, where SMTP delivery succeeded but the
        # database acknowledgement did not.
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        msg["Message-ID"] = f"<supportiq-{digest}@supportiq.local>"
    msg.set_content(body_text)

    if SMTP_USE_SSL:
        smtp_connection = smtplib.SMTP_SSL(
            SMTP_HOST,
            SMTP_PORT,
            timeout=10,
            context=tls_context,
        )
    else:
        smtp_connection = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)

    with smtp_connection as smtp:
        if SMTP_USE_TLS and not SMTP_USE_SSL:
            smtp.starttls(context=tls_context)
        if SMTP_USER:
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
    `queued`; call process_outbox() to attempt SMTP delivery and retry failed
    rows.
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
    try:
        db.commit()
    except IntegrityError:
        # The unique constraint is the authoritative concurrency guard. Two
        # workers can both miss the pre-check, but only one insert succeeds.
        db.rollback()
        return None
    return row


def process_outbox(db: Session, limit: int = 50) -> int:
    """Deliver queued outbox rows and retry failed ones (bounded retries).

    Returns the number of rows attempted.  `queued` rows get their first
    delivery attempt; `failed` rows are retried with a 24h backoff and are
    dropped after 5 attempts.  Idempotent and safe to run from the worker
    loop or a cron job. Rows are claimed with a short database transaction,
    and stale claims are recoverable after the delivery lease expires.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    lease_cutoff = now - timedelta(seconds=DELIVERY_LEASE_SECONDS)
    eligible = or_(
        EmailOutbox.status == "queued",
        and_(
            EmailOutbox.status == "failed",
            or_(EmailOutbox.created_at < cutoff, EmailOutbox.attempts == 0),
        ),
        and_(EmailOutbox.status == "sending", EmailOutbox.locked_at < lease_cutoff),
    )

    # Claim rows in a short transaction. The SMTP call happens after commit,
    # so one slow mail server cannot hold database locks for the whole batch.
    rows = db.execute(
        select(EmailOutbox)
        .where(eligible)
        .where(EmailOutbox.attempts < MAX_DELIVERY_ATTEMPTS)
        .order_by(EmailOutbox.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).scalars().all()
    deliveries = [
        (row.id, row.to, row.subject, row.body_text, row.idempotency_key)
        for row in rows
    ]
    for row in rows:
        row.status = "sending"
        row.locked_at = now
        row.locked_by = WORKER_ID
    db.commit()

    delivered = 0
    for row_id, to, subject, body_text, idempotency_key in deliveries:
        try:
            _send_smtp(
                to,
                subject,
                body_text,
                idempotency_key=idempotency_key,
            )
            result = db.execute(
                update(EmailOutbox)
                .where(
                    EmailOutbox.id == row_id,
                    EmailOutbox.status == "sending",
                    EmailOutbox.locked_by == WORKER_ID,
                )
                .values(
                    status="sent",
                    sent_at=datetime.now(timezone.utc),
                    locked_at=None,
                    locked_by=None,
                    error=None,
                )
            )
            delivered += int(result.rowcount == 1)
        except Exception as exc:  # noqa: BLE001 -- record and keep going
            db.execute(
                update(EmailOutbox)
                .where(
                    EmailOutbox.id == row_id,
                    EmailOutbox.status == "sending",
                    EmailOutbox.locked_by == WORKER_ID,
                )
                .values(
                    status="failed",
                    attempts=EmailOutbox.attempts + 1,
                    error=f"{type(exc).__name__}: {exc}"[:1000],
                    locked_at=None,
                    locked_by=None,
                )
            )
        db.commit()
    return delivered


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
