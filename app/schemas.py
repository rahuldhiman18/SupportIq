"""Request/response schemas: pure data, no logic.

Moved verbatim from app/main.py (split task). Routes import from here.
"""
from datetime import datetime, date
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from .security import _ACCESS_TOKEN_MINUTES, _DEFAULT_REFRESH_MINUTES


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
    # Opaque refresh token (rotation + family revoke). Additive: old clients
    # that only read access_token keep working.
    refresh_token: str
    refresh_expires_in_minutes: int = _DEFAULT_REFRESH_MINUTES


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=200)


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int = _ACCESS_TOKEN_MINUTES
    refresh_token: str
    refresh_expires_in_minutes: int = _DEFAULT_REFRESH_MINUTES


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=200)


#**structure what we get from customer**

class SupportRequest(BaseModel):
    # Shape check at the edge: garbage emails used to be stored and then used
    # for identity scoping / ownership checks. RFC-5322-lite, stdlib re only.
    customer_email : str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
                                 max_length=254)
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


class HumanAnswerRequest(BaseModel):
    # Admin/human pickup: agent writes the authoritative reply for a query the
    # AI could not answer within its retry budget (see app/worker.py).
    answer_text: str = Field(min_length=1, max_length=5000)


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
