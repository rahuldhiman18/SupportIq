"""Unit tests for POST /queries and related routes (Layer 2).

All AI calls are MOCKED -- these tests are 100% offline (no API quota used).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import select

from app.ai_service import (
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
    assert mock_ai.call_args.kwargs["situation"] is None


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
    assert mock_ai.call_args.kwargs["situation"] is None  # no facts loaded


def test_duplicate_order_returns_409_not_500(client):
    assert _create_order_api(client, number="ORD-9004").status_code == 201
    response = _create_order_api(client, number="ORD-9004")
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_live_facts_instruction_outranks_policy_boilerplate():
    from app.ai_service import _INTENT_PROMPT
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
