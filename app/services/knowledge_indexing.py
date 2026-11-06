"""Knowledge ingestion and indexing adapters."""

from __future__ import annotations

import hashlib
import os
import re
from uuid import UUID

from google import genai
from google.genai import types
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.application.ports import (
    DocumentEmbeddingGenerator,
    KnowledgeChunkIndexer,
    TextChunker,
)
from app.models.tables import KnowledgeChunk


class FixedSizeTextChunker(TextChunker):
    """Split text by paragraphs and sentences without provider-specific logic."""

    def __init__(self, chunk_chars: int = 1_500, overlap_chars: int = 150) -> None:
        if chunk_chars <= overlap_chars or overlap_chars < 0:
            raise ValueError("invalid chunk configuration")
        self._chunk_chars = chunk_chars
        self._overlap_chars = overlap_chars

    def chunk(self, text: str) -> list[str]:
        normalized = re.sub(r"\s+", " ", text or "").strip()
        if not normalized:
            return []
        chunks: list[str] = []
        start = 0
        while start < len(normalized):
            end = min(len(normalized), start + self._chunk_chars)
            chunk = normalized[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= len(normalized):
                break
            start = end - self._overlap_chars
        return chunks


class GeminiDocumentEmbeddingGenerator(DocumentEmbeddingGenerator):
    """Gemini implementation for indexing knowledge documents."""

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        self._client = genai.Client(api_key=key)
        self.model = os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")

    def generate(self, text: str) -> list[float]:
        if not text.strip():
            return []
        response = self._client.models.embed_content(
            model=self.model,
            contents=text.strip(),
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT",
                output_dimensionality=768,
            ),
        )
        embeddings = getattr(response, "embeddings", None) or []
        if not embeddings or not getattr(embeddings[0], "values", None):
            raise RuntimeError("embedding provider returned no vector")
        return list(embeddings[0].values)


class PostgreSQLKnowledgeChunkIndexer(KnowledgeChunkIndexer):
    """Index approved knowledge using the existing pgvector chunk model."""

    def __init__(
        self,
        db: Session,
        *,
        chunker: TextChunker,
        embedding_generator: DocumentEmbeddingGenerator,
    ) -> None:
        self._db = db
        self._chunker = chunker
        self._embedding_generator = embedding_generator

    def index_version(
        self,
        *,
        knowledge_answer_id: UUID,
        knowledge_version_id: UUID,
        tenant_id: str | None,
        content: str,
    ) -> int:
        from app.models.tables import KnowledgeAnswerVersion

        version = self._db.get(KnowledgeAnswerVersion, knowledge_version_id)
        if (
            version is None
            or version.knowledge_answer_id != knowledge_answer_id
            or not version.is_current
            or version.status != "approved"
        ):
            raise ValueError("only an approved current version can be indexed")

        self._db.execute(
            delete(KnowledgeChunk).where(
                KnowledgeChunk.knowledge_version_id == knowledge_version_id
            )
        )

        indexed = 0
        for chunk_index, chunk_text in enumerate(self._chunker.chunk(content)):
            embedding = self._embedding_generator.generate(chunk_text)
            if not embedding:
                continue
            content_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
            row = KnowledgeChunk(
                knowledge_answer_id=knowledge_answer_id,
                tenant_id=tenant_id,
                knowledge_version_id=knowledge_version_id,
                chunk_index=chunk_index,
                content=chunk_text,
                content_hash=content_hash,
                embedding_model=os.getenv("EMBEDDING_MODEL", "gemini-embedding-001"),
                embedding=embedding,
                is_active=True,
            )
            self._db.add(row)
            indexed += 1
        self._db.commit()
        return indexed
