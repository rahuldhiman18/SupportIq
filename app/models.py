from uuid import UUID, uuid4
from sqlalchemy import Boolean, DateTime, ForeignKey, String ,CheckConstraint,Index,UniqueConstraint
from sqlalchemy.orm import Mapped , mapped_column, relationship
from .database import Base
from datetime import datetime, timezone, timedelta


class SupportQuery(Base):
    __tablename__ = "support_queries"

    id: Mapped[UUID] = mapped_column(primary_key=True,default = uuid4)
    question: Mapped[str] = mapped_column()
    customer_email: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    status: Mapped[str] = mapped_column(default="pending")
    intent: Mapped[str | None] = mapped_column(nullable=True)  # detected customer intent (eligibility/timing/how_to_request/refund_status/order_tracking/delivery_eta/password_reset/account_management/payment_issues/wrong_purchase/wrong_address/out_of_scope)
    knowledge_version_id:Mapped[UUID | None] = mapped_column(ForeignKey("knowledge_answer_versions.id"),nullable=True)


class Support_Answer(Base):
    __tablename__ = "support_answers"

    id: Mapped[UUID] = mapped_column(primary_key=True,default=uuid4)
    query_id: Mapped[UUID] = mapped_column(ForeignKey("support_queries.id"),nullable=False)
    answer_text:Mapped[str] = mapped_column()
    is_selected: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone =True),default=lambda:datetime.now(timezone.utc))


class KnowledgeAnswer(Base):
    __tablename__= "knowledge_answers"
    id: Mapped[UUID] = mapped_column(primary_key=True,default=uuid4)
    canonical_question: Mapped[str] = mapped_column(String,nullable=False)
    is_active: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone =True),default=lambda:datetime.now(timezone.utc))
    source_url: Mapped[str] = mapped_column(String, nullable=False)

class KnowledgeAnswerVersion(Base):
    __tablename__="knowledge_answer_versions"
    id: Mapped[UUID] = mapped_column(primary_key=True,default=uuid4)
    knowledge_answer_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_answers.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(nullable=False)
    answer_text: Mapped[str] = mapped_column(String,nullable=False)
    status: Mapped[str] = mapped_column(String,default="draft",nullable=False)
    is_current:Mapped[bool] = mapped_column(Boolean,default=False,nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String , nullable=True)
    approved_at: Mapped[datetime | None]= mapped_column(DateTime(timezone =True),default=lambda:datetime.now(timezone.utc),nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone =True),default=lambda:datetime.now(timezone.utc),nullable=False)
    __table_args__ =(CheckConstraint(
        "status IN ('draft','approved','rejected')",
        name="ck_knowledge_answer_versions_status",
    ),
    CheckConstraint(
        "is_current IS False OR status = 'approved'",
        name="ck_knowledge_answer_versions_current_approved",
    ),
    CheckConstraint(
    "status != 'approved' OR "
    "(approved_by IS NOT NULL AND approved_at IS NOT NULL)",
    name="ck_knowledge_answer_versions_approval_metadata",
    ),
    UniqueConstraint("knowledge_answer_id",
    "version_number",
     name ="uq_knowledge_answer_versions_item_number"
    ),
    Index("uq_knowledge_answer_versions_one_current",
              knowledge_answer_id,unique=True,
              postgresql_where = ((is_current.is_(True)))
    ),
    )


# --- Phase 1: order tracking (location + ETA algo) --------------------------
# The MATH lives here, deterministic and unit-testable; the AI only narrates
# these facts. SLAs match the seeded SHIPPING_POLICY text.

DELIVERY_SLA_BUSINESS_DAYS = {"standard": (3, 5), "express": (1, 2)}
STALL_BUSINESS_DAYS = 3          # no new scan for this long = flag it
STAGE_ORDER = ["placed", "packed", "shipped", "out_for_delivery", "delivered"]


def _as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; treat them as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _friendly_day(value: datetime) -> str:
    """'Mon, Oct 19' -- reads naturally when the AI quotes it verbatim."""
    return _as_utc(value).strftime("%a, %b %d").replace(" 0", " ")


