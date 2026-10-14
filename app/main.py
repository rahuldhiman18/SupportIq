import base64
import hashlib
import hmac
import json as _json
import os
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, date
from uuid import UUID
from enum import Enum
from typing import Literal

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app.ai_service import (
    answer_customer_question,
    AITimeoutError,
    AIRateLimitError,
    AIProviderError,
    AIContentBlockedError,
)
from .database import SessionLocal
from .models import SupportQuery , Support_Answer , KnowledgeAnswer , KnowledgeAnswerVersion , Order , OrderEvent , User
from sqlalchemy import select, desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

app = FastAPI(title = "SupportIq")

# ** Phase A auth: admin key guards the critical write endpoints,
#    public chat stays open but IP-throttled (Gemini calls cost money)
api_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)

# Dev fallback ONLY: tests/seed scripts run without a provisioned key.
_TEST_ADMIN_KEY = "test-admin-key"


def _expected_admin_key() -> str | None:
    return os.getenv("ADMIN_API_KEY") or _TEST_ADMIN_KEY


def require_admin_key(key: str | None = Depends(api_key_header)) -> None:
    expected = _expected_admin_key()
    if not key or not expected or not secrets.compare_digest(key, expected):
        raise HTTPException(status_code=401,
                            detail="Invalid or missing admin key")


# ** JWT login (Phase B): stdlib-only HS256 + PBKDF2, no new dependencies.
#    Tokens are OPTIONAL on existing routes (valid token scopes data to the
#    logged-in email; missing token keeps the current email-param behavior).
_JWT_ALGO = "HS256"
_ACCESS_TOKEN_MINUTES = 15
_TEST_JWT_SECRET = "test-jwt-secret-for-offline-tests"

bearer_scheme = HTTPBearer(auto_error=False)


def _jwt_secret() -> str:
    return os.getenv("JWT_SECRET_KEY") or _TEST_JWT_SECRET


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def issue_access_token(user: "User") -> str:
    header = _b64url(_json.dumps({"alg": _JWT_ALGO, "typ": "JWT"}).encode())
    payload = _b64url(_json.dumps({
        "sub": user.username,
        "email": user.email,
        "role": user.role,
        "exp": int(time.time()) + _ACCESS_TOKEN_MINUTES * 60,
    }).encode())
    sig = _b64url(hmac.new(_jwt_secret().encode(), f"{header}.{payload}".encode(),
                           hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def verify_access_token(token: str) -> dict:
    try:
        header, payload, sig = token.split(".")
        expected = _b64url(hmac.new(
            _jwt_secret().encode(), f"{header}.{payload}".encode(),
            hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(status_code=401, detail="Invalid token")
        claims = _json.loads(_b64url_decode(payload))
    except (ValueError, HTTPException):
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    if claims.get("exp", 0) < int(time.time()):
        raise HTTPException(status_code=401, detail="Token expired")
    return claims


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """PBKDF2-SHA256 (210k rounds) -> (salt_hex, hash_hex). No new dependency."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt), 210_000)
    return salt, digest.hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, digest_hex = hash_password(password, salt)
    return hmac.compare_digest(digest_hex, expected_hash)


def current_identity(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict | None:
    """Logged-in claims, or None for anonymous callers (routes stay open)."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        return None
    return verify_access_token(credentials.credentials)


class LoginRequest(BaseModel):
    username_or_email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int = _ACCESS_TOKEN_MINUTES
    username: str
    email: str
    role: str


# Tiny in-memory sliding-window limiter: 30 POST /queries per IP per minute.
# No new dependency (slowapi) for Phase A; swap for Redis in Phase C.
_RATE_LIMIT = 30
_RATE_WINDOW_SECONDS = 60.0
_rate_hits: dict[str, list[float]] = defaultdict(list)


def throttle_public_chat(request: Request) -> None:
    now = time.monotonic()
    ip = request.client.host if request.client else "unknown"
    hits = [t for t in _rate_hits[ip] if now - t < _RATE_WINDOW_SECONDS]
    _rate_hits[ip] = hits
    if len(hits) >= _RATE_LIMIT:
        raise HTTPException(status_code=429,
                            detail="Too many requests - please wait a minute")
    hits.append(now)

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
def create_order(request: OrderCreate, db: Session = Depends(get_db),
                 _admin: None = Depends(require_admin_key)):
    order = Order(
        order_number=request.order_number.strip().upper(),
        customer_email=request.customer_email,
        delivery_method=request.delivery_method,
        destination_city=request.destination_city,
        carrier=request.carrier,
    )
    db.add(order)
    try:
        db.flush()
        for event in request.events:
            db.add(OrderEvent(order_id=order.id, stage=event.stage,
                              location=event.location,
                              occurred_at=event.occurred_at))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Order number already exists")
    return {"order_number": order.order_number, "status": "created",
            "events": len(request.events)}


@app.post("/auth/login", response_model=LoginResponse)
def login(request: LoginRequest, http_request: Request,
          db: Session = Depends(get_db),
          _throttle: None = Depends(throttle_public_chat)):
    # Same throttle as public chat: brute-force protection, stdlib only.
    user = db.execute(
        select(User).where(
            (User.username == request.username_or_email)
            | (User.email == request.username_or_email)
        )
    ).scalars().first()
    if user is None or not user.is_active or not verify_password(
            request.password, user.password_salt, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return LoginResponse(
        access_token=issue_access_token(user),
        username=user.username,
        email=user.email,
        role=user.role,
    )


@app.get("/orders/{order_number}", response_model=OrderTrackResponse)
def get_order(order_number: str, email: str | None = None,
              db: Session = Depends(get_db),
              identity: dict | None = Depends(current_identity)):
    # Token wins over ?email: a logged-in user can only see their own orders.
    # Anonymous callers must pass ?email= (existing behavior, still open).
    effective_email = (identity or {}).get("email") or email
    if not effective_email:
        raise HTTPException(status_code=401,
                            detail="Login or pass ?email= to view this order")
    # Scoped by email: wrong/missing email = 404, never leak other orders
    order = db.execute(
        select(Order).where(
            Order.order_number == order_number.strip().upper(),
            Order.customer_email == effective_email,
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
def create_query(request: SupportRequest, http_request: Request,
                 db = Depends(get_db),
                 _throttle: None = Depends(throttle_public_chat),
                 identity: dict | None = Depends(current_identity)):
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


    
    

    
