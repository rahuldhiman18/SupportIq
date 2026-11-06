import ssl

import pytest

import app.services.mail as mail


def test_starttls_is_used_for_configured_credentials(monkeypatch):
    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls["connect"] = (host, port, timeout)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self, context):
            calls["starttls_context"] = context

        def login(self, user, password):
            calls["login"] = (user, password)

        def send_message(self, message):
            calls["from"] = message["From"]
            calls["to"] = message["To"]

    monkeypatch.setattr(mail, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(mail, "SMTP_PORT", 587)
    monkeypatch.setattr(mail, "SMTP_USER", "supportiq")
    monkeypatch.setattr(mail, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(mail, "SMTP_USE_TLS", True)
    monkeypatch.setattr(mail, "SMTP_USE_SSL", False)
    monkeypatch.setattr(mail, "SMTP_REQUIRE_TLS", True)
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)

    mail._send_smtp("customer@example.com", "Subject", "Body")

    assert calls["connect"] == ("smtp.example.com", 587, 10)
    assert isinstance(calls["starttls_context"], ssl.SSLContext)
    assert calls["login"] == ("supportiq", "secret")


def test_smtps_uses_verified_ssl_context(monkeypatch):
    calls = {}

    class FakeSMTPSSL:
        def __init__(self, host, port, timeout, context):
            calls["connect"] = (host, port, timeout)
            calls["context"] = context

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def login(self, user, password):
            calls["login"] = (user, password)

        def send_message(self, message):
            calls["message"] = message

    monkeypatch.setattr(mail, "SMTP_USE_TLS", False)
    monkeypatch.setattr(mail, "SMTP_USE_SSL", True)
    monkeypatch.setattr(mail, "SMTP_REQUIRE_TLS", True)
    monkeypatch.setattr(mail, "SMTP_USER", "supportiq")
    monkeypatch.setattr(mail, "SMTP_PASSWORD", "")
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTPSSL)

    mail._send_smtp("customer@example.com", "Subject", "Body")

    assert calls["context"].verify_mode == ssl.CERT_REQUIRED
    assert calls["login"] == ("supportiq", "")


def test_production_rejects_unencrypted_smtp(monkeypatch):
    monkeypatch.setattr(mail, "SMTP_USE_TLS", False)
    monkeypatch.setattr(mail, "SMTP_USE_SSL", False)
    monkeypatch.setattr(mail, "SMTP_REQUIRE_TLS", True)

    with pytest.raises(RuntimeError, match="TLS is required"):
        mail._send_smtp("customer@example.com", "Subject", "Body")
