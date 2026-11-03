"""Tests for application orchestration without Redis or Gemini."""

from app.application.services import AnswerGenerationService
from app.core.cache import CachedAnswer
from app.services.ai_service import CustomerAnswer


class FakeCache:
    enabled = True

    def __init__(self, value=None):
        self.value = value
        self.set_calls = 0
        self.released = False

    def get(self, key):
        return self.value

    def set(self, key, answer):
        self.value = answer
        self.set_calls += 1

    def acquire_lock(self, key):
        return "lock"

    def release_lock(self, key, token):
        self.released = True


def test_answer_generation_service_hides_cache_and_provider_details():
    cache = FakeCache()
    calls = []

    def answerer(question, *, policy_context=None, situation=None):
        calls.append((question, policy_context, situation))
        return CustomerAnswer(intent="eligibility", answer_text="Covered.")

    service = AnswerGenerationService(
        answerer=answerer,
        cache=cache,
        key_factory=lambda **kwargs: "answer-key",
    )

    result = service.generate(
        "Am I covered?",
        customer_email="customer@example.com",
        policy_context="Coverage policy",
        situation=None,
    )

    assert result == CachedAnswer(intent="eligibility", answer_text="Covered.")
    assert calls == [("Am I covered?", "Coverage policy", None)]
    assert cache.set_calls == 1
    assert cache.released is True


def test_answer_generation_service_uses_cached_answer_without_provider_call():
    cache = FakeCache(CachedAnswer(intent="refund_status", answer_text="Pending."))

    def unexpected_answerer(*args, **kwargs):
        raise AssertionError("provider should not be called on a cache hit")

    service = AnswerGenerationService(
        answerer=unexpected_answerer,
        cache=cache,
        key_factory=lambda **kwargs: "answer-key",
    )

    assert service.generate(
        "Where is my refund?",
        customer_email="customer@example.com",
        policy_context="Refund policy",
        situation=None,
    ) == cache.value
