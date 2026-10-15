"""Shared pytest fixtures.

Everything here is offline and free:
- A dummy GEMINI_API_KEY is set BEFORE importing the app so ai_service
  can import without raising (the key is never used: AI calls are mocked).
- The app's DB dependency is overridden with in-memory SQLite.
"""

import os

# Must happen before any `app.*` import (ai_service fails fast on missing key)
os.environ.setdefault("GEMINI_API_KEY", "dummy-key-for-tests-no-network")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.main import app, get_db

# In-memory SQLite shared across connections (StaticPool) for the test session
_test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=_test_engine, autoflush=False)


@pytest.fixture(autouse=True)
def _create_test_tables():
    # SQLite ignores postgresql_where, so the Postgres-only partial unique
    # index renders as a normal unique index here -- fine for tests.
    Base.metadata.create_all(_test_engine)
    yield
    Base.metadata.drop_all(_test_engine)
    # The public-chat throttle keeps a GLOBAL in-memory hit list per IP, and
    # TestClient always uses the same IP ("testclient") -- clear it so throttle
    # budget from one test never 429s an unrelated later test.
    import app.main as _main
    _main._rate_hits.clear()


@pytest.fixture
def db_session():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
