from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.models.tables import Order, OrderEvent
from app.services.business_policy import (
    POLICY_FAQ,
    evaluate_cancellation,
    evaluate_refund,
)


def _order(*, age_days=5, stage="delivered"):
    order = Order(
        order_number="ORD-POLICY",
        customer_email="customer@example.com",
        placed_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    )
    order.events = [OrderEvent(stage=stage, location="Test hub",
                               occurred_at=order.placed_at + timedelta(days=1))]
    return order


@pytest.mark.parametrize("index", range(60))
def test_policy_faq_matrix_contains_60_customer_questions(index):
    assert len(POLICY_FAQ) >= 60
    topic, question, answer = POLICY_FAQ[index]
    assert topic in {"refund", "cancel", "tracking"}
    assert question and answer


def test_refund_within_window_is_reviewable_not_payment_execution():
    decision = evaluate_refund(_order())
    assert decision.decision == "eligible_for_review"
    assert decision.requires_confirmation is True
    assert "automatically" in decision.customer_message


def test_refund_window_expired_is_not_eligible():
    assert evaluate_refund(_order(age_days=31)).reason_code == "refund_window_expired"


def test_shipped_refund_requires_human_review():
    decision = evaluate_refund(_order(stage="shipped"))
    assert decision.decision == "human_review"
    assert decision.requires_human is True


def test_cancellation_requires_confirmation_then_is_approved():
    assert evaluate_cancellation(_order(stage="placed")).decision == "confirmation_required"
    assert evaluate_cancellation(_order(stage="placed"), confirmed=True).decision == "approved_for_cancellation"


def test_delivered_order_cannot_be_cancelled():
    assert evaluate_cancellation(_order(stage="delivered")).reason_code == "already_delivered"


def test_policy_endpoints_are_customer_scoped(client):
    from tests.test_queries import _create_order_api

    assert _create_order_api(client, number="ORD-POLICY-API", email="a@b.com").status_code == 201
    refund = client.get("/orders/ORD-POLICY-API/refund-decision?email=a@b.com")
    cancel = client.get("/orders/ORD-POLICY-API/cancellation-decision?email=a@b.com")
    assert refund.status_code == 200
    assert cancel.status_code == 200
    assert refund.json()["policy_version"] == "refund-v1"
    assert cancel.json()["decision"] == "human_review"
    assert client.get("/orders/ORD-POLICY-API/refund-decision?email=other@example.com").status_code == 404


def test_ai_concurrency_guard_returns_typed_rate_limit(monkeypatch):
    import app.services.ai_service as service

    acquired = [service._AI_SLOTS.acquire(blocking=False)
                for _ in range(service.AI_CONCURRENCY_LIMIT)]
    assert all(acquired)
    try:
        with pytest.raises(service.AIRateLimitError):
            service._map_generate_errors("question", single_attempt=True)
    finally:
        for _ in acquired:
            service._AI_SLOTS.release()
