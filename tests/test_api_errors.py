"""Public API error-envelope and validation regression tests."""


def test_validation_errors_hide_pydantic_internals(client):
    response = client.post(
        "/queries",
        json={"customer_email": "not-an-email", "question": ""},
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["detail"] == "Request contains invalid fields"
    assert payload["error"]["code"] == "validation_error"
    assert "customer_email" in payload["error"]["fields"]
    assert "not-an-email" not in payload["error"]["message"]
    assert "input" not in str(payload).lower()


def test_http_error_response_has_stable_detail_and_error(client):
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    payload = response.json()
    assert payload["detail"] == "Resource not found"
    assert payload["error"]["code"] == "not_found"


def test_public_api_error_envelope_supports_custom_errors():
    from app.core.api_errors import ConflictError, public_error_payload

    payload = public_error_payload(
        code=ConflictError().code,
        message=ConflictError().message,
    )
    assert payload["detail"] == "Request conflicts with current state"
    assert payload["error"]["code"] == "conflict"
