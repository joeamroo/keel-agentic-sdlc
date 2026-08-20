"""Stable JSON error shape and exception handlers.

Every error response uses the same body shape and never carries a stack trace, a
database message, a filesystem path or caller supplied markup.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

LOGGER = logging.getLogger("links.errors")

_STATUS_CODE_NAMES: Dict[int, str] = {
    400: "invalid_request",
    404: "not_found",
    405: "method_not_allowed",
    410: "link_expired",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "invalid_request",
    429: "rate_limited",
    500: "internal_error",
    502: "destination_not_routable",
    503: "service_unavailable",
}

_STATUS_MESSAGES: Dict[int, str] = {
    400: "The request could not be understood.",
    404: "No link exists for that code.",
    405: "That method is not allowed for this path.",
    410: "This link has expired.",
    413: "The request body is too large.",
    415: "The request media type is not supported.",
    422: "The request body is not valid.",
    429: "Too many requests; please retry later.",
    500: "An internal error occurred.",
    502: "The destination could not be served.",
    503: "The service is temporarily unavailable.",
}


class ApiError(Exception):
    """Application level error carrying an HTTP status, a machine readable code and a safe message."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        """Create an API error.

        Args:
            status_code: HTTP status to return.
            code: Stable machine readable error code.
            message: Static, caller safe message (never echoes user input).
            headers: Optional extra response headers, e.g. ``Retry-After``.

        Returns:
            None.

        Raises:
            Nothing.
        """
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers: Dict[str, str] = dict(headers) if headers else {}


def error_payload(code: str, message: str) -> Dict[str, str]:
    """Build the canonical error body.

    Args:
        code: Machine readable error code.
        message: Static human readable message.

    Returns:
        A dictionary with ``error``, ``code``, ``message`` and ``detail`` keys, all
        of which are safe to show to any caller.

    Raises:
        Nothing.
    """
    return {"error": code, "code": code, "message": message, "detail": message}


def error_response(
    status_code: int,
    code: str,
    message: str,
    headers: Optional[Mapping[str, str]] = None,
) -> JSONResponse:
    """Build a JSON error response using the canonical shape.

    Args:
        status_code: HTTP status to return.
        code: Machine readable error code.
        message: Static human readable message.
        headers: Optional extra headers merged into the response.

    Returns:
        A :class:`JSONResponse` with ``Cache-Control: no-store``.

    Raises:
        Nothing.
    """
    final_headers: Dict[str, str] = {"Cache-Control": "no-store"}
    if headers:
        final_headers.update({str(key): str(value) for key, value in headers.items()})
    return JSONResponse(status_code=status_code, content=error_payload(code, message), headers=final_headers)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers that convert every failure into the canonical error shape.

    Args:
        app: The FastAPI application to mutate.

    Returns:
        None.

    Raises:
        Nothing.
    """

    async def handle_api_error(_request: Request, exc: Exception) -> JSONResponse:
        """Render an :class:`ApiError` as JSON.

        Returns:
            The canonical error response for the raised error.

        Raises:
            Nothing.
        """
        error = exc if isinstance(exc, ApiError) else ApiError(500, "internal_error", _STATUS_MESSAGES[500])
        return error_response(error.status_code, error.code, error.message, error.headers)

    async def handle_validation_error(_request: Request, _exc: Exception) -> JSONResponse:
        """Render a request-model validation failure as a 400 without echoing input.

        Returns:
            A 400 ``invalid_request`` response.

        Raises:
            Nothing.
        """
        return error_response(400, "invalid_request", "The request body is not valid.")

    async def handle_http_exception(_request: Request, exc: Exception) -> JSONResponse:
        """Render Starlette HTTP exceptions (404, 405, ...) in the canonical shape.

        Returns:
            The canonical error response for the exception's status code.

        Raises:
            Nothing.
        """
        status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
        headers: Dict[str, Any] = {}
        if isinstance(exc, StarletteHTTPException) and exc.headers:
            headers = {str(k): str(v) for k, v in exc.headers.items()}
        code = _STATUS_CODE_NAMES.get(status, "http_error")
        message = _STATUS_MESSAGES.get(status, "The request could not be completed.")
        return error_response(status, code, message, headers)

    async def handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        """Render any unhandled exception as an opaque 500.

        Returns:
            A 500 ``internal_error`` response with no internal detail.

        Raises:
            Nothing.
        """
        LOGGER.error("unhandled error of type %s", type(exc).__name__)
        return error_response(500, "internal_error", _STATUS_MESSAGES[500])

    app.add_exception_handler(ApiError, handle_api_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected_error)
