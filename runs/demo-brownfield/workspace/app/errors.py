"""Stable error envelope and exception handlers.

Every error response emitted by the service has the shape
``{"error": {"code": ..., "message": ...}}``.  Handlers never leak stack
traces, database error strings or filesystem paths.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("shortener.errors")

_STATUS_CODES: Mapping[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "invalid_request",
    429: "rate_limited",
    500: "internal_error",
    503: "unavailable",
}

_STATUS_MESSAGES: Mapping[int, str] = {
    400: "The request could not be understood.",
    404: "Not found.",
    405: "Method not allowed.",
    422: "The request payload is invalid.",
    429: "Rate limit exceeded. Please retry later.",
    500: "An internal error occurred.",
    503: "The service is temporarily unavailable.",
}


class ApiError(Exception):
    """Application level error carrying an HTTP status and envelope code."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        """Create an API error.

        Stores the HTTP status, the stable envelope code and a safe,
        client-facing message.  Raises nothing.
        """
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_body(code: str, message: str) -> Dict[str, Any]:
    """Build the stable error envelope body.

    Returns a JSON-serialisable dict of the form
    ``{"error": {"code": code, "message": message}}``.  Raises nothing.
    """
    return {"error": {"code": code, "message": message}}


def error_response(
    status_code: int,
    code: str,
    message: str,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    """Build a JSONResponse carrying the stable error envelope.

    Returns the response with ``Cache-Control: no-store`` always set plus any
    extra headers supplied.  Raises nothing.
    """
    final_headers: Dict[str, str] = {"Cache-Control": "no-store"}
    if headers:
        final_headers.update(headers)
    return JSONResponse(status_code=status_code, content=error_body(code, message), headers=final_headers)


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """Render an :class:`ApiError` in the stable envelope.

    Returns a JSONResponse with the error's status code.  Raises nothing.
    """
    del request
    return error_response(exc.status_code, exc.code, exc.message)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Render Starlette/FastAPI HTTP exceptions in the stable envelope.

    Returns a JSONResponse whose envelope code is derived from the status code.
    Raises nothing.
    """
    del request
    code = _STATUS_CODES.get(exc.status_code, "error")
    message = _STATUS_MESSAGES.get(exc.status_code)
    if message is None:
        detail = exc.detail
        message = detail if isinstance(detail, str) and detail else "Request failed."
    headers: Dict[str, str] = {}
    if exc.headers:
        for name, value in exc.headers.items():
            headers[str(name)] = str(value)
    return error_response(exc.status_code, code, message, headers=headers)


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Render request validation failures in the stable envelope.

    Returns a 422 JSONResponse with a generic message; field level details are
    deliberately not echoed back.  Raises nothing.
    """
    del request, exc
    return error_response(422, "invalid_request", _STATUS_MESSAGES[422])


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render an unexpected exception as a generic 500.

    Returns a 500 JSONResponse with no internal detail.  Raises nothing.
    """
    del request
    logger.error("Unhandled error of type %s", type(exc).__name__)
    return error_response(500, "internal_error", _STATUS_MESSAGES[500])


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the stable-envelope exception handlers to an application.

    Returns ``None``.  Raises nothing.
    """
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
