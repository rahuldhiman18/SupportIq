"""Structured JSON logging with correlation IDs for production observability.

Every log line is a single JSON object with request_id, endpoint, latency_ms,
and user context. Sensitive header values are redacted automatically.

Usage:
    from app.core.logging import setup_logging, request_id_var, endpoint_var
    setup_logging()   # call once at app import
"""

import contextvars
import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone

# Context vars survive async/await boundaries -- correlation ID stays with the
# request for its entire lifecycle.
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
endpoint_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "endpoint", default=None
)
user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "user_id", default=None
)
customer_email_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "customer_email", default=None
)

# Header names whose values must be redacted in logs.
SENSITIVE_HEADERS = {
    "authorization",
    "x-admin-key",
    "x-api-key",
    "cookie",
    "set-cookie",
    "proxy-authorization",
}

# Env var names whose values must be redacted if they appear in log messages.
SENSITIVE_ENV_KEYS = {
    "gemini_api_key",
    "jwt_secret_key",
    "admin_api_key",
    "smtp_password",
    "database_url",
}

SERVICE_NAME = "supportiq"
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "1.0.0")


def _redact_value(key: str, value: str) -> str:
    """Return the value as-is, or '[REDACTED]' if the key is sensitive."""
    if key.lower() in SENSITIVE_HEADERS or key.lower() in SENSITIVE_ENV_KEYS:
        return "[REDACTED]"
    return value


def _redact_message(msg: str) -> str:
    """Redact known sensitive patterns from a log message string."""
    import re

    # Redact Bearer tokens, API keys in URLs, etc.
    msg = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", msg, flags=re.I)
    msg = re.sub(r"(api[_-]?key|token|secret|password)=[^\s&]+", r"\1=[REDACTED]", msg, flags=re.I)
    return msg


class JSONFormatter(logging.Formatter):
    """Formats log records as a single JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "request_id": request_id_var.get(),
            "endpoint": endpoint_var.get(),
            "user_id": user_id_var.get(),
            "customer_email": customer_email_var.get(),
            "message": _redact_message(record.getMessage()),
            "module": record.name,
        }

        # Add latency if present (set by middleware).
        latency = record.__dict__.get("latency_ms")
        if latency is not None:
            log_obj["latency_ms"] = round(latency, 2)

        # Add exception info as a single escaped string.
        if record.exc_info:
            log_obj["traceback"] = "".join(traceback.format_exception(*record.exc_info))

        # Add extra fields passed via logger.info(..., extra={...}).
        for key, value in record.__dict__.items():
            if key in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "funcName", "lineno", "created", "msecs",
                "relativeCreated", "thread", "threadName", "processName",
                "process", "exc_info", "exc_text", "stack_info",
                "getMessage", "latency_ms",
            ):
                continue
            if not key.startswith("_"):
                # Skip None values so they serialize as JSON null (not "None" string).
                if value is None:
                    continue
                log_obj[key] = _redact_value(key, str(value))

        return json.dumps(log_obj, ensure_ascii=False, separators=(",", ":"))


def setup_logging(level: str | None = None) -> None:
    """Configure the root logger to emit JSON lines to stdout.

    Safe to call multiple times; removes existing handlers first.
    """
    log_level = getattr(logging, (level or os.getenv("LOG_LEVEL", "INFO")).upper(), logging.INFO)

    root = logging.getLogger()
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
    root.setLevel(log_level)

    # Silence noisy third-party loggers unless overridden.
    for noisy in ("uvicorn.access", "uvicorn.error", "sqlalchemy.engine", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_context() -> dict:
    """Return the current correlation context (for testing / manual use)."""
    return {
        "request_id": request_id_var.get(),
        "endpoint": endpoint_var.get(),
        "user_id": user_id_var.get(),
        "customer_email": customer_email_var.get(),
    }