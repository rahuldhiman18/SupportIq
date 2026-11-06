"""Knowledge ingestion and approval routes."""
from datetime import datetime, timezone
import os
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models.tables import KnowledgeAnswer, KnowledgeAnswerVersion
from app.schemas.dto import KnowledgeCreate
from app.core.security import require_admin_key

router = APIRouter(tags=["knowledge"])


@router.post("/knowledge",status_code=201)
def create_knowledge(request: KnowledgeCreate,db: Session = Depends(get_db),
                     _admin: None = Depends(require_admin_key),):
    knowledge = KnowledgeAnswer(canonical_question = request.canonical_question,
    source_url = request.source_url,
    tenant_id=request.tenant_id,
    is_active = False,)
    try:
        db.add(knowledge)
        db.flush()


        version = KnowledgeAnswerVersion(
            knowledge_answer_id=knowledge.id,
            version_number=1,
            answer_text=request.answer_text,
            status="draft",
            is_current=False,
            approved_by=None,
            approved_at=None,
        )


        db.add(version)
        db.commit()
        db.refresh(knowledge)
        db.refresh(version)

        return {
            "knowledge_answer_id": knowledge.id,
            "version_id": version.id,
            "canonical_question": knowledge.canonical_question,
            "answer_text": version.answer_text,
            "source_url": knowledge.source_url,
            "status": version.status,
            "version_number": version.version_number,
        }

    except Exception:
        db.rollback()
        raise


@router.post(
    "/knowledge/{knowledge_answer_id}/versions/{version_id}/approve",
    status_code=200,
)
def approve_knowledge_version(
    knowledge_answer_id: UUID,
    version_id: UUID,
    db: Session = Depends(get_db),
    _admin: None = Depends(require_admin_key),
):
    """Approve one draft and index it into tenant-scoped pgvector chunks."""
    knowledge = db.get(KnowledgeAnswer, knowledge_answer_id)
    if knowledge is None:
        raise HTTPException(status_code=404, detail="Knowledge article not found")

    version = db.get(KnowledgeAnswerVersion, version_id)
    if (
        version is None
        or version.knowledge_answer_id != knowledge_answer_id
    ):
        raise HTTPException(status_code=404, detail="Knowledge version not found")
    if version.status == "rejected":
        raise HTTPException(status_code=409, detail="Knowledge version is rejected")

    current_versions = db.execute(
        select(KnowledgeAnswerVersion).where(
            KnowledgeAnswerVersion.knowledge_answer_id == knowledge_answer_id,
            KnowledgeAnswerVersion.is_current.is_(True),
        )
    ).scalars().all()
    for current in current_versions:
        current.is_current = False

    knowledge.is_active = True
    version.status = "approved"
    version.is_current = True
    version.approved_by = "admin-api-key"
    version.approved_at = datetime.now(timezone.utc)

    indexed_chunks = 0
    if os.getenv("RAG_ENABLED", "false").lower() == "true":
        try:
            from app.services.knowledge_indexing import (
                FixedSizeTextChunker,
                GeminiDocumentEmbeddingGenerator,
                PostgreSQLKnowledgeChunkIndexer,
            )

            indexer = PostgreSQLKnowledgeChunkIndexer(
                db,
                chunker=FixedSizeTextChunker(),
                embedding_generator=GeminiDocumentEmbeddingGenerator(),
            )
            indexed_chunks = indexer.index_version(
                knowledge_answer_id=knowledge_answer_id,
                knowledge_version_id=version_id,
                tenant_id=knowledge.tenant_id,
                content=version.answer_text,
            )
        except Exception as exc:
            db.rollback()
            raise HTTPException(
                status_code=502,
                detail="Knowledge approval failed because vector indexing is unavailable",
            ) from exc
    else:
        db.commit()

    db.refresh(knowledge)
    db.refresh(version)
    return {
        "knowledge_answer_id": knowledge.id,
        "version_id": version.id,
        "status": version.status,
        "is_current": version.is_current,
        "indexed_chunks": indexed_chunks,
    }