def add_business_days(start: datetime, days: int) -> datetime:
    """Add N business days (Mon-Fri only; holidays calendar = later)."""
    current = _as_utc(start)
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def business_days_between(start: datetime, end: datetime) -> int:
    """How many business days elapsed from start to end."""
    start, end = _as_utc(start), _as_utc(end)
    if end <= start:
        return 0
    count, day = 0, start.date()
    while day < end.date():
        day += timedelta(days=1)
        if day.weekday() < 5:
            count += 1
    return count


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    order_number: Mapped[str] = mapped_column(String, unique=True, index=True)
    customer_email: Mapped[str] = mapped_column(String, index=True)
    delivery_method: Mapped[str] = mapped_column(String, default="standard")
    destination_city: Mapped[str] = mapped_column(String, default="")
    carrier: Mapped[str | None] = mapped_column(String, nullable=True)
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    events: Mapped[list["OrderEvent"]] = relationship(
        back_populates="order",
        order_by="OrderEvent.occurred_at",
        cascade="all, delete-orphan",
    )

    @property
    def last_event(self) -> "OrderEvent | None":
        return self.events[-1] if self.events else None

    @property
    def current_stage(self) -> str:
        return self.last_event.stage if self.last_event else "placed"

    @property
    def current_location(self) -> str:
        return self.last_event.location if self.last_event else ""

    def eta_window(self) -> tuple[datetime, datetime]:
        """(earliest, latest) delivery estimate in business days from dispatch."""
        low, high = DELIVERY_SLA_BUSINESS_DAYS.get(
            self.delivery_method, (3, 5))
        shipped = next(
            (e for e in reversed(self.events)
             if e.stage in ("shipped", "out_for_delivery", "delivered")),
            None,
        )
        # not shipped yet -> assume the 1-business-day dispatch SLA applies
        dispatch = (_as_utc(shipped.occurred_at) if shipped
                    else add_business_days(_as_utc(self.placed_at), 1))
        return add_business_days(dispatch, low), add_business_days(dispatch, high)

    def is_stalled(self) -> bool:
        """True when the carrier has not scanned the parcel for 3+ biz days."""
        if self.current_stage == "delivered" or self.last_event is None:
            return False
        return business_days_between(
            self.last_event.occurred_at, datetime.now(timezone.utc)
        ) >= STALL_BUSINESS_DAYS

    def tracking_facts(self) -> str:
        """Prompt-ready one-liner the AI narrates (real data, no guessing)."""
        head = (f"LIVE ORDER FACTS: #{self.order_number} "
                f"({self.delivery_method} shipping to {self.destination_city})")
        last = self.last_event
        if last is None:
            return f"{head}; stage=placed; no carrier scans yet"
        if last.stage == "delivered":
            delivered_on = _friendly_day(last.occurred_at)
            return f"{head}; stage=delivered on {delivered_on}"
        earliest, latest = self.eta_window()
        age_h = (datetime.now(timezone.utc) - _as_utc(last.occurred_at)
                 ).total_seconds() / 3600
        last_scan = ("today" if age_h < 24 else
                     f"{int(age_h // 24)} days ago")
        stalled = ("yes - open a carrier investigation"
                   if self.is_stalled() else "no")
        return (
            f"{head}; stage={last.stage}; last scan at {last.location} "
            f"({_friendly_day(last.occurred_at)}, {last_scan}); "
            f"ETA between {_friendly_day(earliest)} "
            f"and {_friendly_day(latest)}; stalled={stalled}"
        )


class OrderEvent(Base):
    __tablename__ = "order_events"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(
        ForeignKey("orders.id"), nullable=False, index=True)
    # placed / packed / shipped / out_for_delivery / delivered
    stage: Mapped[str] = mapped_column(String)
    location: Mapped[str] = mapped_column(String, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    order: Mapped["Order"] = relationship(back_populates="events")


class User(Base):
    """Login account for JWT auth (Phase B). Password stored as PBKDF2 hash."""
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    password_salt: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, default="customer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    


