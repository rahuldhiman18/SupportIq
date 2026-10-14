"""make support_queries.created_at timezone-aware

Revision ID: c4d8e2f91a05
Revises: a1c9e7d3f5b2
Create Date: 2026-10-14 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d8e2f91a05'
down_revision: Union[str, Sequence[str], None] = 'a1c9e7d3f5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    support_queries was created via Base.metadata.create_all, so created_at
    ended up as a naive ``timestamp without time zone`` (written in UTC by
    datetime.utcnow). Convert it to timestamptz, treating every existing
    naive value as UTC so no timestamps shift.
    """
    op.alter_column(
        'support_queries',
        'created_at',
        type_=sa.DateTime(timezone=True),
        existing_type=sa.DateTime(),
        existing_nullable=False,
        postgresql_using="created_at AT TIME ZONE 'UTC'",
    )


def downgrade() -> None:
    """Downgrade schema.

    Drop the offset, keeping the UTC wall time (inverse of upgrade).
    """
    op.alter_column(
        'support_queries',
        'created_at',
        type_=sa.DateTime(),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        postgresql_using="created_at AT TIME ZONE 'UTC'",
    )