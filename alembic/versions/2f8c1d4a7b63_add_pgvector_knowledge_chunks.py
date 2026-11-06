"""Add pgvector support and knowledge chunk storage.

Revision ID: 2f8c1d4a7b63
Revises: 6a7b8c9d0e1f
Create Date: 2026-11-04 22:21:32
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


revision: str = "2f8c1d4a7b63"
down_revision: Union[str, Sequence[str], None] = "6a7b8c9d0e1f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("knowledge_answer_id", sa.Uuid(), nullable=False),
        sa.Column("knowledge_version_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "chunk_index >= 0",
            name="ck_knowledge_chunks_index",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_knowledge_chunks_content_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_answer_id"],
            ["knowledge_answers.id"],
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_version_id"],
            ["knowledge_answer_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "knowledge_version_id",
            "chunk_index",
            name="uq_knowledge_chunks_version_index",
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_active_version",
        "knowledge_chunks",
        ["knowledge_version_id", "is_active"],
    )
    op.create_index(
        "ix_knowledge_chunks_answer_version",
        "knowledge_chunks",
        ["knowledge_answer_id", "knowledge_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_chunks_answer_version",
        table_name="knowledge_chunks",
    )
    op.drop_index(
        "ix_knowledge_chunks_active_version",
        table_name="knowledge_chunks",
    )
    op.drop_table("knowledge_chunks")
