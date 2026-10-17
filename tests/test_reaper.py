"""P0 reaper + recovery-path tests (offline, mocked AI).

Covers the Python-testable halves: the broad `except` seatbelt in
process_one (unexpected errors book an attempt instead of stranding the
row) and the narrow requeue endpoint (admin recovery, resolved stays final).

The 60s reclaim itself is Postgres-only (it rides the SKIP LOCKED claim
transaction), so it is proven by scripts/verify_queue_live.py Phase 3
against a real database -- SQLite cannot execute that SQL.
"""
from app.worker import MAX_AI_ATTEMPTS, STALE_PROCESSING_SECONDS, process_one
from app.models import SupportQuery


def test_reaper_constants_locked():
    # Approved design numbers: 60s invariant vs ~11s worst-case legit work.
    assert STALE_PROCESSING_SECONDS == 60
    assert MAX_AI_ATTEMPTS == 3


def test_unmapped_exception_books_attempt_not_strand(db_session, monkeypatch):
    q = SupportQuery(question="x", customer_email="crash@reaper.test",
                     status="queued")
    db_session.add(q)
    db_session.commit()

    def _explode(*args, **kwargs):
        raise RuntimeError("totally unexpected boom")

    monkeypatch.setattr("app.worker.answer_customer_question_once", _explode)
    # Seatbelt: ends `queued` for retry -- NOT `processing` (stranded).
    assert process_one(db_session, q) == "queued"
    db_session.refresh(q)
    assert q.attempts == 1
    assert "RuntimeError" in q.last_error
    assert q.next_retry_at is not None and q.locked_at is None


def test_unmapped_exception_at_budget_edge_hands_off(db_session, monkeypatch):
    q = SupportQuery(question="x", customer_email="crash2@reaper.test",
                     status="queued", attempts=MAX_AI_ATTEMPTS - 1)
    db_session.add(q)
    db_session.commit()

    def _explode(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr("app.worker.answer_customer_question_once", _explode)
    assert process_one(db_session, q) == "needs_human"
    db_session.refresh(q)
    assert q.attempts == MAX_AI_ATTEMPTS


def test_success_path_unaffected_by_seatbelt(db_session, monkeypatch):
    # Regression guard: the broad except must not swallow success-path flow.
    from types import SimpleNamespace
    q = SupportQuery(question="x", customer_email="ok@reaper.test",
                     status="queued")
    db_session.add(q)
    db_session.commit()
    monkeypatch.setattr(
        "app.worker.answer_customer_question_once",
        lambda *a, **k: SimpleNamespace(intent="timing", answer_text="fine"),
    )
    assert process_one(db_session, q) == "resolved"
    db_session.refresh(q)
    assert q.attempts == 0 and q.last_error is None


# ---------------- narrow requeue endpoint (admin recovery) ------------------

def _make(client, db_session, status, email):
    q = SupportQuery(question="help me", customer_email=email, status=status)
    db_session.add(q)
    db_session.commit()
    return q


def _requeue(client, qid, key="test-admin-key"):
    headers = {"X-Admin-Key": key} if key else {}
    return client.post(f"/queries/{qid}/requeue", headers=headers)


def test_requeue_needs_human_back_to_queue(client, db_session):
    q = _make(client, db_session, "needs_human", "nh@reaper.test")
    q.attempts = 3
    q.last_error = "AIProviderError: 500"
    db_session.commit()
    r = _requeue(client, q.id)
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    db_session.refresh(q)
    assert q.status == "queued"
    assert q.next_retry_at is None and q.locked_at is None
    assert q.attempts == 3  # audit trail preserved, not reset


def test_requeue_stuck_processing_row(client, db_session):
    q = _make(client, db_session, "processing", "st@reaper.test")
    assert _requeue(client, q.id).status_code == 200
    db_session.refresh(q)
    assert q.status == "queued"


def test_requeue_resolved_is_rejected(client, db_session):
    # resolved stays final: reopen-by-accident impossible by construction.
    q = _make(client, db_session, "resolved", "done@reaper.test")
    assert _requeue(client, q.id).status_code == 409
    db_session.refresh(q)
    assert q.status == "resolved"  # untouched


def test_requeue_pending_query_rejected(client, db_session):
    q = _make(client, db_session, "pending", "pd@reaper.test")
    assert _requeue(client, q.id).status_code == 409


def test_requeue_unknown_query_404(client):
    assert _requeue(client, "00000000-0000-0000-0000-000000000000").status_code == 404


def test_requeue_requires_admin_key(client, db_session):
    q = _make(client, db_session, "needs_human", "nk@reaper.test")
    assert _requeue(client, q.id, key=None).status_code == 401
    assert _requeue(client, q.id, key="wrong-key").status_code == 401