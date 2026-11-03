"""Stable contracts between application logic and infrastructure adapters."""

from __future__ import annotations

from typing import Protocol

from app.core.cache import CachedAnswer
from app.services.ai_service import CustomerAnswer


class AIAnswerer(Protocol):
    """One provider attempt, independent of Gemini or another vendor."""

    def __call__(
        self,
        question: str,
        *,
        policy_context: str | None = None,
        situation: str | None = None,
    ) -> CustomerAnswer:
        ...


class AnswerCachePort(Protocol):
    """Cache and stampede-lock operations needed by answer generation."""

    @property
    def enabled(self) -> bool:
        ...

    def get(self, key: str) -> CachedAnswer | None:
        ...

    def set(self, key: str, answer: CachedAnswer) -> None:
        ...

    def acquire_lock(self, key: str) -> str | None:
        ...

    def release_lock(self, key: str, token: str | None) -> None:
        ...
