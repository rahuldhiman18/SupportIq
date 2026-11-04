"""Public API error abstractions.

Routes raise domain-shaped errors; exception handlers convert them into a
stable client response without exposing internal exception details.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


logger = logging.getLogger(__name__)


class PublicAPIError(Exception):
    """An expected application error that is safe to describe to a client."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


class ClientError(PublicAPIError):
    """Base for caller-correctable validation, identity, or state errors."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int = 400,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            code=code,
            message=message,
            headers=headers,
        )


class ValidationError(ClientError):
    def __init__(self, message: str = "Request contains invalid fields") -> None:
        super().__init__(
            status_code=422,
            code="validation_error",
            message=message,
        )


class NotFoundError(ClientError):
    def __init__(self, resource: str = "Resource") -> None:
        super().__init__(
            status_code=404,
            code="not_found",
            message=f"{resource} not found",
        )


class ConflictError(ClientError):
    def __init__(self, message: str = "Request conflicts with current state") -> None:
        super().__init__(
            status_code=409,
            code="conflict",
            message=message,
        )


def public_error_payload(
    *,
    code: str,
    message: str,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Return the stable public envelope; never accept raw internal errors."""
    payload: dict[str, Any] = {
        "detail": message,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if fields:
        payload["error"]["fields"] = fields
    return payload


def _status_error_code(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        429: "rate_limited",
        502: "upstream_error",
        503: "temporarily_unavailable",
        504: "timeout",
    }.get(status_code, "request_failed")


def register_api_exception_handlers(app: FastAPI) -> None:
    """Register one policy for validation, HTTP errors, and unhandled failures."""

    @app.exception_handler(PublicAPIError)
    async def public_api_error_handler(
        request: Request,
        exc: PublicAPIError,
    ) -> JSONResponse:
        payload = public_error_payload(
            code=exc.code,
            message=exc.message,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=payload,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        fields: list[str] = []
        for item in exc.errors():
            location = item.get("loc", ())
            field = ".".join(str(part) for part in location if part != "body")
            if field and field not in fields:
                fields.append(field)
        message = "Request contains invalid fields"
        payload = public_error_payload(
            code="validation_error",
            message=message,
            fields=fields,
        )
        return JSONResponse(status_code=422, content=payload)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        if isinstance(exc.detail, str):
            message = exc.detail
        else:
            message = {
                401: "Authentication is required",
                403: "You do not have permission to perform this action",
                404: "Resource not found",
                429: "Too many requests",
            }.get(exc.status_code, "Request could not be completed")
        if exc.status_code == 404 and message.lower() in {"not found", "resource not found"}:
            message = "Resource not found"
        payload = public_error_payload(
            code=_status_error_code(exc.status_code),
            message=message,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=payload,
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        logger.exception(
            "unexpected API failure (%s)",
            type(exc).__name__,
            extra={"path": request.url.path, "method": request.method},
        )
        payload = public_error_payload(
            code="internal_error",
            message="An unexpected error occurred. Please try again.",
        )
        return JSONResponse(status_code=500, content=payload)
