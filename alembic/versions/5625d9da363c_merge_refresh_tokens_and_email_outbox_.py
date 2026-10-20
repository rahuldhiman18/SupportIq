"""merge refresh_tokens and email_outbox branches

Revision ID: 5625d9da363c
Revises: 7d3263169a3a, a1b2c3d4e5f6
Create Date: 2026-10-20 12:02:51.727485

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5625d9da363c'
down_revision: Union[str, Sequence[str], None] = ('7d3263169a3a', 'a1b2c3d4e5f6')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
