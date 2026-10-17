"""bootstrap the legacy tables the rest of the chain assumes exist

Revision ID: 0000aa11bb22
Revises: None
Create Date: 2026-10-17 00:00:00.000000

Chapter zero of our migration history. support_queries, support_answers,
orders, order_events and users were originally created by
`Base.metadata.create_all` (create_table.py), and every later migration was
authored on top of that database -- so the log began with "add column to
support_queries" and could never replay on a FRESH database (teammate
checkout, CI, production): even revision 2826b6baca68 failed there.

This revision recreates those five tables in their ORIGINAL pre-chain
shapes so a from-zero `alembic upgrade head` works end to end:
- support_queries.created_at stays naive here; c4d8e2f91a05 converts it.
- intent / queue columns / context columns are absent here; their
  respective revisions add them.
- support_answers has NO query_id index here; f4c9d2a71b83 adds it.
- orders / order_events / users are untouched by any migration, so their
  current shapes ARE their original shapes (verified against git f35e090).

Existing databases already contain these tables; alembic only ever runs
unapplied revisions, so live databases are unaffected by this file.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0000aa11bb22'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema. Original (create_all-era) shapes, pre-chain."""
    op.create_table(
        'support_queries',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('question', sa.String(), nullable=False),
        sa.Column('customer_email', sa.String(), nullable=False),
        # naive timestamp ON PURPOSE: c4d8e2f91a05 converts it with
        # `AT TIME ZONE 'UTC'` exactly as it did for the create_all database
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'support_answers',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('query_id', sa.Uuid(), nullable=False),
        sa.Column('answer_text', sa.String(), nullable=False),
        sa.Column('is_selected', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['query_id'], ['support_queries.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'orders',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('order_number', sa.String(), nullable=False),
        sa.Column('customer_email', sa.String(), nullable=False),
        sa.Column('delivery_method', sa.String(), nullable=False),
        sa.Column('destination_city', sa.String(), nullable=False),
        sa.Column('carrier', sa.String(), nullable=True),
        sa.Column('placed_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_orders_order_number', 'orders', ['order_number'],
                    unique=True)
    op.create_index('ix_orders_customer_email', 'orders', ['customer_email'],
                    unique=False)
    op.create_table(
        'order_events',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('order_id', sa.Uuid(), nullable=False),
        sa.Column('stage', sa.String(), nullable=False),
        sa.Column('location', sa.String(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_order_events_order_id', 'order_events', ['order_id'],
                    unique=False)
    op.create_table(
        'users',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('username', sa.String(), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('password_hash', sa.String(), nullable=False),
        sa.Column('password_salt', sa.String(), nullable=False),
        sa.Column('role', sa.String(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_users_username', 'users', ['username'], unique=True)
    op.create_index('ix_users_email', 'users', ['email'], unique=True)


def downgrade() -> None:
    """Downgrade schema (only meaningful on a from-zero replayed database)."""
    op.drop_index('ix_users_email', table_name='users')
    op.drop_index('ix_users_username', table_name='users')
    op.drop_table('users')
    op.drop_index('ix_order_events_order_id', table_name='order_events')
    op.drop_table('order_events')
    op.drop_index('ix_orders_customer_email', table_name='orders')
    op.drop_index('ix_orders_order_number', table_name='orders')
    op.drop_table('orders')
    op.drop_table('support_answers')
    op.drop_table('support_queries')