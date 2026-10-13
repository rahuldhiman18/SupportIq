"""add support_queries intent column

Revision ID: a1c9e7d3f5b2
Revises: 81aacf128745
Create Date: 2026-10-13 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c9e7d3f5b2'
down_revision: Union[str, Sequence[str], None] = '81aacf128745'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('support_queries', sa.Column('intent', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('support_queries', 'intent')