"""Deep health checks: DB + Gemini connectivity.

Gemini check uses `count_tokens` on an empty string (free, no quota cost)
and caches the result for 30 seconds to avoid hammering the provider.

All functions return plain booleans — no internal details leaked.
The HTTP layer decides what to expose (minimal on /healthz, detailed on
/admin/queue-stats which is admin-key protected).
"""

import time
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.services.ai_service import client as gemini_client


# --------------------------------------------------------------------------- #
# TTL cache for expensive checks (Gemini)
# --------------------------------------------------------------------------- #

class _TTLCache:
    """Tiny TTL cache: key -> (value, expiry_timestamp)."""

    def __init__(self, ttl_seconds: float):
        self.ttl = ttl_seconds
        self._store: dict = {}

    def get(self, key: str, factory: Callable[[], bool]) -> bool:
        now = time.monotonic()
        entry = self._store.get(key)
        if entry is not None and now < entry[1]:
            return entry[0]
        value = factory()
        self._store[key] = (value, now + self.ttl)
        return value


_gemini_cache = _TTLCache(ttl_seconds=30.0)


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #

def check_database(db: Session) -> bool:
    """Return True if DB responds to SELECT 1 within the session timeout."""
    try:
        db.execute(select(1)).fetchall()
        return True
    except SQLAlchemyError:
        return False


def check_gemini() -> bool:
    """Return True if Gemini count_tokens responds (cached 30s).

    Uses count_tokens on an empty string — free tier, no quota consumed.
    Cached for 30s so repeated /healthz probes don't hammer the provider.
    """

    def _probe() -> bool:
        try:
            gemini_client.models.count_tokens(
                model="gemini-3.5-flash-lite",
                contents="",
            )
            return True
        except Exception:  # noqa: BLE001 — any failure means degraded
            return False

    return _gemini_cache.get("gemini", _probe)


def check_health(db: Session) -> dict[str, bool]:
    """Run all health checks and return component statuses (facts only)."""
    return {
        "database": check_database(db),
        "gemini": check_gemini(),
    }