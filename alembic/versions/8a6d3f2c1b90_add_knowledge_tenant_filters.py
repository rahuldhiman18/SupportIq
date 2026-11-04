"""Add tenant filters to knowledge and chunks.

Revision ID: 8a6d3f2c1b90
Revises: 2f8c1d4a7b63
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8a6d3f2c1b90"
down_revision: Union[str, Sequence[str], None] = "2f8c1d4a7b63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "knowledge_answers",
        sa.Column("tenant_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "knowledge_chunks",
        sa.Column("tenant_id", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_knowledge_answers_tenant",
        "knowledge_answers",
        ["tenant_id"],
    )
    op.create_index(
        "ix_knowledge_chunks_tenant_active",
        "knowledge_chunks",
        ["tenant_id", "is_active"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_chunks_tenant_active",
        table_name="knowledge_chunks",
    )
    op.drop_index(
        "ix_knowledge_answers_tenant",
        table_name="knowledge_answers",
    )
    op.drop_column("knowledge_chunks", "tenant_id")
    op.drop_column("knowledge_answers", "tenant_id")
