from uuid import UUID, uuid4
from sqlalchemy import String
from sqlalchemy.orm import Mapped , mapped_column
from .database import Base
from datetime import datetime

class SupportQuery(Base):
    __tablename__ = "support_queries"

    id: Mapped[UUID] = mapped_column(primary_key=True,default = uuid4)
    question: Mapped[str] = mapped_column()
    customer_email: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default = datetime.utcnow)
    status: Mapped[str] = mapped_column(default="pending")