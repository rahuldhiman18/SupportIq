from uuid import UUID, uuid4
from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped , mapped_column
from .database import Base
from datetime import datetime, timezone

class SupportQuery(Base):
    __tablename__ = "support_queries"

    id: Mapped[UUID] = mapped_column(primary_key=True,default = uuid4)
    question: Mapped[str] = mapped_column()
    customer_email: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default = datetime.utcnow)
    status: Mapped[str] = mapped_column(default="pending")


class Support_Answer(Base):
    __tablename__ = "support_answers"

    id: Mapped[UUID] = mapped_column(primary_key=True,default=uuid4)
    query_id: Mapped[UUID] = mapped_column(ForeignKey("support_queries.id"),nullable=False)
    answer_text:Mapped[str] = mapped_column()
    is_selected: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone =True),default=lambda:datetime.now(timezone.utc))