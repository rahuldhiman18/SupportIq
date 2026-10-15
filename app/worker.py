"""Queue transition logic for the AI answer workers (Option A, quota-safe).

The claim/drain loop still to come (Step 3b); the state machine below is what
that loop will drive, and it is fully unit-tested here.

Option A rules (approved):
- exactly ONE Gemini call per attempt (ai_service.answer_customer_question_once)
- max MAX_AI_ATTEMPTS attempts total, then hand off to a human
- exponential backoff between attempts so free-tier quota can refill
- AIContentBlockedError (safety filter) is never retried -- instant handoff

Statuses written here are plain strings: the PATCH /queries/{id} enum stays
narrow until that endpoint gets admin auth (it currently has none).
"""
import logging
from datetime import datetime, timedelta, timezone

from .ai_service import AIContentBlockedError
from .models import SupportQuery

logger = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_RESOLVED = "resolved"
STATUS_NEEDS_HUMAN = "needs_human"

MAX_AI_ATTEMPTS = 3
# minutes: attempt 1 -> retry after 2, attempt 2 -> 4, (3rd failure is terminal).
# The backoff is deliberately minutes-scale: hours would defeat FCFS freshness,
# and 2/4/8 keeps a failed query's total dead time under ~15 minutes while
# staying quota-safe on the free tier.
RETRY_BACKOFF_MINUTES = (2, 4, 8)


def record_attempt_failure(query: SupportQuery, exc: Exception) -> str:
    """Book one failed AI attempt on `query` (caller owns the transaction).

    Returns the status the row ends up in:
    - AIContentBlockedError: safety filter would block again -- hand to a human
      immediately instead of burning quota.
    - attempts < MAX_AI_ATTEMPTS: queue again with exponential backoff.
    - attempts exhausted: needs_human (the human support loop).
    """
    query.attempts = (query.attempts or 0) + 1
    query.last_error = f"{type(exc).__name__}: {exc}"[:1000]

    if isinstance(exc, AIContentBlockedError) or query.attempts >= MAX_AI_ATTEMPTS:
        query.status = STATUS_NEEDS_HUMAN
        query.next_retry_at = None
        query.locked_at = None
        logger.warning(
            "query %s handed to human support after %s attempt(s): %s",
            query.id,
            query.attempts,
            query.last_error,
        )
        return query.status

    minutes = RETRY_BACKOFF_MINUTES[min(query.attempts, len(RETRY_BACKOFF_MINUTES)) - 1]
    query.status = STATUS_QUEUED
    query.next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    query.locked_at = None
    return query.status


def record_success(query: SupportQuery, intent: str) -> str:
    """Book a successful AI attempt (caller owns the transaction).

    `attempts` is intentionally kept as the audit trail of failures it took;
    only the error/lock bookkeeping is cleared.
    """
    query.intent = intent
    query.status = STATUS_RESOLVED
    query.next_retry_at = None
    query.locked_at = None
    query.last_error = None
    return query.status
