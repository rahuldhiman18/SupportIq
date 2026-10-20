"""Unit tests for POST /queries and related routes (Layer 2).

All AI calls are MOCKED -- these tests are 100% offline (no API quota used).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import select

from app.services.ai_service import (
    CustomerAnswer,
    AIRateLimitError,
    AITimeoutError,
    AIContentBlockedError,
    AIProviderError,
)
from app.models import (
    KnowledgeAnswer,
    KnowledgeAnswerVersion,
    SupportQuery,
    Order,
    OrderEvent,
    add_business_days,
)

POLICY_TEXT = "Refunds are available within 30 days of purchase."


def _seed_current_policy(db_session) -> None:
    knowledge = KnowledgeAnswer(
        canonical_question="What is the refund policy?",
        source_url="https://example.com/terms/refund-policy",
        is_active=True,
    )
    db_session.add(knowledge)
    db_session.flush()
    db_session.add(
        KnowledgeAnswerVersion(
            knowledge_answer_id=knowledge.id,
            version_number=1,
            answer_text=POLICY_TEXT,
            status="approved",
            is_current=True,
            approved_by="test",
            approved_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()


# --- happy path: knowledge context is passed to the AI layer ---------------

def test_refund_question_passes_policy_context(client, db_session):
    _seed_current_policy(db_session)

    fake = CustomerAnswer(intent="eligibility", answer_text="You have 30 days.")
    with patch(
        "app.main.answer_customer_question", return_value=fake
    ) as mock_ai:
        response = client.post(
            "/queries",
            json={"customer_email": "a@b.com", "question": "can I get a refund?"},
        )

    assert response.status_code == 202
    assert mock_ai.call_count == 1
    # policy_context must contain the seeded policy text (labeled with its topic)
    assert POLICY_TEXT in mock_ai.call_args.kwargs["policy_context"]
    assert "What is the refund policy?" in mock_ai.call_args.kwargs["policy_context"]
    assert mock_ai.call_args.args[0] == "can I get a refund?"
    # intent from the AI result is returned to the caller
    assert response.json()["intent"] == "eligibility"


def test_query_records_knowledge_version_and_intent(client, db_session):
    _seed_current_policy(db_session)

    fake = CustomerAnswer(intent="timing", answer_text="5-10 business days.")
    with patch("app.main.answer_customer_question", return_value=fake):
        response = client.post(
            "/queries",
            json={
                "customer_email": "a@b.com",
                "question": "when will my refund come back?",
            },
        )

    assert response.status_code == 202
    assert response.json()["intent"] == "timing"
    query_id = response.json()["query_id"]
    query = db_session.execute(
        select(SupportQuery).where(SupportQuery.id == UUID(query_id))
    ).scalar_one()
    assert query.knowledge_version_id is not None
    assert query.intent == "timing"


def test_no_knowledge_passes_none_context(client):
    fake = CustomerAnswer(intent="out_of_scope", answer_text="A human will follow up.")
    with patch(
        "app.main.answer_customer_question", return_value=fake
    ) as mock_ai:
        response = client.post(
            "/queries",
            json={"customer_email": "a@b.com", "question": "hello"},
        )

    assert response.status_code == 202
    assert mock_ai.call_args.kwargs["policy_context"] is None
    assert mock_ai.call_args.kwargs["situation"] is None


# --- multiple approved policies: ALL of them must reach the prompt ----------
SHIPPING_TEXT = "Standard shipping takes 3-5 business days."


def test_multiple_policies_all_reach_the_prompt(client, db_session):
    _seed_current_policy(db_session)
    shipping = KnowledgeAnswer(
        canonical_question="What is the shipping policy?",
        source_url="https://example.com/terms/shipping",
        is_active=True,
    )
    db_session.add(shipping)
    db_session.flush()
    db_session.add(
        KnowledgeAnswerVersion(
            knowledge_answer_id=shipping.id,
            version_number=1,
            answer_text=SHIPPING_TEXT,
            status="approved",
            is_current=True,
            approved_by="test",
            approved_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    fake = CustomerAnswer(intent="timing", answer_text="ok")
    with patch("app.main.answer_customer_question", return_value=fake) as mock_ai:
        response = client.post(
            "/queries",
            json={"customer_email": "a@b.com", "question": "do you ship fast?"},
        )

    assert response.status_code == 202
    context = mock_ai.call_args.kwargs["policy_context"]
    # both current policies, each labeled with its topic, in ONE context
    assert POLICY_TEXT in context
    assert SHIPPING_TEXT in context
    assert "What is the refund policy?" in context
    assert "What is the shipping policy?" in context


# --- personalization: customer situation reaches the AI prompt --------------

def test_situation_is_built_from_purchase_date_and_order_status(client):
    purchase_date = (datetime.now(timezone.utc).date() - timedelta(days=12)).isoformat()
    fake = CustomerAnswer(intent="eligibility", answer_text="You are within 30 days.")
    with patch(
        "app.main.answer_customer_question", return_value=fake
    ) as mock_ai:
        response = client.post(
            "/queries",
            json={
                "customer_email": "a@b.com",
                "question": "am i still in time for a refund?",
                "purchase_date": purchase_date,
                "order_status": "delivered",
            },
        )

    assert response.status_code == 202
    situation = mock_ai.call_args.kwargs["situation"]
    # days_ago math is computed against today; accept 11-13 to stay robust
    # against a UTC/local midnight boundary during the test run
    assert any(f"purchased {d} days ago" in situation for d in (11, 12, 13))
    assert "order status: delivered" in situation


def test_situation_none_when_no_details_given(client):
    fake = CustomerAnswer(intent="how_to_request", answer_text="Email us.")
    with patch(
        "app.main.answer_customer_question", return_value=fake
    ) as mock_ai:
        response = client.post(
            "/queries",
            json={"customer_email": "a@b.com", "question": "how do I ask for a refund?"},
        )

    assert response.status_code == 202
    # prompt_context always adds the not-logged-in hint for anonymous
    # order-topic questions (asks for order number, never invents facts).
    assert "NOT LOGGED IN" in mock_ai.call_args.kwargs["situation"]


# --- error mapping: typed AI errors -> proper HTTP statuses (all free) ------

def _post_question(client):
    return client.post(
        "/queries",
        json={"customer_email": "a@b.com", "question": "can I get a refund?"},
    )


@pytest.mark.parametrize(
    ("side_effect", "expected_status"),
    [
        (AIRateLimitError("quota"), 429),
        (AITimeoutError("slow"), 504),
        (AIContentBlockedError("blocked"), 502),
        (AIProviderError("down"), 502),
    ],
)
def test_ai_errors_map_to_http_statuses(client, side_effect, expected_status):
    with patch("app.main.answer_customer_question", side_effect=side_effect):
        response = _post_question(client)
    assert response.status_code == expected_status


# --- the fixed routes actually work -----------------------------------------

def test_get_query_route_resolves(client):
    fake = CustomerAnswer(intent="eligibility", answer_text="ok")
    with patch("app.main.answer_customer_question", return_value=fake):
        created = _post_question(client)
    query_id = created.json()["query_id"]

    response = client.get(f"/queries/{query_id}")
    assert response.status_code == 200
    assert response.json()["query_id"] == query_id
    assert response.json()["intent"] == "eligibility"


def test_get_query_404(client):
    response = client.get("/queries/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_get_endpoints_are_read_only_no_side_effects(client, db_session):
    # Repeated GET polling must not create answers, mutate the query, or call AI.
    from app.models import Support_Answer
    fake = CustomerAnswer(intent="eligibility", answer_text="ok")
    with patch("app.main.answer_customer_question", return_value=fake) as mock_ai:
        created = _post_question(client)
    query_id = created.json()["query_id"]
    before_count = db_session.query(Support_Answer).count()
    mock_ai.reset_mock()

    for _ in range(5):
        assert client.get(f"/queries/{query_id}").status_code == 200
        assert client.get(f"/queries/{query_id}/answer").status_code == 200

    db_session.expire_all()
    assert mock_ai.call_count == 0  # no AI triggered by GET polling
    assert db_session.query(Support_Answer).count() == before_count


def test_get_query_scoped_to_owner_email_returns_404_for_stranger(client):
    # ?email= scoping: stranger must not read another customer's query (404, not leak).
    fake = CustomerAnswer(intent="eligibility", answer_text="ok")
    with patch("app.main.answer_customer_question", return_value=fake):
        created = _post_question(client)  # owned by a@b.com
    query_id = created.json()["query_id"]

    assert client.get(f"/queries/{query_id}?email=a@b.com").status_code == 200
    assert client.get(f"/queries/{query_id}?email=stranger@evil.com").status_code == 404
    assert client.get(
        f"/queries/{query_id}/answer?email=stranger@evil.com").status_code == 404


# --- Step 2: priority-with-aging math (pure code, no DB writes) --------------

def test_base_priority_ignores_client_urgency_spoof():
    # Server maps from intent + verified stall only; no client urgency param exists.
    from app.models import (compute_base_priority, PRIORITY_CRITICAL,
                            PRIORITY_HIGH, PRIORITY_NORMAL, PRIORITY_LOW,
                            PRIORITY_LOWEST)
    assert compute_base_priority("payment_issues") == PRIORITY_CRITICAL
    assert compute_base_priority("eligibility", stalled=True) == PRIORITY_CRITICAL
    assert compute_base_priority("order_tracking") == PRIORITY_HIGH
    assert compute_base_priority("delivery_eta") == PRIORITY_HIGH
    assert compute_base_priority("eligibility") == PRIORITY_NORMAL
    assert compute_base_priority("timing") == PRIORITY_NORMAL
    assert compute_base_priority("how_to_request") == PRIORITY_LOW
    assert compute_base_priority("out_of_scope") == PRIORITY_LOWEST
    assert compute_base_priority(None) == PRIORITY_LOWEST
    assert compute_base_priority("unknown") == PRIORITY_LOWEST


def test_aging_eventually_beats_fresh_urgent_no_starvation():
    # Wide bands: critical=0, normal=200, 1pt per 30s.
    # Old normal after 50min wait: 200-100=100... after 110min: 200-220=-20 < 0.
    from datetime import timedelta
    from app.models import (effective_score, PRIORITY_CRITICAL, PRIORITY_NORMAL)
    now = datetime.now(timezone.utc)
    fresh_urgent = effective_score(PRIORITY_CRITICAL, now, now)
    old_normal_50min = effective_score(
        PRIORITY_NORMAL, now - timedelta(minutes=50), now)
    old_normal_110min = effective_score(
        PRIORITY_NORMAL, now - timedelta(minutes=110), now)
    # 50min is not enough to overtake (100 > 0): priority still respected early.
    assert old_normal_50min > fresh_urgent
    # 110min overtakes (-20 < 0): starvation-proof over long waits.
    assert old_normal_110min < fresh_urgent


def test_aging_ordering_is_monotonic_in_wait():
    # Same band: older always scores lower (served first). Newer never jumps older.
    from datetime import timedelta
    from app.models import effective_score, PRIORITY_HIGH
    now = datetime.now(timezone.utc)
    older = effective_score(PRIORITY_HIGH, now - timedelta(minutes=10), now)
    newer = effective_score(PRIORITY_HIGH, now - timedelta(minutes=1), now)
    assert older < newer


# --- Human-in-the-loop: admin answers after the AI gives up (Option A) ------


def _make_needs_human(db_session, email="a@b.com"):
    q = SupportQuery(
        question="where is my refund?",
        customer_email=email,
        status="needs_human",
    )
    db_session.add(q)
    db_session.commit()
    db_session.refresh(q)
    return q


def test_human_answer_requires_admin_key(client, db_session):
    q = _make_needs_human(db_session)
    r = client.patch(f"/queries/{q.id}/answer", json={"answer_text": "Hi"})
    assert r.status_code == 401
    r = client.patch(
        f"/queries/{q.id}/answer",
        json={"answer_text": "Hi"},
        headers={"X-Admin-Key": "wrong-key"},
    )
    assert r.status_code == 401


def test_human_answer_404_unknown_query(client):
    r = client.patch(
        "/queries/00000000-0000-0000-0000-000000000000/answer",
        json={"answer_text": "Hi"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 404


def test_human_answer_resolves_query(client, db_session):
    from app.models import Support_Answer

    q = _make_needs_human(db_session)
    r = client.patch(
        f"/queries/{q.id}/answer",
        json={"answer_text": "Your refund is on the way."},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["answer_text"] == "Your refund is on the way."
    # Single transaction: answer persisted AND query flipped out of needs_human.
    db_session.expire_all()
    assert db_session.query(Support_Answer).filter_by(query_id=q.id).count() == 1
    row = db_session.query(SupportQuery).filter_by(id=q.id).one()
    assert row.status == "resolved"


def test_human_answer_rejects_empty_text(client, db_session):
    q = _make_needs_human(db_session)
    r = client.patch(
        f"/queries/{q.id}/answer",
        json={"answer_text": ""},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 422


def test_human_answer_rejects_closed_query(client, db_session):
    q = _make_needs_human(db_session)
    q.status = "closed"
    db_session.commit()
    r = client.patch(
        f"/queries/{q.id}/answer",
        json={"answer_text": "Hi"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 409


# --- Status-change auth + the "AI failed" notification feed ------------------


def test_status_change_requires_admin_key(client, db_session):
    q = _make_needs_human(db_session)
    q.status = "pending"
    db_session.commit()
    r = client.patch(f"/queries/{q.id}", json={"status": "resolved"})
    assert r.status_code == 401
    r = client.patch(
        f"/queries/{q.id}",
        json={"status": "resolved"},
        headers={"X-Admin-Key": "wrong-key"},
    )
    assert r.status_code == 401


def test_admin_can_change_status(client, db_session):
    q = _make_needs_human(db_session)
    q.status = "pending"
    db_session.commit()
    r = client.patch(
        f"/queries/{q.id}",
        json={"status": "closed"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "closed"
    db_session.expire_all()
    assert db_session.query(SupportQuery).filter_by(id=q.id).one().status == "closed"


def test_status_change_404_and_422(client, db_session):
    q = _make_needs_human(db_session)
    r = client.patch(
        "/queries/00000000-0000-0000-0000-000000000000",
        json={"status": "closed"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 404
    r = client.patch(
        f"/queries/{q.id}",
        json={"status": "not-a-status"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 422


def test_needs_human_feed_requires_admin_key(client):
    assert client.get("/admin/needs-human").status_code == 401
    r = client.get("/admin/needs-human", headers={"X-Admin-Key": "wrong"})
    assert r.status_code == 401


def test_needs_human_feed_lists_only_failed_queries(client, db_session):
    # Empty DB -> count 0 (conftest drops tables between tests).
    r = client.get("/admin/needs-human", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"count": 0, "items": []}

    answered = _make_needs_human(db_session, email="done@b.com")
    answered.status = "resolved"
    db_session.add(
        SupportQuery(question="still pending", customer_email="c@d.com", status="pending")
    )
    failed = _make_needs_human(db_session, email="stuck@b.com")
    failed.attempts = 3
    failed.last_error = "AIProviderError: 500"
    db_session.commit()

    r = client.get("/admin/needs-human", headers=ADMIN_HEADERS)
    body = r.json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["query_id"] == str(failed.id)
    assert item["question"] == "where is my refund?"
    assert item["attempts"] == 3
    assert "AIProviderError" in item["last_error"]
    # The popup text the dashboard renders:
    assert "you need to answer" in item["message"]
    assert "AI failed" in item["message"]


def test_needs_human_feed_orders_oldest_first(client, db_session):
    older = _make_needs_human(db_session, email="older@b.com")
    newer = _make_needs_human(db_session, email="newer@b.com")
    older.created_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    db_session.commit()

    r = client.get("/admin/needs-human", headers=ADMIN_HEADERS)
    ids = [i["query_id"] for i in r.json()["items"]]
    assert ids == [str(older.id), str(newer.id)]


def test_get_latest_answer(client):
    fake = CustomerAnswer(intent="timing", answer_text="You have 30 days.")
    with patch("app.main.answer_customer_question", return_value=fake):
        created = _post_question(client)
    query_id = created.json()["query_id"]

    response = client.get(f"/queries/{query_id}/answer")
    assert response.status_code == 200
    assert response.json()["answer_text"] == "You have 30 days."


# --- new business intents are accepted end-to-end (not downgraded to unknown) ---

@pytest.mark.parametrize(
    "intent",
    [
        "order_tracking",
        "delivery_eta",
        "password_reset",
        "account_management",
        "payment_issues",
        "wrong_purchase",
        "wrong_address",
    ],
)
def test_new_intents_pass_through(client, intent):
    fake = CustomerAnswer(intent=intent, answer_text="Here is what to do.")
    with patch("app.main.answer_customer_question", return_value=fake):
        response = _post_question(client)

    assert response.status_code == 202
    assert response.json()["intent"] == intent


# --- Phase 1: location algo (pure code - no AI involved) --------------------

def test_add_business_days_skips_weekends():
    friday = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    assert add_business_days(friday, 1) == datetime(
        2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    assert add_business_days(friday, 5) == datetime(
        2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _seed_order(db_session, number="ORD-777", email="a@b.com",
                method="standard", events=None):
    order = Order(order_number=number, customer_email=email,
                  delivery_method=method, destination_city="Pune")
    db_session.add(order)
    db_session.flush()
    for stage, location, occurred_at in (events or []):
        db_session.add(OrderEvent(order_id=order.id, stage=stage,
                                  location=location, occurred_at=occurred_at))
    db_session.commit()
    return order


def test_eta_window_is_business_day_math(db_session):
    order = _seed_order(db_session, events=[
        ("placed", "Mumbai warehouse",
         datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)),
        ("shipped", "Pune hub",
         datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)),
    ])
    earliest, latest = order.eta_window()
    assert earliest.date().isoformat() == "2026-10-15"   # +3 business days
    assert latest.date().isoformat() == "2026-10-19"     # +5 (weekend skipped)


def test_stalled_order_detected_when_scans_go_stale(db_session):
    stale = _seed_order(db_session, number="ORD-111", events=[
        ("shipped", "Delhi hub",
         datetime.now(timezone.utc) - timedelta(days=10)),
    ])
    assert stale.is_stalled() is True
    fresh = _seed_order(db_session, number="ORD-222", events=[
        ("shipped", "Delhi hub",
         datetime.now(timezone.utc) - timedelta(hours=5)),
    ])
    assert fresh.is_stalled() is False


# --- Phase 1: live facts reach the AI, but only for the right customer ------

ADMIN_HEADERS = {"X-Admin-Key": "test-admin-key"}


def _create_order_api(client, number="ORD-9001", email="a@b.com"):
    return client.post("/orders", json={
        "order_number": number, "customer_email": email,
        "delivery_method": "standard", "destination_city": "Pune",
        "carrier": "BlueDart",
        "events": [
            {"stage": "placed", "location": "Mumbai warehouse",
             "occurred_at": "2026-10-10T10:00:00Z"},
            {"stage": "shipped", "location": "Pune hub",
             "occurred_at": "2026-10-12T09:00:00Z"},
        ],
    }, headers=ADMIN_HEADERS)


def test_order_facts_are_injected_into_situation(client):
    assert _create_order_api(client).status_code == 201
    fake = CustomerAnswer(intent="order_tracking", answer_text="On the way!")
    with patch("app.main.answer_customer_question", return_value=fake) as mock_ai:
        response = client.post("/queries", json={
            "customer_email": "a@b.com",
            "question": "where is my order right now?",
            "order_number": "ORD-9001",
        })
    assert response.status_code == 202
    situation = mock_ai.call_args.kwargs["situation"]
    assert "ORD-9001" in situation and "Pune hub" in situation
    assert "ETA" in situation


def test_order_number_is_extracted_from_question_text(client):
    assert _create_order_api(client, number="ORD-9002").status_code == 201
    fake = CustomerAnswer(intent="order_tracking", answer_text="Checking!")
    with patch("app.main.answer_customer_question", return_value=fake) as mock_ai:
        response = client.post("/queries", json={
            "customer_email": "a@b.com",
            "question": "can you check the status for ORD-9002 please?",
        })
    assert response.status_code == 202
    assert "ORD-9002" in mock_ai.call_args.kwargs["situation"]


def test_order_facts_never_leak_to_another_customer(client):
    assert _create_order_api(client, number="ORD-9003").status_code == 201
    fake = CustomerAnswer(intent="order_tracking", answer_text="ok")
    with patch("app.main.answer_customer_question", return_value=fake) as mock_ai:
        response = client.post("/queries", json={
            "customer_email": "stranger@evil.com",
            "question": "where is my order?",
            "order_number": "ORD-9003",
        })
    assert response.status_code == 202
    # Server refuses to load another customer's order (no leak); it records
    # a NOT FOUND note so the AI asks for verification instead of inventing.
    assert "NOT FOUND" in mock_ai.call_args.kwargs["situation"]


def test_duplicate_order_returns_409_not_500(client):
    assert _create_order_api(client, number="ORD-9004").status_code == 201
    response = _create_order_api(client, number="ORD-9004")
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_live_facts_instruction_outranks_policy_boilerplate():
    from app.services.ai_service import _INTENT_PROMPT
    assert "those facts\nare the answer's core" in _INTENT_PROMPT
    assert "quote the order number, stage, location, ETA" in _INTENT_PROMPT


def test_tracking_facts_use_friendly_dates_not_iso(db_session):
    order = _seed_order(db_session, number="ORD-FRIENDLY", events=[
        ("placed", "Mumbai warehouse",
         datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)),
        ("shipped", "Pune hub",
         datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)),
    ])
    facts = order.tracking_facts()
    assert "2026-10-" not in facts          # no raw ISO dates leak out
    assert "Mon, Oct 12" in facts           # last scan reads naturally
    assert "Thu, Oct 15" in facts and "Mon, Oct 19" in facts  # ETA window
    assert "T" not in facts.split("ETA")[0].split("scan")[-1]  # no ISO time


# --- Phase A auth: admin key on critical writes, throttle on public chat ---

def test_create_order_without_admin_key_is_401(client):
    response = client.post("/orders", json={
        "order_number": "ORD-NOKEY", "customer_email": "a@b.com"})
    assert response.status_code == 401


def test_create_order_with_wrong_admin_key_is_401(client):
    response = client.post("/orders", json={
        "order_number": "ORD-NOKEY", "customer_email": "a@b.com"},
        headers={"X-Admin-Key": "wrong-key"})
    assert response.status_code == 401


def test_create_knowledge_without_admin_key_is_401(client):
    response = client.post("/knowledge", json={
        "canonical_question": "Q?", "answer_text": "A",
        "source_url": "https://example.com/a"})
    assert response.status_code == 401


def test_create_knowledge_with_admin_key_is_201(client):
    response = client.post("/knowledge", json={
        "canonical_question": "Q?", "answer_text": "A",
        "source_url": "https://example.com/a"}, headers=ADMIN_HEADERS)
    assert response.status_code == 201


def test_public_chat_is_throttled_after_30_per_minute(client):
    from app.main import _rate_hits
    _rate_hits.clear()
    fake = CustomerAnswer(intent="out_of_scope", answer_text="ok")
    with patch("app.main.answer_customer_question", return_value=fake):
        for _ in range(30):
            response = client.post("/queries", json={
                "customer_email": "a@b.com", "question": "hi"})
            assert response.status_code == 202
        blocked = client.post("/queries", json={
            "customer_email": "a@b.com", "question": "one too many"})
    assert blocked.status_code == 429
    _rate_hits.clear()


# --- Phase B: JWT login (optional tokens; existing routes stay open) --------

def test_login_rejects_wrong_password(client, db_session):
    from app.main import hash_password
    from app.models import User
    salt, digest = hash_password("correct-horse")
    db_session.add(User(username="u1", email="u1@example.com",
                        password_hash=digest, password_salt=salt))
    db_session.commit()
    response = client.post("/auth/login", json={
        "username_or_email": "u1", "password": "wrong-horse"})
    assert response.status_code == 401


def test_login_issues_usable_token_and_order_self_scopes(client, db_session):
    from app.main import hash_password, _rate_hits
    from app.models import User
    _rate_hits.clear()
    salt, digest = hash_password("s3cret!")
    db_session.add(User(username="rahul", email="rahul@example.com",
                        password_hash=digest, password_salt=salt, role="admin"))
    db_session.commit()

    login = client.post("/auth/login", json={
        "username_or_email": "rahul@example.com", "password": "s3cret!"})
    assert login.status_code == 200
    token = login.json()["access_token"]
    assert login.json()["username"] == "rahul"

    # token-only order lookup (no ?email=): self-scopes to token email
    assert _create_order_api(client, number="ORD-JWT",
                             email="rahul@example.com").status_code == 201
    mine = client.get("/orders/ORD-JWT",
                      headers={"Authorization": f"Bearer {token}"})
    assert mine.status_code == 200
    assert mine.json()["order_number"] == "ORD-JWT"

    # another customer's order stays 404 even with a valid token
    assert _create_order_api(client, number="ORD-OTHER",
                             email="other@example.com").status_code == 201
    hidden = client.get("/orders/ORD-OTHER",
                        headers={"Authorization": f"Bearer {token}"})
    assert hidden.status_code == 404

    # tampered token is rejected
    bad = client.get("/orders/ORD-JWT",
                     headers={"Authorization": f"Bearer {token[:-2]}xx"})
    assert bad.status_code == 401
    _rate_hits.clear()


# --- Bug-hunt regressions (fixes applied in this pass) -----------------------

def test_double_human_answer_two_rows_newest_wins(client, db_session):
    # Regression: two rapid PATCH /answer calls used to land in the same
    # Windows timer tick and share one created_at; the ORDER BY created_at DESC
    # tie made GET /answer surface the OLDER row. models.utcnow_unique() now
    # guarantees strictly increasing timestamps within the process.
    from app.models import Support_Answer

    q = SupportQuery(question="q?", customer_email="probe@b.com",
                     status="needs_human")
    db_session.add(q)
    db_session.commit()
    db_session.refresh(q)

    r1 = client.patch(f"/queries/{q.id}/answer", json={"answer_text": "first"},
                      headers=ADMIN_HEADERS)
    r2 = client.patch(f"/queries/{q.id}/answer", json={"answer_text": "second"},
                      headers=ADMIN_HEADERS)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["created_at"] != r2.json()["created_at"]

    db_session.expire_all()
    assert db_session.query(Support_Answer).filter_by(query_id=q.id).count() == 2
    got = client.get(f"/queries/{q.id}/answer?email=probe@b.com").json()
    assert got["answer_text"] == "second"


def test_utcnow_unique_is_strictly_increasing_under_burst():
    from app.models import utcnow_unique

    stamps = [utcnow_unique() for _ in range(500)]
    assert all(b > a for a, b in zip(stamps, stamps[1:]))


def test_non_ascii_admin_key_is_401_not_500():
    # secrets.compare_digest raises TypeError on non-ASCII str; the fix
    # compares UTF-8 bytes so garbage input can never 500 the endpoint.
    # (httpx refuses to ascii-encode such headers client-side, so call the
    # auth dependency directly -- this is exactly what a raw socket client
    # delivering b"caf\\xe9" would hit after latin-1 header decoding.)
    from fastapi import HTTPException
    from app.main import require_admin_key

    with pytest.raises(HTTPException) as exc:
        require_admin_key(key="café")
    assert exc.value.status_code == 401


def test_email_shape_validation(client):
    # Garbage emails used to be accepted and stored; shape is now validated
    # at the edge (422) before any AI call or DB write. Mock keeps it offline
    # if validation ever regresses.
    with patch("app.main.answer_customer_question",
               return_value=CustomerAnswer("timing", "ok")):
        r = client.post("/queries", json={"question": "q?",
                                          "customer_email": "not-an-email"})
    assert r.status_code == 422


def test_admin_key_fallback_disabled_outside_pytest(monkeypatch):
    # The old fallback served the hardcoded "test-admin-key" even in prod
    # when ADMIN_API_KEY was unset. Now it activates only under pytest.
    from app.main import _expected_admin_key

    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert _expected_admin_key() is None

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x")
    assert _expected_admin_key() == "test-admin-key"


# --- Step 4: inline resolution returns "resolved" --------------------------

def test_inline_resolve_returns_status_resolved(client):
    """POST /queries inline path now returns status: resolved (not pending)."""
    fake = CustomerAnswer(intent="eligibility", answer_text="You are covered.")
    with patch("app.main.answer_customer_question", return_value=fake):
        response = client.post("/queries", json={
            "customer_email": "a@b.com", "question": "refund?"})
    assert response.status_code == 202
    assert response.json()["status"] == "resolved"


# --- Step 4: email outbox idempotency + delivery --------------------------

def test_queue_email_is_idempotent(db_session):
    """Same idempotency_key never produces two outbox rows."""
    from app.services.mail import queue_email
    row1 = queue_email(
        db_session,
        to="a@b.com",
        subject="S",
        body_text="B",
        idempotency_key="resolved:test-1",
    )
    row2 = queue_email(
        db_session,
        to="a@b.com",
        subject="S",
        body_text="B",
        idempotency_key="resolved:test-1",
    )
    assert row1 is not None
    assert row1.status == "queued"
    assert row2 is None  # duplicate key -> no duplicate row


def test_process_outbox_delivers_queued_email(db_session, monkeypatch):
    """process_outbox delivers queued rows and marks them sent."""
    from app.services.mail import queue_email, process_outbox, _send_smtp
    sent = []

    def fake_send(to, subject, body_text):
        sent.append((to, subject, body_text))

    monkeypatch.setattr("app.services.mail._send_smtp", fake_send)

    row = queue_email(
        db_session,
        to="a@b.com",
        subject="Hello",
        body_text="Your query is resolved.",
        idempotency_key="resolved:test-2",
    )
    assert row.status == "queued"

    count = process_outbox(db_session)
    assert count == 1
    assert len(sent) == 1
    assert sent[0][0] == "a@b.com"

    db_session.refresh(row)
    assert row.status == "sent"
    assert row.sent_at is not None


def test_process_outbox_marks_failed_on_smtp_error(db_session, monkeypatch):
    """process_outbox records failure, increments attempts, keeps going."""
    from app.services.mail import queue_email, process_outbox, _send_smtp

    def boom(to, subject, body_text):
        raise ConnectionError("mail server down")

    monkeypatch.setattr("app.services.mail._send_smtp", boom)

    row = queue_email(
        db_session,
        to="a@b.com",
        subject="S",
        body_text="B",
        idempotency_key="resolved:test-3",
    )
    assert row.status == "queued"

    count = process_outbox(db_session)
    assert count == 0  # no successful sends (SMTP down)

    db_session.refresh(row)
    assert row.status == "failed"
    assert row.attempts == 1  # one attempt was made and recorded
    assert "ConnectionError" in row.error


# --- Step 4: feedback endpoint ---------------------------------------------

def test_feedback_endpoint_creates_and_is_idempotent(client, db_session):
    """Customer rates a resolved query; repeat submission returns existing."""
    q = SupportQuery(
        question="refund?", customer_email="a@b.com", status="resolved",
    )
    db_session.add(q)
    db_session.commit()
    db_session.refresh(q)

    r = client.patch(
        f"/queries/{q.id}/feedback",
        json={"rating": 5, "comment": "great"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["query_id"] == str(q.id)
    assert body["rating"] == 5
    assert body["comment"] == "great"

    # Duplicate submission returns the existing row (idempotent by construction)
    r2 = client.patch(
        f"/queries/{q.id}/feedback",
        json={"rating": 1, "comment": "terrible"},
    )
    assert r2.status_code == 200
    assert r2.json()["rating"] == 5  # unchanged from first submission


def test_feedback_409_on_unresolved_query(client, db_session):
    """Feedback is only accepted on resolved queries."""
    q = SupportQuery(
        question="refund?", customer_email="a@b.com", status="pending",
    )
    db_session.add(q)
    db_session.commit()
    db_session.refresh(q)

    r = client.patch(
        f"/queries/{q.id}/feedback",
        json={"rating": 5},
    )
    assert r.status_code == 409


# --- Step 4: admin PATCH to resolved sends no mail --------------------------

def test_admin_patch_resolved_no_mail(client, db_session):
    """Manual staff resolution (PATCH /queries/{id}->resolved) queues no mail."""
    from app.models import EmailOutbox

    q = SupportQuery(
        question="refund?", customer_email="a@b.com", status="pending",
    )
    db_session.add(q)
    db_session.commit()
    db_session.refresh(q)

    r = client.patch(
        f"/queries/{q.id}",
        json={"status": "resolved"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "resolved"

    # No email row was queued for a manual staff resolution
    rows = db_session.query(EmailOutbox).all()
    assert len(rows) == 0
