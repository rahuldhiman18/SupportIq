from app.core.privacy import sanitize_for_ai
from fastapi import HTTPException

from app.core.security import require_customer_identity


def test_sanitize_for_ai_removes_direct_identifiers_without_redacting_dates():
    text = "Email me at customer@example.com or call +91 98765 43210 about ORD-9001 on 2026-10-01."

    sanitized = sanitize_for_ai(text)

    assert "customer@example.com" not in sanitized
    assert "+91 98765 43210" not in sanitized
    assert "ORD-9001" not in sanitized
    assert "2026-10-01" in sanitized
    assert "[REDACTED_EMAIL]" in sanitized
    assert "[REDACTED_PHONE]" in sanitized
    assert "[ORDER_REFERENCE]" in sanitized


def test_production_customer_data_requires_jwt(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")

    try:
        require_customer_identity(None)
    except HTTPException as exc:
        assert exc.status_code == 401
        assert exc.headers["WWW-Authenticate"] == "Bearer"
    else:
        raise AssertionError("anonymous production access must be rejected")
