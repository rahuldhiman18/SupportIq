"""add request-context columns so queued workers can rebuild the prompt

Revision ID: e7f8a9b0c1d2
Revises: d5e6f7a8b9c0
Create Date: 2026-10-15 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7f8a9b0c1d2'
down_revision: Union[str, Sequence[str], None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Step 3b enqueues without calling the AI inside the HTTP request, so the
    worker must be able to rebuild the exact prompt later. These three
    nullable columns persist the SupportRequest extras that inline mode used
    transiently (order facts, purchase-date math, order-status hint).
    """
    op.add_column('support_queries',
                  sa.Column('order_number', sa.String(length=32),
                            nullable=True))
    op.add_column('support_queries',
                  sa.Column('purchase_date', sa.Date(), nullable=True))
    op.add_column('support_queries',
                  sa.Column('order_status', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('support_queries', 'order_status')
    op.drop_column('support_queries', 'purchase_date')
    op.drop_column('support_queries', 'order_number')