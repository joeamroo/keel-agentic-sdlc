"""One stable error envelope for every failure the service reports.

Error bodies always look like ``{"error": {"code": ..., "message": ...}}`` and
never contain a stack trace, a database error string or a filesystem path.
"""

from __future__ import annotations

from typing import Dict

_STATUS_TO_CODE: Dict[int, str] = {
    400: "invalid_request",
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
    503: "service_unavailable",
}

_DEFAULT_MESSAGES: Dict[int, str] = {
    400: "The request was invalid.",
    404: "The requested resource does not exist.",
    405: "The HTTP method is not allowed for this resource.",
    422: "The request body failed validation.",
    429: "Rate limit exceeded. Please retry later.",
    500: "An internal error occurred.",
    503: "The service is temporarily unavailable.",
}


class ApiError(Exception):
    """An error that maps directly onto the public error envelope."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        """Create an API error.

        Stores the HTTP status, the stable machine readable code and a safe
        human readable message.  Raises nothing.
        """
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def status_error_code(status_code: int) -> str:
    """Map an HTTP status code onto a stable error code.

    Returns the mapped code, or 'error' for statuses without a mapping.
    Raises nothing.
    """
    return _STATUS_TO_CODE.get(status_code, "error")


def default_error_message(status_code: int) -> str:
    """Return a safe default message for an HTTP status code.

    Returns a generic sentence that leaks no internal detail.  Raises nothing.
    """
    return _DEFAULT_MESSAGES.get(status_code, "The request could not be completed.")


def error_payload(code: str, message: str) -> Dict[str, Dict[str, str]]:
    """Build the JSON-serialisable error envelope.

    Returns ``{"error": {"code": code, "message": message}}``.  Raises nothing.
    """
    return {"error": {"code": code, "message": message}}
