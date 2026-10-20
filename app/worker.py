"""Queue transition logic for the AI answer workers (Option A, quota-safe).

Step 3b complete: the state machine below plus the claim/drain loop at the
bottom of this file drive the whole queue -- enqueue-only POST /queries hands
rows to `run_forever`/`run_once`, which claim by effective_score with
FOR UPDATE SKIP LOCKED (exactly-once proven live under 6 concurrent claimers).

Option A rules (approved):
- exactly ONE Gemini call per attempt (ai_service.answer_customer_question_once)
- max MAX_AI_ATTEMPTS attempts total, then hand off to a human
- exponential backoff between attempts so free-tier quota can refill
- AIContentBlockedError (safety filter) is never retried -- instant handoff

Statuses written here are plain strings: the PATCH /queries/{id} enum stays
narrow until that endpoint gets admin auth (it currently has none).
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from .ai_service import (
    AIContentBlockedError,
    AIProviderError,
    AIRateLimitError,
    AITimeoutError,
    answer_customer_question_once,
)
from .database import SessionLocal
from .mail import process_outbox, queue_email, resolution_email
from .models import AGING_SECONDS_PER_POINT, SupportQuery, Support_Answer
from .prompt_context import build_prompt_context

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

# Queue invariant (approved design): no row stays in `processing` beyond
# this. A legit attempt finishes in ~11s worst case (10s hard Gemini timeout
# + ms of DB work), so 60s = 5.5x headroom; tune from live data later.
STALE_PROCESSING_SECONDS = 60

# Reaper (P0): reclaims rows a crashed or hung worker left behind. Runs
# inside claim_next's transaction. A reclaimed row burns one attempt against
# the SAME 3-strike budget -- crashes count, so a poison row hands off to a
# human instead of crash-looping. Idempotent: status leaves `processing`, so
# concurrent workers cannot double-count. NULL locked_at = corrupt row ->
# reclaimed immediately. NEWEST-ANSWER-WINS covers the pathological case
# where the "dead" worker was actually alive and finishes later.
RECLAIM_STALE_SQL = """
    UPDATE support_queries
       SET attempts = attempts + 1,
           last_error = 'reclaimed: stuck in processing (worker crash/hang)',
           status = CASE WHEN attempts + 1 >= :max_attempts
                         THEN :needs_human ELSE :queued END,
           next_retry_at = NULL,
           locked_at = NULL
     WHERE status = :processing
       AND (locked_at IS NULL OR locked_at < :cutoff)
"""


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


def _queue_resolution_email(db: Session, query: SupportQuery, answer_text: str) -> None:
    """Queue the 'query resolved' email for a worker-resolved query.

    Idempotency key f\"resolved:{query.id}\" prevents duplicates on retry.
    """
    email = resolution_email(
        query_id=query.id,
        customer_email=query.customer_email,
        question=query.question,
        answer_text=answer_text,
    )
    queue_email(
        db,
        to=email["to"],
        subject=email["subject"],
        body_text=email["body"],
        idempotency_key=f"resolved:{query.id}",
    )


# ===================== Step 3b: claim + drain loop ===========================
# The claim statement is the one proven live (6 concurrent claimers took 10
# rows exactly-once in ~0.2s, every claim the best effective_score visible at
# that instant). It orders by priority - age/30 exactly like
# models.effective_score, skips backoff-pending rows, and never returns a row
# another worker holds (FOR UPDATE SKIP LOCKED).

CLAIM_NEXT_SQL = """
    UPDATE support_queries
       SET status = :processing, locked_at = :now
     WHERE id = (
            SELECT id
              FROM support_queries
             WHERE status = :queued
               AND (next_retry_at IS NULL OR next_retry_at <= :now)
             ORDER BY priority - EXTRACT(EPOCH FROM (:now - created_at)) / :aging
             LIMIT 1
               FOR UPDATE SKIP LOCKED
           )
 RETURNING id
