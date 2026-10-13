from datetime import datetime
from uuid import UUID
from enum import Enum

from fastapi import FastAPI,Depends,HTTPException
from pydantic import BaseModel, Field

from app.ai_service import generate_support_response
from .database import SessionLocal
from .models import SupportQuery , Support_Answer , KnowledgeAnswer , KnowledgeAnswerVersion
from sqlalchemy import select, desc
from sqlalchemy.orm import Session

app = FastAPI(title = "SupportIq")

#**structure what we get from customer**

class SupportRequest(BaseModel):
    customer_email : str
    question : str = Field(min_length = 1 , max_length = 500)

class QueryResponse(BaseModel):
    query_id:UUID
    question: str
    status: str
    created_at:datetime

class QueryStatus(str,Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    RESOLVED = "Resolved"
    CLOSED = "closed"

class QueryUpdate(BaseModel):
    status: QueryStatus


class KnowledgeCreate(BaseModel):
    canonical_question :str = Field(min_length=1 , max_length=500)
    answer_text:str = Field(min_length=1 , max_length=5000)
    source_url:str = Field(min_length = 1, max_length= 2000)

class KnowledgeDraftResponse(BaseModel):
    knowledge_answer_id : UUID
    version_id : UUID
    canonical_question : str
    answer_text: str
    source_url : str
    status : str
    version_number: int

class AnswerResponse(BaseModel):
    id: UUID
    query_id: UUID
    answer_text: str
    created_at: datetime




def get_db():
    db = SessionLocal()

    try:
       yield db
    finally:
       db.close()

# to create  unique query_id 
@app.post("/queries", status_code = 202,response_model=QueryResponse)
def create_query(request: SupportRequest,db = Depends(get_db)):
    query = SupportQuery(question= request.question,
                         customer_email=request.customer_email)
    db.add(query)
    db.commit()
    db.refresh(query)

    # !Generate an Ai response
    generate_text = generate_support_response(query.question)

    # **create object of Suppoert_Answer to create an answer linked with saved query
    answer = Support_Answer(query_id = query.id, answer_text= generate_text)
    db.add(answer)
    db.commit()
    db.refresh(answer)

    return {
    "query_id": query.id,
    "question": query.question,
    "status": query.status,
    "created_at": query.created_at
}

@app.get("queries/{query_id}",response_model=QueryResponse)
def get_query(query_id:UUID,db = Depends(get_db)):
    result = db.execute(select(SupportQuery).where(SupportQuery.id == query_id))
    query = result.scalar_one_or_none()
    if query is None:
        raise HTTPException(status_code=404,detail="Query not found")
    return {"query_id":query.id,
            "question":query.question,
            "status":query.status,
            "created_at":query.created_at
            }


@app.patch("/queries/{query_id}",response_model=QueryResponse,status_code=200)
def update_query(query_id:UUID, request:QueryUpdate, db = Depends(get_db)):
    result = db.execute(select(SupportQuery).where(SupportQuery.id==query_id))
    query = result.scalar_one_or_none()
    if query is None:
        raise HTTPException(status_code= 404,detail="Query Not found")
    query.status = request.status
    db.commit()
    db.refresh(query)
    return {"query_id":query.id,
            "question":query.question,
            "status":query.status,
            "created_at":query.created_at
            }


@app.get("/queries/{query_id}/answer", response_model=AnswerResponse)
def get_latest_answer(query_id: UUID, db: Session = Depends(get_db)):
    # Find answers for this query, newest first
    stmt = (
        select(Support_Answer)
        .where(Support_Answer.query_id == query_id)
        .order_by(desc(Support_Answer.created_at))
    )
    result = db.execute(stmt)
    answer = result.scalars().first()

    if not answer:
        raise HTTPException(status_code=404, detail="No answer found for this query yet.")

    return answer


@app.post("/knowledge",status_code=201)
def create_knowledge(request: KnowledgeCreate,db: Session = Depends(get_db),):
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


    
    

    
