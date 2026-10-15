"""add support_queries queue columns for priority-with-aging workers

Revision ID: d5e6f7a8b9c0
Revises: c4d8e2f91a05
Create Date: 2026-10-15 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, Sequence[str], None] = 'c4d8e2f91a05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('support_queries', sa.Column('priority', sa.Integer(),
                                               server_default='400',
                                               nullable=False))
    op.add_column('support_queries', sa.Column('attempts', sa.Integer(),
                                               server_default='0',
                                               nullable=False))
    op.add_column('support_queries', sa.Column('locked_at',
                                               sa.DateTime(timezone=True),
                                               nullable=True))
    op.add_column('support_queries', sa.Column('next_retry_at',
                                               sa.DateTime(timezone=True),
                                               nullable=True))
    op.add_column('support_queries', sa.Column('last_error', sa.String(),
                                               nullable=True))
    op.create_index('ix_support_queries_queue_claim', 'support_queries',
                    ['status', 'next_retry_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_support_queries_queue_claim',
                  table_name='support_queries')
    op.drop_column('support_queries', 'last_error')
    op.drop_column('support_queries', 'next_retry_at')
    op.drop_column('support_queries', 'locked_at')
    op.drop_column('support_queries', 'attempts')
    op.drop_column('support_queries', 'priority')
