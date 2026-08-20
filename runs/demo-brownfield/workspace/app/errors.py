"""Stable error envelope helpers.

Every error the service emits has the shape
``{"error": {"code": "...", "message": "..."}}``. No stack trace, database error
string or filesystem path ever reaches a client.
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional

from fastapi.responses import JSONResponse

MAX_MESSAGE_LENGTH = 500


class AppError(Exception):
    """Application level error carrying an HTTP status and a stable error code."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        """Create an error.

        ``status_code`` is the HTTP status, ``code`` the stable machine readable
        identifier, ``message`` a safe human readable description and
        ``headers`` optional extra response headers. Raises nothing.
        """
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message[:MAX_MESSAGE_LENGTH]
        self.headers: Optional[Dict[str, str]] = dict(headers) if headers else None


def error_payload(code: str, message: str) -> Dict[str, Dict[str, str]]:
    """Build the error envelope body.

    Returns the dictionary that is serialised as the JSON error response.
    Raises nothing.
    """
    return {"error": {"code": code, "message": message[:MAX_MESSAGE_LENGTH]}}


def error_response(
    status_code: int,
    code: str,
    message: str,
    headers: Optional[Mapping[str, str]] = None,
) -> JSONResponse:
    """Build a JSON error response using the stable envelope.

    Returns a :class:`JSONResponse` with the given status, envelope body and
    optional headers. Raises nothing.
    """
    return JSONResponse(
        status_code=status_code,
        content=error_payload(code, message),
        headers=dict(headers) if headers else None,
    )
