from sqlalchemy import String
from sqlalchemy.orm import Mapped , mapped_column
from .database import Base

class SupportQuery(Base):
    __tablename__ = "support_queries"

    id: Mapped[int] = mapped_column(primary_key=True)
    question: Mapped[str] = mapped_column()
    customer_email: Mapped[str] = mapped_column()
    status: Mapped[str] = mapped_column(default="pending")