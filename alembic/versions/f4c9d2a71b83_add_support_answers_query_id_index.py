"""index support_answers.query_id for the answer-poll hot path

Revision ID: f4c9d2a71b83
Revises: e7f8a9b0c1d2
Create Date: 2026-10-16 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4c9d2a71b83'
down_revision: Union[str, Sequence[str], None] = 'e7f8a9b0c1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    GET /queries/{id}/answer is the hottest READ in queued mode: clients poll
    it per query, and the worst case (poll before the answer exists) scans
    everything to find nothing. PostgreSQL does NOT auto-index foreign keys,
    so without this index the only access path is a sequential scan over
    support_answers -- including dead tuples left behind by cleanup, as the
    live EXPLAIN demo showed (Seq Scan, "Disabled: true", 37 rows removed
    for 0 returned).

    Production note: on a table that already holds real traffic, create this
    with CREATE INDEX CONCURRENTLY (alembic autocommit block) to avoid
    blocking answer inserts; the table is empty today, so plain CREATE INDEX
    is instant here.
    """
    op.create_index('ix_support_answers_query_id', 'support_answers',
                    ['query_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_support_answers_query_id', table_name='support_answers')