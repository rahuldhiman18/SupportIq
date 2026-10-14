import re
from datetime import datetime, date
from uuid import UUID
from enum import Enum
from typing import Literal

from fastapi import FastAPI,Depends,HTTPException
from pydantic import BaseModel, Field

from app.ai_service import (
    answer_customer_question,
    AITimeoutError,
    AIRateLimitError,
    AIProviderError,
    AIContentBlockedError,
)
from .database import SessionLocal
from .models import SupportQuery , Support_Answer , KnowledgeAnswer , KnowledgeAnswerVersion , Order , OrderEvent
from sqlalchemy import select, desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

app = FastAPI(title = "SupportIq")

#**structure what we get from customer**

class SupportRequest(BaseModel):
    customer_email : str
    question : str = Field(min_length = 1 , max_length = 500)
    # Optional situation details -> lets the AI personalize (e.g. eligibility math)
    purchase_date: date | None = None
    order_status: str | None = Field(default=None, max_length=50)
    # Optional: exact order number -> live tracking facts get injected
    order_number: str | None = Field(default=None, max_length=32)

class QueryResponse(BaseModel):
    query_id:UUID
    question: str
    status: str
    created_at:datetime
    intent: str | None = None

class QueryStatus(str,Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    RESOLVED = "resolved"
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


# ** Phase 1: order tracking API (dummy orders feed the location algo)
class OrderEventIn(BaseModel):
    stage: Literal["placed", "packed", "shipped", "out_for_delivery", "delivered"]
    location: str = Field(default="", max_length=200)
    occurred_at: datetime


class OrderCreate(BaseModel):
    order_number: str = Field(min_length=3, max_length=32)
    customer_email: str
    delivery_method: Literal["standard", "express"] = "standard"
    destination_city: str = Field(default="", max_length=100)
    carrier: str | None = Field(default=None, max_length=100)
    events: list[OrderEventIn] = []


class OrderTrackResponse(BaseModel):
    order_number: str
    stage: str
    current_location: str
    eta_earliest: datetime | None = None
    eta_latest: datetime | None = None
    stalled: bool
    delivery_method: str
    destination_city: str




def get_db():
    db = SessionLocal()

    try:
       yield db
    finally:
       db.close()


# ** Order-number extractor from free text: "status for ORD-9002" / "#A12345"
_ORDER_NUMBER_RE = re.compile(r"\b#?([A-Za-z]{1,4}-?\d{4,8})\b")


def _extract_order_number(question: str) -> str | None:
    match = _ORDER_NUMBER_RE.search(question)
    return match.group(1).upper() if match else None


@app.post("/orders", status_code=201)
def create_order(request: OrderCreate, db: Session = Depends(get_db)):
    order = Order(
        order_number=request.order_number.strip().upper(),
        customer_email=request.customer_email,
        delivery_method=request.delivery_method,
        destination_city=request.destination_city,
        carrier=request.carrier,
    )
    db.add(order)
    db.flush()
    for event in request.events:
        db.add(OrderEvent(order_id=order.id, stage=event.stage,
                          location=event.location,
                          occurred_at=event.occurred_at))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Order number already exists")
    return {"order_number": order.order_number, "status": "created",
            "events": len(request.events)}


@app.get("/orders/{order_number}", response_model=OrderTrackResponse)
def get_order(order_number: str, email: str, db: Session = Depends(get_db)):
    # Scoped by email: wrong/missing email = 404, never leak other orders
    order = db.execute(
        select(Order).where(
            Order.order_number == order_number.strip().upper(),
            Order.customer_email == email,
        )
    ).scalars().first()
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    earliest = latest = None
    if order.current_stage != "delivered":
        earliest, latest = order.eta_window()
    return OrderTrackResponse(
        order_number=order.order_number,
        stage=order.current_stage,
        current_location=order.current_location,
        eta_earliest=earliest,
        eta_latest=latest,
        stalled=order.is_stalled(),
        delivery_method=order.delivery_method,
        destination_city=order.destination_city,
    )

# to create  unique query_id 
@app.post("/queries", status_code = 202,response_model=QueryResponse)
def create_query(request: SupportRequest,db = Depends(get_db)):
    query = SupportQuery(question= request.question,
                         customer_email=request.customer_email)
    db.add(query)
    db.commit()
    db.refresh(query)

    # ** Find ALL current approved knowledge versions (source of truth), each
    #    labeled with its topic so the AI grounds in the RIGHT policy.
    #    (The old code used .first() which picked ONE arbitrary policy once
    #    several existed -- answers could come from the wrong document.)
    current_policies = db.execute(
        select(KnowledgeAnswerVersion, KnowledgeAnswer.canonical_question)
        .join(
            KnowledgeAnswer,
            KnowledgeAnswerVersion.knowledge_answer_id == KnowledgeAnswer.id,
        )
        .where(KnowledgeAnswerVersion.is_current.is_(True))
        .order_by(KnowledgeAnswer.canonical_question)
    ).all()

    if current_policies:
        # schema allows one FK per query: store the first as the primary one
        query.knowledge_version_id = current_policies[0][0].id
        db.commit()
        db.refresh(query)

    # One combined, labeled policy block keeps the single AI call fast.
    policy_context = "\n\n".join(
        f"Topic - {canonical_question}:\n{version.answer_text}"
        for version, canonical_question in current_policies
    ) or None

    # !Generate an intent-aware Ai response, grounded in the knowledge base
    # Map typed AI errors to proper HTTP statuses (instead of raw 500s)
    situation_parts = []

    # ** Phase 1: LIVE order facts (location algo) - real data beats guessing.
    #    Scoped to the requester's email: other people's orders never load.
    order_ref = request.order_number or _extract_order_number(request.question)
    if order_ref:
        order = db.execute(
            select(Order).where(
                Order.order_number == order_ref.strip().upper(),
                Order.customer_email == request.customer_email,
            )
        ).scalars().first()
        if order:
            situation_parts.append(order.tracking_facts())

    if request.purchase_date:
        days_ago = (date.today() - request.purchase_date).days
        situation_parts.append(
            f"purchased {days_ago} days ago (on {request.purchase_date.isoformat()})"
        )
    if request.order_status:
        situation_parts.append(f"order status: {request.order_status}")
    situation = "; ".join(situation_parts) or None

    try:
        result = answer_customer_question(
            query.question,
            policy_context=policy_context,
            situation=situation,
        )
    except AIRateLimitError:
        raise HTTPException(status_code=429, detail="AI is busy, please try again shortly")
    except AITimeoutError:
        raise HTTPException(status_code=504, detail="AI timed out, please try again")
    except (AIContentBlockedError, AIProviderError):
        raise HTTPException(status_code=502, detail="Could not generate an answer right now")

    generate_text = result.answer_text
    query.intent = result.intent
    db.commit()
    db.refresh(query)

    # **create object of Suppoert_Answer to create an answer linked with saved query
    answer = Support_Answer(query_id = query.id, answer_text= generate_text)
    db.add(answer)
    db.commit()
    db.refresh(answer)

    return {
    "query_id": query.id,
    "question": query.question,
    "status": query.status,
    "created_at": query.created_at,
    "intent": query.intent
}

@app.get("/queries/{query_id}",response_model=QueryResponse)
def get_query(query_id:UUID,db = Depends(get_db)):
    result = db.execute(select(SupportQuery).where(SupportQuery.id == query_id))
    query = result.scalar_one_or_none()
    if query is None:
        raise HTTPException(status_code=404,detail="Query not found")
    return {"query_id":query.id,
            "question":query.question,
            "status":query.status,
            "created_at":query.created_at,
            "intent":query.intent
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
            "created_at":query.created_at,
            "intent":query.intent
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


    
    

    
