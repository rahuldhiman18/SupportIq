"""Health + queue observability: /healthz (open LB probe) + /admin/queue-stats.

Proves the approved design:
- /healthz is OPEN (no admin key), 200 with {status, db, worker}
- inline mode reports worker=disabled (no thread expected, not a failure)
- DB down -> 503 (LB routes away)
- /admin/queue-stats needs the admin key (401 without), returns counts +
  ages as FACTS (monitor owns thresholds, app only hints 120/600)
- heartbeat: fresh loop beat -> running/idle, stale beat -> stale
"""
import time

import pytest

import app.main as main
from app import worker as _worker


def test_healthz_open_inline_reports_disabled(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "up"
    assert body["worker"] == "disabled"  # inline mode: no thread expected


def test_healthz_needs_no_admin_key(client):
    # Explicit: no header at all, still 200 (LBs carry no credentials).
    r = client.get("/healthz", headers={})
    assert r.status_code == 200


def test_healthz_503_when_db_down(client):
    from sqlalchemy.exc import OperationalError

    # Stub session: raises INSIDE the endpoint body (where `except
    # SQLAlchemyError -> 503` lives), not at dependency-resolution time.
    class DeadDB:
        def execute(self, *a, **k):
            raise OperationalError("select 1", {}, Exception("down"))

    main.app.dependency_overrides[main.get_db] = lambda: DeadDB()
    try:
        r = client.get("/healthz")
        assert r.status_code == 503
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)


def test_queue_stats_requires_admin_key(client):
    r = client.get("/admin/queue-stats")
    assert r.status_code == 401


def test_queue_stats_counts_and_ages(client, db_session):
    from tests.test_queries import ADMIN_HEADERS
    from app.models import SupportQuery

    db_session.add(SupportQuery(question="q1", customer_email="a@b.com",
                                status="queued"))
    db_session.add(SupportQuery(question="q2", customer_email="a@b.com",
                                status="needs_human", attempts=3))
    db_session.commit()
    r = client.get("/admin/queue-stats", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["counts"]["queued"] >= 1
    assert body["needs_human"] >= 1
    assert body["oldest_queued_age_s"] is not None
    assert body["oldest_queued_age_s"] >= 0
    # App reports facts; thresholds are monitor-owned hints, not verdicts.
    assert body["alert_hint_s"] == {"warn": 120, "critical": 600}
    assert body["worker"] == "disabled"


def test_heartbeat_fresh_vs_stale(monkeypatch):
    import app.routes_health as health

    monkeypatch.setenv("QUERY_MODE", "queued")
    now = time.monotonic()
    monkeypatch.setattr(_worker, "_heartbeat",
                        {"last_loop_at": now, "last_claim_at": now})
    state, loop_age, claim_age = health._worker_state()
    assert state == "running"
    assert loop_age is not None and loop_age < 1.0
    # Looping but never claimed: healthy idle, not dead.
    monkeypatch.setattr(_worker, "_heartbeat",
                        {"last_loop_at": now, "last_claim_at": None})
    assert health._worker_state()[0] == "idle"
    # No beat for 60s: thread is gone.
    monkeypatch.setattr(_worker, "_heartbeat",
                        {"last_loop_at": now - 60.0, "last_claim_at": now - 60.0})
    assert health._worker_state()[0] == "stale"
    # Never started: also stale (queued mode expects a worker).
    monkeypatch.setattr(_worker, "_heartbeat",
                        {"last_loop_at": None, "last_claim_at": None})
    assert health._worker_state()[0] == "stale"


def test_claim_path_beats_claim_heartbeat():
    # Layer boundary (approved): claim_next's raw SQL is Postgres-only, so
    # SQLite can only test the pure-Python bookkeeping -- the one-liner
    # `run_once` calls after a successful claim. Live Postgres proves the
    # full path via scripts/verify_queue_live.py.
    _worker._beat(claimed=True)
    assert _worker.get_heartbeat()["last_claim_at"] is not None
