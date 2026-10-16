"""Schema-shape regression: the index guarding the answer-poll hot path.

What this proves: the MODEL declares ix_support_answers_query_id, so every
`Base.metadata.create_all` database (tests, fresh dev envs) gets it -- the
same channel that originally created our orders/users indexes.

What it does NOT prove: that the migration ran on live Postgres. That side
is verified by `alembic check` plus querying pg_indexes after upgrade.

Why dialect-aware: pytest runs on in-memory SQLite (no pg_indexes), and the
test must stay offline-free.
"""
from sqlalchemy import text

EXPECTED_INDEX = "ix_support_answers_query_id"


def test_support_answers_query_id_index_is_declared(db_session):
    bind = db_session.get_bind()
    if bind.dialect.name == "postgresql":
        stmt = text("SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'support_answers'")
    else:
        stmt = text("SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND tbl_name = 'support_answers'")
    names = {row[0] for row in db_session.execute(stmt).all()}
    assert EXPECTED_INDEX in names, (
        f"{EXPECTED_INDEX} missing from {bind.dialect.name} schema -- "
        "answer-poll reads would fall back to a sequential scan"
    )