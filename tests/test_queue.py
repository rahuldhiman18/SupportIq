"""Unit tests for Option A queue transitions: retry budget, backoff, human handoff."""
from datetime import datetime, timedelta, timezone

from app.services.ai_service import (
    AIContentBlockedError,
    AIProviderError,
    AIRateLimitError,
    AITimeoutError,
)
from app.models import SupportQuery
from app.services.worker import (
    MAX_AI_ATTEMPTS,
    RETRY_BACKOFF_MINUTES,
    record_attempt_failure,
    record_success,
)


def _query(attempts=0, status="queued"):
    return SupportQuery(
        question="where is my refund?",
        customer_email="a@b.com",
        attempts=attempts,
        status=status,
    )


def test_budget_is_three_attempts_with_2_4_8_backoff():
    # The approved Option A constants: if these drift, quota safety drifts.
    assert MAX_AI_ATTEMPTS == 3
    assert RETRY_BACKOFF_MINUTES == (2, 4, 8)


def test_transient_failure_schedules_backoff():
    q = _query()
    assert record_attempt_failure(q, AITimeoutError("boom")) == "queued"
    assert q.attempts == 1
    assert q.next_retry_at is not None
    delta = q.next_retry_at - datetime.now(timezone.utc)
    # ~2 minutes from now (generous skew window for slow CI)
    assert timedelta(seconds=110) <= delta <= timedelta(minutes=2, seconds=30)
    assert "AITimeoutError" in q.last_error


def test_second_failure_backs_off_longer():
    q = _query(attempts=1)
    record_attempt_failure(q, AIRateLimitError("429"))
    assert q.attempts == 2
    delta = q.next_retry_at - datetime.now(timezone.utc)
    assert timedelta(minutes=3, seconds=50) <= delta <= timedelta(minutes=4, seconds=30)


def test_third_failure_hands_off_to_human():
    q = _query(attempts=2)
    assert record_attempt_failure(q, AIProviderError("500")) == "needs_human"
    assert q.attempts == 3
    assert q.next_retry_at is None
    assert q.locked_at is None
    assert "AIProviderError" in q.last_error


def test_content_blocked_never_burns_quota():
    # Safety filter will block every retry -- exactly one wasted call, then human.
    q = _query()
    assert record_attempt_failure(q, AIContentBlockedError("blocked")) == "needs_human"
    assert q.attempts == 1
    assert q.next_retry_at is None


def test_success_marks_resolved_and_clears_error_state():
    q = _query(status="processing")
    q.locked_at = datetime.now(timezone.utc)
    assert record_success(q, "timing") == "resolved"
    assert q.intent == "timing"
    assert q.next_retry_at is None
    assert q.locked_at is None
    assert q.last_error is None


# --- Option A quota-safety guards (promoted from bug-hunt probes) ------------

def test_once_path_makes_exactly_one_gemini_call():
    # Worker path must never burn more than ONE quota unit per attempt.
    from unittest.mock import patch
    import app.services.ai_service as ai_service
    from app.services.ai_service import answer_customer_question_once

    with patch.object(ai_service.client.models, "generate_content",
                      side_effect=TimeoutError("slow")) as m:
        try:
            answer_customer_question_once("q?")
        except AITimeoutError:
            pass
    assert m.call_count == 1


def test_sync_path_still_burns_up_to_three_calls():
    # Documents the deliberate split: sync request path keeps tenacity's
    # 3 retries; the queue worker path (above) is the quota-safe one.
    import httpx
    from unittest.mock import patch
    import app.services.ai_service as ai_service
    from app.services.ai_service import answer_customer_question

    with patch.object(ai_service.client.models, "generate_content",
                      side_effect=httpx.ConnectError("down")) as m:
        try:
            answer_customer_question("q?")
        except AITimeoutError:
            pass
    assert m.call_count == 3


def test_prompt_parity_sync_vs_once():
    # The 1-try and 3-try paths must send IDENTICAL prompts -- otherwise the
    # worker answers would drift from what customers see on the sync path.
    from unittest.mock import patch
    import app.services.ai_service as ai_service
    from app.services.ai_service import (
        answer_customer_question,
        answer_customer_question_once,
    )

    seen = []

    def capture(question, *, single_attempt):
        seen.append((question, single_attempt))
        return '{"intent": "timing", "answer": "ok"}'

    with patch.object(ai_service, "_map_generate_errors", side_effect=capture):
        answer_customer_question("q?", policy_context="P", situation="S")
        answer_customer_question_once("q?", policy_context="P", situation="S")

    (p_sync, sync_flag), (p_once, once_flag) = seen
    assert sync_flag is False and once_flag is True
    assert p_sync == p_once, "prompt drifted between sync and once paths"


def test_empty_answer_json_is_content_blocked():
    from types import SimpleNamespace
    from unittest.mock import patch
    import app.services.ai_service as ai_service
    from app.services.ai_service import answer_customer_question_once

    fake = SimpleNamespace(candidates=[SimpleNamespace()],
                           text='{"intent": "timing", "answer": ""}')
    with patch.object(ai_service.client.models, "generate_content",
                      return_value=fake):
        try:
            answer_customer_question_once("q?")
        except ai_service.AIContentBlockedError:
            return
    raise AssertionError("empty answer was not raised as AIContentBlockedError")
