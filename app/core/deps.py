"""Shared FastAPI dependencies: DB session, throttle, query-mode flag.

One-way dependency: routes import from here; nothing here imports routes.
"""
import os
import time
import hashlib
import hmac
import secrets
from collections import defaultdict

from fastapi import HTTPException, Request
from redis import Redis
from redis.exceptions import RedisError

from app.core.database import SessionLocal


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


# Shared Redis sliding-window limiter: 30 protected requests per client/minute.
# The local dictionary remains only as a development/test fallback when Redis
# is not configured. When REDIS_URL is configured, Redis failures fail closed
# so an outage cannot silently remove protection from Gemini/login endpoints.
_RATE_LIMIT = 30
_RATE_WINDOW_SECONDS = 60.0
_rate_hits: dict[str, list[float]] = defaultdict(list)

_REDIS_LIMIT_SCRIPT = """
local now = redis.call('TIME')
local now_ms = (now[1] * 1000) + math.floor(now[2] / 1000)
local cutoff = now_ms - tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, cutoff)
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[2]) then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return {0, count, oldest[2] or now_ms}
end
redis.call('ZADD', KEYS[1], now_ms, ARGV[3])
redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[1]) / 1000) + 1)
return {1, count + 1, 0}
"""


def _rate_limit_key(client_id: str) -> str:
    # Do not store raw IP addresses in Redis. A keyed digest prevents the
    # limiter keyspace from becoming a customer/network identifier store.
    secret = os.getenv("CACHE_KEY_SECRET") or os.getenv("JWT_SECRET_KEY")
    if not secret:
        secret = "supportiq-rate-limit-development-only"
    digest = hmac.new(
        secret.encode("utf-8"), client_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"supportiq:ratelimit:v1:{digest}"


def _redis_client() -> Redis | None:
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    # Construct once per process. Short timeouts stop Redis from becoming a
    # request-thread bottleneck while still failing closed on an outage.
    client = getattr(_redis_client, "_client", None)
    if client is None:
        client = Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
            health_check_interval=30,
        )
        _redis_client._client = client
    return client


def _throttle_with_redis(client_id: str) -> None:
    client = _redis_client()
    if client is None:
        return
    try:
        allowed, _count, oldest_ms = client.eval(
            _REDIS_LIMIT_SCRIPT,
            1,
            _rate_limit_key(client_id),
            int(_RATE_WINDOW_SECONDS * 1000),
            _RATE_LIMIT,
            secrets.token_urlsafe(16),
        )
    except RedisError as exc:
        raise HTTPException(
            status_code=503,
            detail="Rate limiting temporarily unavailable",
        ) from exc

    if int(allowed) != 1:
        retry_after = max(
            1,
            int((_RATE_WINDOW_SECONDS * 1000 - (time.time() * 1000 - int(oldest_ms))) / 1000),
        )
        raise HTTPException(
            status_code=429,
            detail="Too many requests - please wait a minute",
            headers={"Retry-After": str(retry_after)},
        )


def throttle_public_chat(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    if _redis_client() is not None:
        _throttle_with_redis(ip)
        return

    now = time.monotonic()
    hits = [t for t in _rate_hits[ip] if now - t < _RATE_WINDOW_SECONDS]
    _rate_hits[ip] = hits
    if len(hits) >= _RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="Too many requests - please wait a minute",
            headers={"Retry-After": "60"},
        )
    hits.append(now)
