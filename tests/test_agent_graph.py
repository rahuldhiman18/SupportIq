from types import SimpleNamespace

import pytest

from app.services.agent_graph import GraphExecutionError, run_support_graph
from app.services.ai_service import AITimeoutError


def test_graph_resolves_valid_answer():
    result = run_support_graph(
        "What is the refund window?",
        answer_fn=lambda *args, **kwargs: SimpleNamespace(
            intent="eligibility", answer_text="14 days, per policy."
        ),
    )

    assert result.outcome == "resolved"
    assert result.intent == "eligibility"
    assert result.answer_text == "14 days, per policy."


def test_graph_routes_unknown_intent_to_human():
    result = run_support_graph(
        "Something unusual happened",
        answer_fn=lambda *args, **kwargs: SimpleNamespace(
            intent="unknown", answer_text="I am not sure."
        ),
    )

    assert result.outcome == "needs_human"
    assert result.validation_error == "model returned an unknown intent"


def test_graph_routes_oversized_answer_to_human():
    result = run_support_graph(
        "Explain this",
        answer_fn=lambda *args, **kwargs: SimpleNamespace(
            intent="timing", answer_text="x" * 5001
        ),
    )

    assert result.outcome == "needs_human"
    assert "5000 character" in (result.validation_error or "")


def test_graph_routes_invalid_customer_input_to_human_without_provider_call():
    calls = 0

    def answer_fn(*args, **kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(intent="timing", answer_text="answer")

    result = run_support_graph("   ", answer_fn=answer_fn)

    assert result.outcome == "needs_human"
    assert result.validation_error == "customer question is missing"
    assert calls == 0


def test_graph_preserves_typed_provider_errors_for_worker_retry():
    def answer_fn(*args, **kwargs):
        raise AITimeoutError("provider timeout")

    with pytest.raises(AITimeoutError, match="provider timeout"):
        run_support_graph("Where is my order?", answer_fn=answer_fn)


def test_graph_wraps_unexpected_node_errors_without_leaking_details():
    def answer_fn(*args, **kwargs):
        raise RuntimeError("secret provider response")

    with pytest.raises(GraphExecutionError, match="support graph execution failed") as exc:
        run_support_graph("Where is my order?", answer_fn=answer_fn)

    assert "secret provider response" not in str(exc.value)
