"""Step 3b tests: enqueue-only POST /queries (QUERY_MODE=queued) + worker drain.

AI is MOCKED everywhere -- offline, zero quota. The Postgres-only claim SQL
(FOR UPDATE SKIP LOCKED / EXTRACT) cannot run on the in-memory SQLite test
DB, so claim behavior is covered by scripts/verify_queue_live.py against real
Postgres; here we drive process_one directly and monkeypatch claim_next to
exercise run_once end-to-end.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

from app.services.ai_service import AIProviderError, AITimeoutError
from app.models import Order, OrderEvent, Support_Answer, SupportQuery
from app.services.worker import MAX_AI_ATTEMPTS, process_one, run_once


def _seed_stalled_order(db, number="ORD-3B01", email="buyer@step3b.test"):
    """Shipped order whose last scan is 10 days old -> 3+ biz days stale."""
    order = Order(order_number=number, customer_email=email,
                  delivery_method="standard", destination_city="Pune",
                  carrier="BlueDart")
    db.add(order)
    db.flush()
    db.add(OrderEvent(order_id=order.id, stage="placed",
                      location="Mumbai warehouse",
                      occurred_at=datetime.now(timezone.utc) - timedelta(days=12)))
    db.add(OrderEvent(order_id=order.id, stage="shipped",
                      location="Mumbai warehouse",
                      occurred_at=datetime.now(timezone.utc) - timedelta(days=10)))
    db.commit()
    return order


def test_query_mode_defaults_to_inline(monkeypatch):
    # Rollback safety: with no env set, Phase A behavior must remain default.
    from app.main import _query_mode
    monkeypatch.delenv("QUERY_MODE", raising=False)
    assert _query_mode() == "inline"


def test_queued_mode_enqueues_without_ai_and_persists_context(client, db_session,
                                                              monkeypatch):
    monkeypatch.setenv("QUERY_MODE", "queued")

    def _boom(*args, **kwargs):
        raise AssertionError("AI must never run inside POST /queries")

    monkeypatch.setattr("app.main.answer_customer_question", _boom)
    r = client.post("/queries", json={
        "customer_email": "q@step3b.test",
        "question": "What is your return window?",
        "order_number": "ORD-3B99",
        "purchase_date": "2026-10-01",
        "order_status": "delivered",
    })
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert body["intent"] is None  # classified later, by the worker
    row = db_session.get(SupportQuery, UUID(body["query_id"]))
    assert row.status == "queued"
    assert row.priority == 400  # base tier; ages -1pt/30s from here
    assert row.attempts == 0
    # Step 3b context persistence: the worker rebuilds the prompt from these
    assert row.order_number == "ORD-3B99"
    assert row.purchase_date == date(2026, 10, 1)
    assert row.order_status == "delivered"


def test_queued_mode_stalled_order_jumps_to_critical(client, db_session,
                                                     monkeypatch):
    monkeypatch.setenv("QUERY_MODE", "queued")
    _seed_stalled_order(db_session)
    r = client.post("/queries", json={
        "customer_email": "buyer@step3b.test",
        "question": "Where is my parcel? It should be ORD-3B01",
        "order_number": "ORD-3B01",
    })
    assert r.status_code == 202
    row = db_session.get(SupportQuery, UUID(r.json()["query_id"]))
    # Stalled carrier (verified server-side from live events) -> CRITICAL,
    # ahead of every non-stalled query in the claim order.
    assert row.priority == 0


def test_inline_mode_still_answers_inside_the_request(client, db_session,
                                                      monkeypatch):
    monkeypatch.delenv("QUERY_MODE", raising=False)
    monkeypatch.setattr(
        "app.main.answer_customer_question",
        lambda *a, **k: SimpleNamespace(intent="timing", answer_text="14 days."),
    )
    r = client.post("/queries", json={
        "customer_email": "inline@step3b.test",
        "question": "Can I still return this?",
        "order_number": "ORD-IN01",
    })
    assert r.status_code == 202
    body = r.json()
    assert body["intent"] == "timing"
    row = db_session.get(SupportQuery, UUID(body["query_id"]))
    assert row.order_number == "ORD-IN01"  # persistence is mode-independent
    assert db_session.query(Support_Answer).filter(
        Support_Answer.query_id == row.id).count() == 1


def test_worker_success_resolves_and_answers(db_session, monkeypatch):
    q = SupportQuery(question="What is the refund window?",
                     customer_email="w@step3b.test", status="queued")
    db_session.add(q)
    db_session.commit()
    monkeypatch.setattr(
        "app.services.worker.answer_customer_question_once",
        lambda *a, **k: SimpleNamespace(intent="eligibility",
                                        answer_text="14 days, per policy."),
    )
    assert process_one(db_session, q) == "resolved"
    db_session.refresh(q)
    assert q.intent == "eligibility"
    assert q.attempts == 0
    assert q.next_retry_at is None and q.locked_at is None
    answer = db_session.query(Support_Answer).filter(
        Support_Answer.query_id == q.id).one()
    assert answer.answer_text == "14 days, per policy."


def test_worker_failure_books_backoff_not_a_500(db_session, monkeypatch):
    q = SupportQuery(question="payment failed", customer_email="f@step3b.test",
                     status="queued")
    db_session.add(q)
    db_session.commit()

    def _raise(*args, **kwargs):
        raise AITimeoutError("slow")

    monkeypatch.setattr("app.services.worker.answer_customer_question_once", _raise)
    assert process_one(db_session, q) == "queued"
    db_session.refresh(q)
    assert q.attempts == 1
    assert q.next_retry_at is not None  # ~2 minutes out
    assert q.locked_at is None
    assert "AITimeoutError" in q.last_error


def test_worker_exhausted_hands_off_to_human_feed(client, db_session,
                                                  monkeypatch):
    q = SupportQuery(question="payment failed help", status="queued",
                     customer_email="h@step3b.test",
                     attempts=MAX_AI_ATTEMPTS - 1)
    db_session.add(q)
    db_session.commit()

    def _raise(*args, **kwargs):
        raise AIProviderError("500")

    monkeypatch.setattr("app.services.worker.answer_customer_question_once", _raise)
    assert process_one(db_session, q) == "needs_human"
    db_session.refresh(q)
    assert q.attempts == MAX_AI_ATTEMPTS
    feed = client.get("/admin/needs-human",
                      headers={"X-Admin-Key": "test-admin-key"})
    assert feed.status_code == 200
    assert str(q.id) in [item["query_id"] for item in feed.json()["items"]]


def test_run_once_drains_one_claim_then_stops(db_session, monkeypatch):
    q = SupportQuery(question="hello there", customer_email="r@step3b.test",
                     status="queued")
    db_session.add(q)
    db_session.commit()
    calls = {"claims": 0}

    def fake_claim(db, now=None):
        # Postgres claim SQL can't run on SQLite; emulate exactly one claim.
        calls["claims"] += 1
        return db.get(SupportQuery, q.id) if calls["claims"] == 1 else None

    monkeypatch.setattr("app.services.worker.claim_next", fake_claim)
    monkeypatch.setattr(
        "app.services.worker.answer_customer_question_once",
        lambda *a, **k: SimpleNamespace(intent="out_of_scope",
                                        answer_text="A human will follow up."),
    )
    assert run_once(db=db_session) is True
    db_session.refresh(q)
    assert q.status == "resolved"
    assert run_once(db=db_session) is False  # queue empty now