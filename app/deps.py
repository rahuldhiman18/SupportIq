"""Shared FastAPI dependencies: DB session, throttle, query-mode flag.

One-way dependency: routes import from here; nothing here imports routes.
"""
import os
import time
from collections import defaultdict

from fastapi import HTTPException, Request

from .database import SessionLocal


# ** Step 3b: QUERY_MODE flag -- "inline" (Phase A, default) keeps the AI
#    call inside the HTTP request; "queued" enqueues only and a background
#    worker drains by effective_score (FOR UPDATE SKIP LOCKED). Default
#    stays inline until queued mode passes live smoke; flip via env.
def _query_mode() -> str:
    return os.getenv("QUERY_MODE", "inline").strip().lower()


def get_db():
    db = SessionLocal()

    try:
       yield db
    finally:
       db.close()


# Tiny in-memory sliding-window limiter: 30 POST /queries per IP per minute.
# No new dependency (slowapi) for Phase A; swap for Redis in Phase C.
_RATE_LIMIT = 30
_RATE_WINDOW_SECONDS = 60.0
_rate_hits: dict[str, list[float]] = defaultdict(list)


def throttle_public_chat(request: Request) -> None:
    now = time.monotonic()
    ip = request.client.host if request.client else "unknown"
    hits = [t for t in _rate_hits[ip] if now - t < _RATE_WINDOW_SECONDS]
    _rate_hits[ip] = hits
    if len(hits) >= _RATE_LIMIT:
        raise HTTPException(status_code=429,
                            detail="Too many requests - please wait a minute")
    hits.append(now)
