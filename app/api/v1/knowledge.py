"""Knowledge routes: draft creation (approval flow = later)."""
from fastapi import APIRouter, Depends
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
