from uuid import UUID, uuid4
from sqlalchemy import Boolean, DateTime, ForeignKey, String ,CheckConstraint,Index,UniqueConstraint
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
    


