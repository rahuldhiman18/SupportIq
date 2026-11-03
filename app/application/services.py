"""Application services that coordinate infrastructure through ports."""

from __future__ import annotations

import time
from collections.abc import Callable

from app.application.ports import AIAnswerer, AnswerCachePort
from app.core.cache import CachedAnswer


class AnswerGenerationService:
    """Generate a grounded answer while hiding cache/lock mechanics.

    The HTTP and worker layers provide an AI function and a cache port. They do
    not need to know whether the cache is Redis, memory, or disabled, nor do
    they need to duplicate stampede-prevention logic.
    """

    def __init__(
        self,
        *,
        answerer: AIAnswerer,
        cache: AnswerCachePort,
        key_factory: Callable[..., str],
    ) -> None:
        self._answerer = answerer
        self._cache = cache
        self._key_factory = key_factory

    def generate(
        self,
        question: str,
        *,
        customer_email: str,
        policy_context: str | None,
        situation: str | None,
    ) -> CachedAnswer:
        key = self._key_factory(
            customer_email=customer_email,
            question=question,
            policy_context=policy_context,
            situation=situation,
        )
        cached = self._cache.get(key)
        lock_token = None
        if cached is None:
            lock_token = self._cache.acquire_lock(key)
            if lock_token is None:
                for _ in range(3 if self._cache.enabled else 1):
                    cached = self._cache.get(key)
                    if cached is not None:
                        break
                    if self._cache.enabled:
                        time.sleep(0.05)

        try:
            if cached is not None:
                return cached
            generated = self._answerer(
                question,
                policy_context=policy_context,
                situation=situation,
            )
            result = CachedAnswer(
                intent=generated.intent,
                answer_text=generated.answer_text,
            )
            self._cache.set(key, result)
            return result
        finally:
            self._cache.release_lock(key, lock_token)
