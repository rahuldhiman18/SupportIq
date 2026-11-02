"""add durable email outbox delivery leases

Revision ID: 6a7b8c9d0e1f
Revises: 5625d9da363c
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6a7b8c9d0e1f"
down_revision: Union[str, Sequence[str], None] = "5625d9da363c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "email_outbox",
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "email_outbox",
        sa.Column("locked_by", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_email_outbox_locked_at", "email_outbox", ["locked_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_email_outbox_locked_at", table_name="email_outbox")
    op.drop_column("email_outbox", "locked_by")
    op.drop_column("email_outbox", "locked_at")
