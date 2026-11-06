"""Add a cosine ANN index for tenant-scoped knowledge retrieval.

Revision ID: b3c4d5e6f7a8
Revises: 9c4e7a1d2b58
"""

from typing import Sequence, Union

from alembic import op


revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "9c4e7a1d2b58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE knowledge_chunks
        ALTER COLUMN embedding TYPE vector(768)
        USING embedding::vector(768)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_embedding_cosine
        ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ix_knowledge_chunks_embedding_cosine"
    )
