"""Stable JSON error envelope and the handlers that emit it.

Every error response has the shape ``{"error": {"code": ..., "message": ...}}`` and
never carries a stack trace, a database error string or a filesystem path.
"""

from __future__ import annotations

import logging
from typing import Dict, Mapping, Optional

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

LOGGER = logging.getLogger("shortener.errors")

STATUS_ERROR_CODES: Dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}


def error_payload(code: str, message: str) -> Dict[str, Dict[str, str]]:
    """Build the canonical error envelope.

    Args:
        code: Stable machine-readable error code.
        message: Human-readable, non-sensitive description.

    Returns:
        The envelope dictionary ready to be serialised as JSON.

    Raises:
        Nothing.
    """
    return {"error": {"code": code, "message": message}}


class ApiError(StarletteHTTPException):
    """HTTP exception carrying a stable error code for the JSON envelope."""

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        """Create an API error.

        Args:
            status_code: HTTP status to return.
            error_code: Stable code placed in ``error.code``.
            message: Safe message placed in ``error.message``.
            headers: Optional extra response headers.

        Returns:
            None.

        Raises:
            Nothing.
        """
        super().__init__(status_code=status_code, detail=message, headers=dict(headers or {}))
        self.error_code = error_code


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render any HTTP exception through the shared error envelope.

    Args:
        request: The incoming request (unused beyond logging context).
        exc: The raised exception; expected to be an HTTPException subclass.

    Returns:
        A :class:`JSONResponse` carrying the error envelope.

    Raises:
        Nothing.
    """
    if not isinstance(exc, StarletteHTTPException):
        return await unhandled_error_handler(request, exc)
    error_code = getattr(exc, "error_code", None) or STATUS_ERROR_CODES.get(exc.status_code, "error")
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail else "Request failed."
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(str(error_code), detail),
        headers=dict(exc.headers or {}),
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render request-model validation failures as a 422 envelope.

    Args:
        request: The incoming request (unused).
        exc: The raised :class:`RequestValidationError`.

    Returns:
        A 422 :class:`JSONResponse` with a generic, non-leaking message.

    Raises:
        Nothing.
    """
    if isinstance(exc, RequestValidationError):
        LOGGER.info("Rejected invalid request body or parameters for %s", request.url.path)
    return JSONResponse(
        status_code=422,
        content=error_payload("validation_error", "Request validation failed."),
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render an unexpected exception as a 500 envelope without leaking details.

    Args:
        request: The incoming request, used only for a server-side log line.
        exc: The unhandled exception.

    Returns:
        A 500 :class:`JSONResponse` with a fixed message.

    Raises:
        Nothing.
    """
    LOGGER.exception("Unhandled error while serving %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=error_payload("internal_error", "Internal server error."),
    )
