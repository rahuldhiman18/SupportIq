"""Health and queue observability endpoints."""

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.deps import _query_mode, get_db
from app.core.health import check_health
from app.core.security import require_admin_key
from app.models.tables import SupportQuery
from app.services import worker as _worker

router = APIRouter(tags=["health"])

HEARTBEAT_STALE_SECONDS = 10.0
DB_PING_TIMEOUT_SECONDS = 2.0


def _worker_state() -> tuple[str, float | None, float | None]:
    """Return worker state and heartbeat ages."""
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
    """Load-balancer probe with minimal status output."""
    components = check_health(db)
    if not all(components.values()):
        raise HTTPException(status_code=503, detail="degraded")
    return {"status": "ok"}


@router.get("/admin/queue-stats")
def queue_stats(
    db: Session = Depends(get_db),
    _admin: None = Depends(require_admin_key),
):
    counts = dict(
        db.execute(
            select(SupportQuery.status, func.count()).group_by(SupportQuery.status)
        ).all()
    )
    oldest_queued = db.execute(
        select(func.min(SupportQuery.created_at)).where(
            SupportQuery.status == "queued"
        )
    ).scalar()
    oldest_processing = db.execute(
        select(func.min(SupportQuery.locked_at)).where(
            SupportQuery.status == "processing"
        )
    ).scalar()

    def _age_s(ts) -> float | None:
        if ts is None:
            return None
        ts_utc = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
        return max(0.0, (datetime.now(timezone.utc) - ts_utc).total_seconds())

    worker_state, loop_age, claim_age = _worker_state()
    return {
        "counts": counts,
        "oldest_queued_age_s": _age_s(oldest_queued),
        "oldest_processing_age_s": _age_s(oldest_processing),
        "needs_human": counts.get("needs_human", 0),
        "components": check_health(db),
        "worker": worker_state,
        "worker_loop_age_s": loop_age,
        "worker_claim_age_s": claim_age,
        "alert_hint_s": {"warn": 120, "critical": 600},
    }
