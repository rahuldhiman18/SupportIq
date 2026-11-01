"""Optional Redis response cache for grounded AI answers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from typing import Any

from redis import Redis
from redis.exceptions import RedisError


@dataclass(frozen=True)
class CachedAnswer:
    intent: str
    answer_text: str


class AnswerCache:
    """Best-effort shared cache for successful grounded AI answers."""

    def __init__(self) -> None:
        self._url = os.getenv("REDIS_URL")
        self._ttl = max(1, int(os.getenv("AI_CACHE_TTL_SECONDS", "600")))
        self._lock_ttl = max(1, int(os.getenv("AI_CACHE_LOCK_SECONDS", "15")))
        self._client: Redis | None = None

    def _redis(self) -> Redis | None:
        if not self._url:
            return None
        if self._client is None:
            self._client = Redis.from_url(
                self._url,
                decode_responses=True,
                socket_connect_timeout=0.1,
                socket_timeout=0.1,
                health_check_interval=30,
            )
        return self._client

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def key(self, payload: dict[str, Any]) -> str:
        """Return a non-sensitive, versioned cache key."""
        secret = os.getenv("CACHE_KEY_SECRET") or os.getenv("JWT_SECRET_KEY")
        if not secret:
            secret = "supportiq-cache-development-only"
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        digest = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
        return f"supportiq:ai:v1:{digest}"

    def get(self, key: str) -> CachedAnswer | None:
        client = self._redis()
        if client is None:
            return None
        try:
            raw = client.get(key)
            if not raw:
                return None
            value = json.loads(raw)
            if not isinstance(value, dict):
                return None
            intent = value.get("intent")
            answer_text = value.get("answer_text")
            if not isinstance(intent, str) or not isinstance(answer_text, str):
                return None
            return CachedAnswer(intent=intent, answer_text=answer_text)
        except (RedisError, ValueError, TypeError):
            return None

    def set(self, key: str, answer: CachedAnswer) -> None:
        client = self._redis()
        if client is None:
            return
        try:
            client.set(
                key,
                json.dumps({"intent": answer.intent, "answer_text": answer.answer_text}),
                ex=self._ttl,
            )
        except RedisError:
            return

    def acquire_lock(self, key: str) -> str | None:
        """Acquire a short stampede-prevention lock, best effort."""
        client = self._redis()
        if client is None:
            return None
        token = secrets.token_urlsafe(16)
        try:
            return token if client.set(f"{key}:lock", token, nx=True, ex=self._lock_ttl) else None
        except RedisError:
            return None

    def release_lock(self, key: str, token: str | None) -> None:
        if not token:
            return
        client = self._redis()
        if client is None:
            return
        try:
            client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                f"{key}:lock",
                token,
            )
        except RedisError:
            return


answer_cache = AnswerCache()


def build_answer_cache_key(
    *,
    customer_email: str,
    question: str,
    policy_context: str | None,
    situation: str | None,
) -> str:
    return answer_cache.key({
        "customer_scope": customer_email.strip().lower(),
        "question": question.strip(),
        "policy_context": policy_context,
        "situation": situation,
        "model": "gemini-3.5-flash-lite",
        "prompt_version": "v1",
        "temperature": 0.2,
        "max_output_tokens": 300,
    })
