"""Stable contracts between application logic and infrastructure adapters."""

from __future__ import annotations

from typing import Protocol
from dataclasses import dataclass
from uuid import UUID

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


@dataclass(frozen=True)
class RetrievedSource:
    """Client-safe retrieval evidence returned to the application layer."""

    chunk_id: UUID
    knowledge_answer_id: UUID
    knowledge_version_id: UUID
    source_url: str
    canonical_question: str
    content: str
    similarity_score: float


@dataclass(frozen=True)
class RetrievalResult:
    sources: tuple[RetrievedSource, ...]
    context: str


@dataclass(frozen=True)
class RetrievalRequest:
    question: str
    tenant_id: str | None = None
    max_sources: int = 6
    max_context_chars: int = 20_000


class QueryEmbeddingGenerator(Protocol):
    """Generates the vector representation for a query."""

    def generate(self, question: str) -> list[float]:
        ...


class KnowledgeStore(Protocol):
    """Backend-neutral knowledge search operations."""

    def vector_search(
        self,
        query_embedding: list[float],
        *,
        tenant_id: str | None,
        limit: int,
    ) -> list[RetrievedSource]:
        ...

    def full_text_search(
        self,
        question: str,
        *,
        tenant_id: str | None,
        limit: int,
    ) -> list[RetrievedSource]:
        ...
