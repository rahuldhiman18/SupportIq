"""Harden account JWT state and knowledge tenant consistency.

Revision ID: 9c4e7a1d2b58
Revises: 8a6d3f2c1b90
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9c4e7a1d2b58"
down_revision: Union[str, Sequence[str], None] = "8a6d3f2c1b90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("tenant_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
    op.add_column(
        "support_queries",
        sa.Column("tenant_id", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_support_queries_tenant_id",
        "support_queries",
        ["tenant_id"],
    )

    op.execute(
        """
        UPDATE knowledge_chunks c
        SET tenant_id = k.tenant_id
        FROM knowledge_answers k
        WHERE k.id = c.knowledge_answer_id
          AND c.tenant_id IS DISTINCT FROM k.tenant_id
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_knowledge_chunk_tenant()
        RETURNS trigger AS $$
        BEGIN
            SELECT tenant_id INTO NEW.tenant_id
            FROM knowledge_answers
            WHERE id = NEW.knowledge_answer_id;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'knowledge answer not found';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_chunk_tenant_before_write
        BEFORE INSERT OR UPDATE OF knowledge_answer_id, tenant_id
        ON knowledge_chunks
        FOR EACH ROW
        EXECUTE FUNCTION enforce_knowledge_chunk_tenant()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION propagate_knowledge_answer_tenant()
        RETURNS trigger AS $$
        BEGIN
            UPDATE knowledge_chunks
            SET tenant_id = NEW.tenant_id
            WHERE knowledge_answer_id = NEW.id;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_answer_tenant_after_update
        AFTER UPDATE OF tenant_id
        ON knowledge_answers
        FOR EACH ROW
        EXECUTE FUNCTION propagate_knowledge_answer_tenant()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS knowledge_answer_tenant_after_update "
        "ON knowledge_answers"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS propagate_knowledge_answer_tenant()"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS knowledge_chunk_tenant_before_write "
        "ON knowledge_chunks"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS enforce_knowledge_chunk_tenant()"
    )
    op.drop_index(
        "ix_support_queries_tenant_id",
        table_name="support_queries",
    )
    op.drop_column("support_queries", "tenant_id")
    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_column("users", "token_version")
    op.drop_column("users", "tenant_id")