"""


def claim_next(db: Session, now: datetime | None = None) -> SupportQuery | None:
    """Atomically claim the best queued row; None when the queue is empty.

    The row flips to `processing` + locked_at in the same statement, so a
    crashed worker's row stays visible for admin recovery instead of
    vanishing mid-flight.
    """
    at = now or datetime.now(timezone.utc)
    # 1) reaper first: stale `processing` rows re-enter the queue in the
    #    SAME transaction as the claim -- no window where they float free.
    db.execute(text(RECLAIM_STALE_SQL), {
        "processing": STATUS_PROCESSING,
        "queued": STATUS_QUEUED,
        "needs_human": STATUS_NEEDS_HUMAN,
        "max_attempts": MAX_AI_ATTEMPTS,
        "cutoff": at - timedelta(seconds=STALE_PROCESSING_SECONDS),
    })
    # 2) then claim the best row exactly once (FOR UPDATE SKIP LOCKED).
    row = db.execute(
        text(CLAIM_NEXT_SQL),
        {
            "processing": STATUS_PROCESSING,
            "queued": STATUS_QUEUED,
            "now": at,
            "aging": AGING_SECONDS_PER_POINT,
        },
    ).first()
    db.commit()
    if row is None:
        return None
    return db.get(SupportQuery, row[0])


def process_one(db: Session, query: SupportQuery) -> str:
    """Run exactly ONE Gemini attempt for a claimed row (Option A budget).

    Context is rebuilt from the row's persisted columns (Step 3b), success
    books an answer + resolved status, and every mapped AI error funnels
    through record_attempt_failure (2/4/8-minute backoff, then needs_human).
    """
    ctx = build_prompt_context(
        db,
        question=query.question,
        customer_email=query.customer_email,
        order_number=query.order_number,
        purchase_date=query.purchase_date,
        order_status=query.order_status,
    )
    if ctx.knowledge_version_id is not None:
        query.knowledge_version_id = ctx.knowledge_version_id

    try:
        result = answer_customer_question_once(
            query.question,
            policy_context=ctx.policy_context,
            situation=ctx.situation,
        )
    except (AIRateLimitError, AITimeoutError,
            AIContentBlockedError, AIProviderError) as exc:
        record_attempt_failure(query, exc)
        db.commit()
        return query.status
    except Exception as exc:
        # Seatbelt (the P0 pair to the 60s reaper): UNMAPPED failures (our
        # own bugs, parse errors) still book an attempt instead of stranding
        # the row until the reaper notices. KeyboardInterrupt/SystemExit are
        # BaseException and deliberately NOT caught -- shutting down should
        # abandon the row, and the reaper owns it from there.
        logger.exception("unexpected worker failure for query %s", query.id)
        record_attempt_failure(query, exc)
        db.commit()
        return query.status

    db.add(Support_Answer(query_id=query.id, answer_text=result.answer_text))
    record_success(query, result.intent)
    db.commit()
    db.refresh(query)
    # Queue the resolution email (idempotent: safe on retries).
    _queue_resolution_email(db, query, result.answer_text)
    return query.status


def run_once(db: Session | None = None) -> bool:
    """Claim + process ONE row, then drain the email outbox.

    False means the queue had nothing ready.  `process_outbox` delivers any
    `queued` resolution emails and retries `failed` ones (bounded at 5
    attempts), so resolution mail reaches customers even when SMTP was down
    at queue time.
    """
    own_session = db is None
    db = db or SessionLocal()
    try:
        query = claim_next(db)
        if query is None:
            return False
        _beat(claimed=True)
        process_one(db, query)
        # Deliver / retry resolution emails after every processed query.
        try:
            process_outbox(db)
        except Exception:  # noqa: BLE001 -- never let mail break the worker
            logger.exception("email outbox processing failed")
        return True
    finally:
        if own_session:
            db.close()


POLL_SECONDS = 1.0

# --- Worker heartbeat (health observability) --------------------------------
# In-memory timestamps (time.monotonic: immune to NTP jumps). Two signals:
#   last_loop_at  -- worker thread is iterating (alive vs dead)
#   last_claim_at -- worker did USEFUL work (claim succeeded; idle vs active)
# "Looping but queue empty" (loop fresh, claim stale) = healthy idle.
# "Not looping at all" (loop stale) = dead thread. Only the heartbeat's
# FRESHNESS is a signal; a live-but-erroring worker keeps beating (the loop
# body already logs + continues) while errors surface in logs/queue-stats.
# Stale threshold lives with the READER (routes_health), not here.
_heartbeat: dict[str, float | None] = {"last_loop_at": None,
                                       "last_claim_at": None}
_HEARTBEAT_LOCK = threading.Lock()


def get_heartbeat() -> dict[str, float | None]:
    with _HEARTBEAT_LOCK:
        return dict(_heartbeat)


def _beat(loop: bool = False, claimed: bool = False) -> None:
    now = time.monotonic()
    with _HEARTBEAT_LOCK:
        if loop:
            _heartbeat["last_loop_at"] = now
        if claimed:
            _heartbeat["last_claim_at"] = now


def run_forever(stop_event: threading.Event,
                poll_seconds: float = POLL_SECONDS) -> None:
    """Drain loop for the in-process thread (or `python -m app.worker`).

    One row at a time keeps Gemini concurrency at exactly 1 per worker and
    lets priority order win between every single claim. An empty queue
    sleeps; a hot queue loops immediately.
    """
    logger.info("queue worker started (poll every %.1fs)", poll_seconds)
    while not stop_event.is_set():
        _beat(loop=True)
        try:
            worked = run_once()
        except Exception:  # a crashed loop is worse than a slow one
            logger.exception("queue worker iteration failed")
            worked = False
        if not worked:
            stop_event.wait(poll_seconds)
    logger.info("queue worker stopped")


if __name__ == "__main__":
    # Standalone worker (optional -- the API runs one in-process by default):
    #   QUERY_MODE=queued python -m app.worker
    import signal

    _stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    run_forever(_stop)
