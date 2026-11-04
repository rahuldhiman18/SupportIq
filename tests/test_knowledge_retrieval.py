"""Retrieval orchestration tests with fake provider/store substitutes."""

from uuid import uuid4

import pytest

from app.application.ports import (
    RetrievedSource,
    RetrievalRequest,
)
from app.services.knowledge_retrieval import HybridKnowledgeRetriever


class FakeEmbeddingGenerator:
    def generate(self, question: str) -> list[float]:
        assert question == "refund policy"
        return [0.1, 0.2]


class FakeStore:
    def __init__(self) -> None:
        self.vector_filters = None
        self.text_filters = None
        chunk_id = uuid4()
        version_id = uuid4()
        answer_id = uuid4()
        self.sources = [
            RetrievedSource(
                chunk_id=chunk_id,
                knowledge_answer_id=answer_id,
                knowledge_version_id=version_id,
                source_url="https://example.com/refunds",
                canonical_question="Refund policy",
                content="Approved refunds are available within 30 days.",
                similarity_score=0.90,
            )
        ]

    def vector_search(self, embedding, *, tenant_id, limit):
        self.vector_filters = (tenant_id, limit)
        return self.sources

    def full_text_search(self, question, *, tenant_id, limit):
        self.text_filters = (tenant_id, limit)
        return self.sources


def test_hybrid_retrieval_merges_caps_and_returns_source_metadata():
    store = FakeStore()
    retriever = HybridKnowledgeRetriever(
        embedding_generator=FakeEmbeddingGenerator(),
        store=store,
        max_sources=6,
        max_context_chars=500,
    )

    result = retriever.retrieve(
        RetrievalRequest(
            question="refund policy",
            tenant_id="tenant-a",
            max_sources=3,
            max_context_chars=500,
        )
    )

    assert len(result.sources) == 1
    source = result.sources[0]
    assert source.source_url == "https://example.com/refunds"
    assert source.knowledge_version_id is not None
    assert 0.0 <= source.similarity_score <= 1.0
    assert "Approved refunds" in result.context
    assert store.vector_filters[0] == "tenant-a"
    assert store.text_filters[0] == "tenant-a"


def test_hybrid_retrieval_caps_context_characters():
    store = FakeStore()
    store.sources[0] = RetrievedSource(
        chunk_id=store.sources[0].chunk_id,
        knowledge_answer_id=store.sources[0].knowledge_answer_id,
        knowledge_version_id=store.sources[0].knowledge_version_id,
        source_url="https://example.com/refunds",
        canonical_question="Refund policy",
        content="X" * 5000,
        similarity_score=0.8,
    )
    retriever = HybridKnowledgeRetriever(
        embedding_generator=FakeEmbeddingGenerator(),
        store=store,
    )

    result = retriever.retrieve(
        RetrievalRequest(
            question="refund policy",
            max_sources=1,
            max_context_chars=120,
        )
    )

    assert len(result.context) <= 120


def test_empty_retrieval_question_is_rejected():
    retriever = HybridKnowledgeRetriever(
        embedding_generator=FakeEmbeddingGenerator(),
        store=FakeStore(),
    )

    with pytest.raises(ValueError, match="retrieval question is required"):
        retriever.retrieve(RetrievalRequest(question="   "))
