"""Approved knowledge retrieval orchestration."""

from __future__ import annotations

import os
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from google import genai
from google.genai import types

from app.application.ports import (
    KnowledgeStore,
    QueryEmbeddingGenerator,
    RetrievedSource,
    RetrievalRequest,
    RetrievalResult,
)
MAX_RETRIEVAL_SOURCES = 6
MAX_RETRIEVAL_CONTEXT_CHARS = 20_000
VECTOR_CANDIDATE_MULTIPLIER = 4
FULL_TEXT_CANDIDATE_MULTIPLIER = 4
EMBEDDING_MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "gemini-embedding-001",
)


class GeminiQueryEmbeddingGenerator(QueryEmbeddingGenerator):
    """Gemini adapter; only query embedding mechanics are provider-specific."""

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        self._client = genai.Client(api_key=key)

    def generate(self, question: str) -> list[float]:
        response = self._client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=question.strip(),
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY",
                output_dimensionality=768,
            ),
        )
        embeddings = getattr(response, "embeddings", None) or []
        if not embeddings or not getattr(embeddings[0], "values", None):
            raise RuntimeError("embedding provider returned no vector")
        return list(embeddings[0].values)


class PostgreSQLKnowledgeStore(KnowledgeStore):
    """PostgreSQL implementation of vector and full-text search."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def vector_search(
        self,
        query_embedding: list[float],
        *,
        tenant_id: str | None,
        limit: int,
    ) -> list[RetrievedSource]:
        if not query_embedding:
            return []

        vector_literal = "[" + ",".join(str(float(v)) for v in query_embedding) + "]"
        tenant_clause = (
            "AND k.tenant_id = :tenant_id AND c.tenant_id = :tenant_id"
            if tenant_id is not None
            else "AND k.tenant_id IS NULL AND c.tenant_id IS NULL"
        )
        sql = text(
            """
            SELECT c.id, c.knowledge_answer_id, c.knowledge_version_id,
                   k.source_url, k.canonical_question, c.content,
                   1 - (c.embedding <=> CAST(:embedding AS vector)) AS score
            FROM knowledge_chunks c
            JOIN knowledge_answer_versions v
              ON v.id = c.knowledge_version_id
            JOIN knowledge_answers k
              ON k.id = c.knowledge_answer_id
            WHERE c.is_active = true
              AND v.is_current = true
              AND v.status = 'approved'
              AND k.is_active = true
              AND c.embedding IS NOT NULL
              AND c.tenant_id = k.tenant_id
              """ + tenant_clause + """
            ORDER BY c.embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
            """
        )
        rows = self._db.execute(
            sql,
            {"embedding": vector_literal, "tenant_id": tenant_id, "limit": limit},
        ).all()
        return [self._row_to_source(row) for row in rows]

    def full_text_search(
        self,
        question: str,
        *,
        tenant_id: str | None,
        limit: int,
    ) -> list[RetrievedSource]:
        if not question.strip():
            return []

        tenant_clause = (
            "AND k.tenant_id = :tenant_id AND c.tenant_id = :tenant_id"
            if tenant_id is not None
            else "AND k.tenant_id IS NULL AND c.tenant_id IS NULL"
        )
        sql = text(
            """
            SELECT c.id, c.knowledge_answer_id, c.knowledge_version_id,
                   k.source_url, k.canonical_question, c.content,
                   ts_rank(
                       to_tsvector('english', c.content),
                       websearch_to_tsquery('english', :question)
                   ) AS score
            FROM knowledge_chunks c
            JOIN knowledge_answer_versions v
              ON v.id = c.knowledge_version_id
            JOIN knowledge_answers k
              ON k.id = c.knowledge_answer_id
            WHERE c.is_active = true
              AND v.is_current = true
              AND v.status = 'approved'
              AND k.is_active = true
              AND to_tsvector('english', c.content) @@
                  websearch_to_tsquery('english', :question)
              """ + tenant_clause + """
            ORDER BY score DESC
            LIMIT :limit
            """
        )
        rows = self._db.execute(
            sql,
            {"question": question, "tenant_id": tenant_id, "limit": limit},
        ).all()
        return [self._row_to_source(row) for row in rows]

    @staticmethod
    def _row_to_source(row: object) -> RetrievedSource:
        return RetrievedSource(
            chunk_id=row[0],
            knowledge_answer_id=row[1],
            knowledge_version_id=row[2],
            source_url=row[3],
            canonical_question=row[4],
            content=row[5],
            similarity_score=max(0.0, min(1.0, float(row[6]))),
        )


class HybridKnowledgeRetriever:
    """Generate, search, merge, and cap approved knowledge evidence."""

    def __init__(
        self,
        *,
        embedding_generator: QueryEmbeddingGenerator,
        store: KnowledgeStore,
        max_sources: int = MAX_RETRIEVAL_SOURCES,
        max_context_chars: int = MAX_RETRIEVAL_CONTEXT_CHARS,
        vector_weight: float = 0.65,
        text_weight: float = 0.35,
    ) -> None:
        self._embedding_generator = embedding_generator
        self._store = store
        self._max_sources = max_sources
        self._max_context_chars = max_context_chars
        self._vector_weight = vector_weight
        self._text_weight = text_weight

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        question = self._validate_question(request.question)
        max_sources = max(1, min(request.max_sources, self._max_sources))
        max_context_chars = max(
            1,
            min(request.max_context_chars, self._max_context_chars),
        )

        embedding = self._embedding_generator.generate(question)
        candidate_limit = max_sources * max(
            VECTOR_CANDIDATE_MULTIPLIER,
            FULL_TEXT_CANDIDATE_MULTIPLIER,
        )

        vector_sources = self._store.vector_search(
            embedding,
            tenant_id=request.tenant_id,
            limit=candidate_limit,
        )
        text_sources = self._store.full_text_search(
            question,
            tenant_id=request.tenant_id,
            limit=candidate_limit,
        )

        merged = self._merge(vector_sources, text_sources)
        selected, context = self._cap_sources(
            merged,
            max_sources=max_sources,
            max_context_chars=max_context_chars,
        )
        return RetrievalResult(sources=tuple(selected), context=context)

    @staticmethod
    def _validate_question(question: str) -> str:
        value = question.strip() if isinstance(question, str) else ""
        if not value:
            raise ValueError("retrieval question is required")
        return value[:10_000]

    def _merge(
        self,
        vector_sources: list[RetrievedSource],
        text_sources: list[RetrievedSource],
    ) -> list[RetrievedSource]:
        scores: dict[UUID, float] = {}
        sources: dict[UUID, RetrievedSource] = {}

        for rank, source in enumerate(vector_sources, start=1):
            sources[source.chunk_id] = source
            scores[source.chunk_id] = scores.get(source.chunk_id, 0.0) + (
                self._vector_weight * self._rank_score(rank, len(vector_sources))
                + self._vector_weight * source.similarity_score
            )
        for rank, source in enumerate(text_sources, start=1):
            if source.chunk_id not in sources:
                sources[source.chunk_id] = source
            scores[source.chunk_id] = scores.get(source.chunk_id, 0.0) + (
                self._text_weight * self._rank_score(rank, len(text_sources))
                + self._text_weight * source.similarity_score
            )

        merged = []
        for chunk_id, source in sources.items():
            merged.append(
                RetrievedSource(
                    chunk_id=source.chunk_id,
                    knowledge_answer_id=source.knowledge_answer_id,
                    knowledge_version_id=source.knowledge_version_id,
                    source_url=source.source_url,
                    canonical_question=source.canonical_question,
                    content=source.content,
                    similarity_score=max(0.0, min(1.0, scores[chunk_id])),
                )
            )
        merged.sort(key=lambda source: source.similarity_score, reverse=True)
        return merged

    @staticmethod
    def _rank_score(rank: int, candidate_count: int) -> float:
        if candidate_count <= 0:
            return 0.0
        return 1.0 / rank

    @staticmethod
    def _cap_sources(
        sources: list[RetrievedSource],
        *,
        max_sources: int,
        max_context_chars: int,
    ) -> tuple[list[RetrievedSource], str]:
        selected: list[RetrievedSource] = []
        parts: list[str] = []
        remaining = max_context_chars

        for source in sources[:max_sources]:
            if remaining <= 0:
                break
            header = (
                f"Source: {source.source_url}\n"
                f"Version: {source.knowledge_version_id}\n"
            )
            available = remaining - len(header) - 2
            if available <= 0:
                break
            content = source.content[:available].rstrip()
            if not content:
                break
            selected.append(source)
            parts.append(
                header + content
            )
            remaining -= len(header) + len(content)
            if remaining > 0:
                remaining -= 2

        return selected, "\n\n".join(parts)
