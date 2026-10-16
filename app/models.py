from uuid import UUID, uuid4
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String ,CheckConstraint,Index,UniqueConstraint
from sqlalchemy.orm import Mapped , mapped_column, relationship
from .database import Base
from datetime import datetime, timezone, timedelta, date
import threading


# --- Step 2.5: strictly-increasing clock (bug-hunt fix) ----------------------
# Windows serves datetime.now() in ~15.6ms timer ticks, so back-to-back
# inserts (e.g. two PATCH /queries/{id}/answer calls) can land on the SAME
# microsecond. With ORDER BY created_at DESC that is a tie, and the "older"
# answer could surface as latest. A process-wide high-water mark keeps
# timestamps unique by nudging stalled reads forward 1us (error stays well
# under a millisecond). Multi-process deployments can still theoretically
# collide across processes; that window is sub-microsecond and worth a DB
# sequence only if it ever shows up in practice.
_ts_lock = threading.Lock()
_ts_high_water: datetime | None = None


def utcnow_unique() -> datetime:
    """now(UTC) that never returns the same value twice in this process."""
    global _ts_high_water
    now = datetime.now(timezone.utc)
    with _ts_lock:
        if _ts_high_water is not None and now <= _ts_high_water:
            now = _ts_high_water + timedelta(microseconds=1)
        _ts_high_water = now
    return now


# --- Step 2: priority-with-aging queue (DB-persisted, crash-safe) --------------
# Wide bands you approved: 0/100/200/300/400, 1 point per 30s.
# Lower score = served first. Waiting reduces score so old normals
# eventually outrank new urgents (no starvation).
AGING_SECONDS_PER_POINT = 30.0

PRIORITY_CRITICAL = 0      # payment_issues or stalled order (verified server-side)
PRIORITY_HIGH = 100        # order_tracking / delivery_eta / wrong_address / wrong_purchase
PRIORITY_NORMAL = 200      # eligibility / timing / refund_status
PRIORITY_LOW = 300         # how_to_request / password_reset / account_management
PRIORITY_LOWEST = 400      # out_of_scope / unknown / default


def compute_base_priority(intent: str | None,
                          stalled: bool = False) -> int:
    """Server-derived base weight. Never trust a client-sent urgency flag.

    Stalled carrier (3+ biz days, computed from Order.is_stalled) and
    payment_issues jump to 0. Everything else maps from intent.
    """
    if stalled or intent == "payment_issues":
        return PRIORITY_CRITICAL
    if intent in ("order_tracking", "delivery_eta",
                  "wrong_address", "wrong_purchase"):
        return PRIORITY_HIGH
    if intent in ("eligibility", "timing", "refund_status"):
        return PRIORITY_NORMAL
    if intent in ("how_to_request", "password_reset", "account_management"):
        return PRIORITY_LOW
    return PRIORITY_LOWEST


def effective_score(base_priority: int, created_at: datetime,
                    now: datetime | None = None) -> float:
    """Lower = serve first. wait_seconds / 30 aged off the base weight."""
    created = created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    at = now or datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    waited = max(0.0, (at - created).total_seconds())
    return base_priority - waited / AGING_SECONDS_PER_POINT


class SupportQuery(Base):
    __tablename__ = "support_queries"

    id: Mapped[UUID] = mapped_column(primary_key=True,default = uuid4)
    question: Mapped[str] = mapped_column()
    customer_email: Mapped[str] = mapped_column()
    # utcnow_unique: equal created_at values would tie effective_score and
    # make queue claim order non-deterministic.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=utcnow_unique)
    status: Mapped[str] = mapped_column(default="pending")
    intent: Mapped[str | None] = mapped_column(nullable=True)  # detected customer intent (eligibility/timing/how_to_request/refund_status/order_tracking/delivery_eta/password_reset/account_management/payment_issues/wrong_purchase/wrong_address/out_of_scope)
    knowledge_version_id:Mapped[UUID | None] = mapped_column(ForeignKey("knowledge_answer_versions.id"),nullable=True)
    # Queue durability: survives restarts; workers claim via FOR UPDATE SKIP LOCKED.
    priority: Mapped[int] = mapped_column(Integer, default=PRIORITY_LOWEST,
                                          server_default="400", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0,
                                          server_default="0", nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                       nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                           nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    # Step 3b request context: queued mode persists the SupportRequest extras
    # so the worker rebuilds the identical prompt (order facts, purchase-date
    # math, order-status hint) after the HTTP request is long gone.
    order_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    purchase_date: Mapped[date | None] = mapped_column(nullable=True)
    order_status: Mapped[str | None] = mapped_column(String, nullable=True)
    __table_args__ = (
        Index("ix_support_queries_queue_claim", "status", "next_retry_at"),
    )


class Support_Answer(Base):
    __tablename__ = "support_answers"

    id: Mapped[UUID] = mapped_column(primary_key=True,default=uuid4)
    query_id: Mapped[UUID] = mapped_column(ForeignKey("support_queries.id"),nullable=False, index=True)
    answer_text:Mapped[str] = mapped_column()
    is_selected: Mapped[bool] = mapped_column(default=False)
    # utcnow_unique: GET /queries/{id}/answer orders by created_at DESC; the
    # tick-quantized wall clock handed two rapid answers the SAME timestamp
    # and the older answer could win the tie. Unique timestamps fix that
    # without a schema change.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone =True),default=utcnow_unique)


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
    


