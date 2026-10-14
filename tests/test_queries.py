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
from app.models import KnowledgeAnswer, KnowledgeAnswerVersion, SupportQuery

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
