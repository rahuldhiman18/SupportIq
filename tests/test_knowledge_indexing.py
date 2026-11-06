from types import SimpleNamespace
from uuid import uuid4

from app.services.knowledge_indexing import (
    FixedSizeTextChunker,
    PostgreSQLKnowledgeChunkIndexer,
)
from app.models.tables import KnowledgeAnswer, KnowledgeAnswerVersion


class FakeEmbeddingGenerator:
    def generate(self, text: str) -> list[float]:
        return [0.25, 0.75] if text else []


class FakeDB:
    def __init__(self) -> None:
        self.added = []
        self.commits = 0
        version_id = uuid4()
        answer_id = uuid4()
        self.version = SimpleNamespace(
            id=version_id,
            knowledge_answer_id=answer_id,
            is_current=True,
            status="approved",
        )
        self.answer_id = answer_id
        self.version_id = version_id

    def get(self, model, key):
        return self.version if key == self.version_id else None

    def execute(self, statement):
        return None

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1


def test_fixed_size_chunker_returns_bounded_chunks():
    chunker = FixedSizeTextChunker(chunk_chars=40, overlap_chars=5)
    chunks = chunker.chunk("Refunds are available within thirty days. Contact support.")

    assert chunks
    assert all(len(chunk) <= 40 for chunk in chunks)
    assert chunks[0].startswith("Refunds")


def test_indexer_only_indexes_an_approved_current_version():
    db = FakeDB()
    indexer = PostgreSQLKnowledgeChunkIndexer(
        db,
        chunker=FixedSizeTextChunker(chunk_chars=100, overlap_chars=10),
        embedding_generator=FakeEmbeddingGenerator(),
    )

    indexed = indexer.index_version(
        knowledge_answer_id=db.answer_id,
        knowledge_version_id=db.version_id,
        tenant_id="tenant-a",
        content="Approved refunds are available within 30 days.",
    )

    assert indexed == 1
    assert len(db.added) == 1
    assert db.added[0].tenant_id == "tenant-a"
    assert db.added[0].content_hash
    assert db.commits == 1


def test_approval_promotes_version_without_indexing_when_rag_disabled(
    client,
    db_session,
    monkeypatch,
):
    monkeypatch.setenv("RAG_ENABLED", "false")
    article = KnowledgeAnswer(
        canonical_question="Refund policy",
        source_url="https://example.com/refunds",
        is_active=False,
    )
    db_session.add(article)
    db_session.flush()
    version = KnowledgeAnswerVersion(
        knowledge_answer_id=article.id,
        version_number=1,
        answer_text="Approved refunds are available within 30 days.",
        status="draft",
        is_current=False,
    )
    db_session.add(version)
    db_session.commit()

    response = client.post(
        f"/knowledge/{article.id}/versions/{version.id}/approve",
        headers={"X-Admin-Key": "test-admin-key"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["is_current"] is True
    assert body["indexed_chunks"] == 0
