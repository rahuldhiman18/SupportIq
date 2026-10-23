"""FastAPI middleware: correlation IDs, request timing, security headers, timeouts.

- X-Request-ID generated per request, propagated via contextvars to loggers.
- X-Request-ID echoed back in response headers.
- X-Process-Time header added (latency in ms).
- Security headers added to every response.
- TimeoutMiddleware wraps call_next with asyncio.wait_for; returns 504 on timeout.
"""

import asyncio
import logging
import os
import time
import uuid
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.logging import endpoint_var, request_id_var

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": "default-src 'self'",
}

REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))


class TimeoutMiddleware(BaseHTTPMiddleware):
    """Wrap every request with a hard timeout; return 504 on timeout.

    Outermost middleware so it wraps correlation, logging, and route handlers.
    A cancelled asyncio task may leave DB/Gemini work in flight, but the
    customer gets a fast 504 instead of a hanging connection.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            return await asyncio.wait_for(
                call_next(request), timeout=REQUEST_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            return JSONResponse(
                status_code=504,
                content={"status": "degraded", "error": "request timeout"},
            )


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Attach request ID, measure latency, add security headers."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Generate or accept incoming request ID.
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        token_req = request_id_var.set(request_id)

        # Capture endpoint.
        endpoint = f"{request.method} {request.url.path}"
        token_ep = endpoint_var.set(endpoint)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Reset context vars before re-raising so they don't leak.
            request_id_var.reset(token_req)
            endpoint_var.reset(token_ep)
            raise

        latency_ms = (time.perf_counter() - start) * 1000

        # Echo request ID back to client.
        response.headers["x-request-id"] = request_id
        response.headers["x-process-time"] = f"{latency_ms:.2f}"

        # Security headers.
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)

        # Reset context vars so they don't leak to the next request.
        request_id_var.reset(token_req)
        endpoint_var.reset(token_ep)

        return response


class LoggingMiddleware(BaseHTTPMiddleware):
    """Log every request with structured fields after completion.

    Captures correlation context at request start so the values are available
    in the `finally` block regardless of middleware ordering (context vars are
    reset by CorrelationMiddleware after the response is built).
    """

    logger = logging.getLogger("supportiq.access")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Capture correlation context now (before call_next may reset it).
        from app.core.logging import (
            customer_email_var,
            endpoint_var,
            request_id_var,
            user_id_var,
        )

        captured_request_id = request_id_var.get()
        captured_endpoint = endpoint_var.get()
        captured_user_id = user_id_var.get()
        captured_customer_email = customer_email_var.get()

        start = time.perf_counter()
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            status_code = 500
            raise
        finally:
            latency_ms = (time.perf_counter() - start) * 1000
            self.logger.info(
                "request completed",
                extra={
                    "latency_ms": latency_ms,
                    "status_code": status_code,
                    "method": request.method,
                    "path": request.url.path,
                    "client_host": request.client.host if request.client else None,
                    "request_id": captured_request_id,
                    "endpoint": captured_endpoint,
                    "user_id": captured_user_id,
                    "customer_email": captured_customer_email,
                },
            )
        return response