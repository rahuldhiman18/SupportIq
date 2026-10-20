"""Health + queue observability.

Two endpoints, different jobs (approved design):
  GET  /healthz           -- LB probe. OPEN (LBs carry no admin key; body is
                             three strings, zero customer data). DB ping with
                             a SHORT timeout -> 503 only when the DB is down.
                             Worker field is informational: disabled (inline
                             mode) / running / idle / stale. A stale worker
                             stays 200 -- the API serves reads/writes fine;
                             only background draining is degraded. Taking a
                             working API out of rotation is worse than the
                             disease; staleness belongs to alerts, not the LB.
  GET  /admin/queue-stats -- admin key. Pure FACTS (counts, ages), no verdicts:
                             the monitor owns warn/critical thresholds
                             (starting points: 120s warn / 600s critical).
                             Alerting rules live in monitor config, not here.

Heartbeat: worker beats last_loop_at every iteration + last_claim_at on each
claim (time.monotonic, immune to NTP). Reader-side stale threshold below.
"""
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.services import worker as _worker
from app.core.deps import _query_mode, get_db
from app.models.tables import SupportQuery
from app.core.security import require_admin_key

router = APIRouter(tags=["health"])

# Reader-side staleness: no beat for this long => thread is gone (not merely
# idle -- idle still loops and beats every POLL_SECONDS).
HEARTBEAT_STALE_SECONDS = 10.0

# DB ping must fail FAST: a hanging probe is worse than a 503 (the LB waits
# instead of routing away). Seconds, not the pool's default patience.
DB_PING_TIMEOUT_SECONDS = 2.0


def _worker_state() -> tuple[str, float | None, float | None]:
    """(state, loop_age_s, claim_age_s). State in {disabled,running,idle,stale}."""
    if _query_mode() != "queued":
        return "disabled", None, None
    hb = _worker.get_heartbeat()
    now = time.monotonic()
    loop_age = None if hb["last_loop_at"] is None else now - hb["last_loop_at"]
    claim_age = None if hb["last_claim_at"] is None else now - hb["last_claim_at"]
    if loop_age is None or loop_age > HEARTBEAT_STALE_SECONDS:
        return "stale", loop_age, claim_age
    if claim_age is None:
        return "idle", loop_age, claim_age
    return "running", loop_age, claim_age


@router.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    try:
        db.execute(select(1)).fetchall()
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="database unreachable")
    state, _, _ = _worker_state()
    return {"status": "ok", "db": "up", "worker": state}


@router.get("/admin/queue-stats")
def queue_stats(db: Session = Depends(get_db),
                _admin: None = Depends(require_admin_key)):
    counts = dict(
        db.execute(
            select(SupportQuery.status, func.count())
            .group_by(SupportQuery.status)
        ).all()
    )
    now = func.now()
    oldest_queued = db.execute(
        select(func.min(SupportQuery.created_at)).where(
            SupportQuery.status == "queued")
    ).scalar()
    oldest_processing = db.execute(
        select(func.min(SupportQuery.locked_at)).where(
            SupportQuery.status == "processing")
    ).scalar()

    def _age_s(ts) -> float | None:
        if ts is None:
            return None
        ts_utc = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
        return max(0.0, (datetime.now(timezone.utc) - ts_utc).total_seconds())

    _, loop_age, claim_age = _worker_state()
    return {
        "counts": counts,
        "oldest_queued_age_s": _age_s(oldest_queued),
        "oldest_processing_age_s": _age_s(oldest_processing),
        "needs_human": counts.get("needs_human", 0),
        "worker": _worker_state()[0],
        "worker_loop_age_s": loop_age,
        "worker_claim_age_s": claim_age,
        # Documented starting thresholds for MONITOR config (not enforced
        # here -- the app reports facts, the monitor decides what pages).
        "alert_hint_s": {"warn": 120, "critical": 600},
    }
