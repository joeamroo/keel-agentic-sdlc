"""Stable error envelope shared by every endpoint.

All failures are rendered as ``{"error": {"code": ..., "message": ...}}``.
No stack trace, database message or filesystem path is ever placed in a
response body.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from fastapi.responses import JSONResponse

MAX_MESSAGE_LENGTH = 400


class ApiError(Exception):
    """Application level error carrying an HTTP status and a stable code."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        """Create an API error.

        ``status_code`` is the HTTP status to return, ``code`` the stable
        machine readable identifier, ``message`` a human readable, non
        sensitive explanation and ``headers`` optional extra response headers.
        Returns nothing. Raises nothing.
        """
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message[:MAX_MESSAGE_LENGTH]
        self.headers: Dict[str, str] = dict(headers or {})


def error_payload(code: str, message: str) -> Dict[str, Any]:
    """Build the canonical error body.

    Returns a dict of the shape ``{"error": {"code": ..., "message": ...}}``.
    Raises nothing.
    """
    return {"error": {"code": code, "message": message[:MAX_MESSAGE_LENGTH]}}


def error_response(
    status_code: int,
    code: str,
    message: str,
    headers: Optional[Mapping[str, str]] = None,
) -> JSONResponse:
    """Build a JSON error response using the canonical envelope.

    Returns a :class:`fastapi.responses.JSONResponse` with ``status_code`` and
    the given headers. Raises nothing.
    """
    return JSONResponse(
        status_code=status_code,
        content=error_payload(code, message),
        headers=dict(headers or {}),
    )
