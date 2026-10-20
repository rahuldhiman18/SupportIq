"""add email_outbox + feedback tables

Revision ID: a1b2c3d4e5f6
Revises: f4c9d2a71b83
Create Date: 2026-10-19 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'f4c9d2a71b83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'email_outbox',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('idempotency_key', sa.String(), nullable=False),
        sa.Column('to', sa.String(), nullable=False),
        sa.Column('subject', sa.String(), nullable=False),
        sa.Column('body_text', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key', name='uq_email_outbox_idempotency_key'),
    )
    op.create_index('ix_email_outbox_status', 'email_outbox', ['status'])
    op.create_index('ix_email_outbox_idempotency_key', 'email_outbox', ['idempotency_key'], unique=True)

    op.create_table(
        'feedback',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('query_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('rating', sa.Integer(), nullable=False),
        sa.Column('comment', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['query_id'], ['support_queries.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint('rating >= 1 AND rating <= 5', name='ck_feedback_rating_range'),
    )
    op.create_index('ix_feedback_query_id', 'feedback', ['query_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_feedback_query_id', table_name='feedback')
    op.drop_table('feedback')
    op.drop_index('ix_email_outbox_idempotency_key', table_name='email_outbox')
    op.drop_index('ix_email_outbox_status', table_name='email_outbox')
    op.drop_table('email_outbox')
