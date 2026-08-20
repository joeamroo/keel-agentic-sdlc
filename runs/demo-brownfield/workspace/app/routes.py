"""HTTP routes for the URL shortener."""

from __future__ import annotations

import logging
import secrets
from contextlib import closing
from datetime import timedelta
from typing import FrozenSet, Optional

from fastapi import APIRouter, Request, status
from fastapi.responses import RedirectResponse

from .config import Settings
from .db import connect, get_click_stats, get_link, insert_link, resolve_redirect
from .errors import ApiError
from .schemas import (
    CreateLinkRequest,
    HealthResponse,
    LinkCreatedResponse,
    LinkStatsResponse,
)
from .timeutil import is_expired, to_rfc3339, utc_now
from .urlvalidation import UrlValidationError, validate_target_url

LOGGER = logging.getLogger("shortener.routes")

router = APIRouter()

CODE_ALPHABET: str = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH: int = 7
MAX_CODE_ATTEMPTS: int = 10
MAX_PATH_CODE_LENGTH: int = 64
ALLOWED_CODE_CHARS: FrozenSet[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
RESERVED_CODES: FrozenSet[str] = frozenset(
    {"health", "api", "docs", "redoc", "openapi.json", "favicon.ico", "static", "metrics"}
)
NOT_FOUND_MESSAGE: str = "No such link."


def _settings(request: Request) -> Settings:
    """Fetch the configuration snapshot attached to the application.

    Args:
        request: The incoming request.

    Returns:
        The :class:`Settings` stored on ``app.state``.

    Raises:
        Nothing.
    """
    settings: Settings = request.app.state.settings
    return settings


def _is_valid_code(code: str) -> bool:
    """Report whether a path segment could be a short code.

    Args:
        code: Candidate short code taken from the URL path.

    Returns:
        ``True`` when the code has a plausible length and character set.

    Raises:
        Nothing.
    """
    if not code or len(code) > MAX_PATH_CODE_LENGTH:
        return False
    return all(char in ALLOWED_CODE_CHARS for char in code)


def _generate_code() -> str:
    """Generate a random, unpredictable short code.

    Returns:
        A 7-character code drawn from an unambiguous alphabet.

    Raises:
        Nothing.
    """
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    tags=["ops"],
    summary="Liveness probe",
)
def health() -> HealthResponse:
    """Report process liveness without reading user data.

    Returns:
        A :class:`HealthResponse` with status ``"ok"``.

    Raises:
        Nothing.
    """
    return HealthResponse(status="ok")


@router.post(
    "/api/links",
    response_model=LinkCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["links"],
    summary="Create a short link",
)
def create_link(payload: CreateLinkRequest, request: Request) -> LinkCreatedResponse:
    """Validate a target URL and store a new short link.

    Args:
        payload: Validated request body.
        request: The incoming request, used for configuration access.

    Returns:
        A :class:`LinkCreatedResponse` describing the stored link.

    Raises:
        ApiError: 422 when the URL is unsafe or malformed, 409 when a requested
            custom code is unavailable, 500 when no free code could be allocated.
    """
    settings = _settings(request)
    try:
        target_url = validate_target_url(payload.url)
    except UrlValidationError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc

    created_at = utc_now()
    expires_at = (
        created_at + timedelta(seconds=payload.expires_in_seconds)
        if payload.expires_in_seconds is not None
        else None
    )

    with closing(connect(settings.db_path)) as conn:
        if payload.code is not None:
            if payload.code.lower() in RESERVED_CODES:
                raise ApiError(409, "code_unavailable", "Short code is not available.")
            if not insert_link(conn, payload.code, target_url, created_at, expires_at):
                raise ApiError(409, "code_unavailable", "Short code is not available.")
            code = payload.code
        else:
            code = ""
            for _ in range(MAX_CODE_ATTEMPTS):
                candidate = _generate_code()
                if insert_link(conn, candidate, target_url, created_at, expires_at):
                    code = candidate
                    break
            if not code:
                raise ApiError(500, "internal_error", "Could not allocate a short code.")

    return LinkCreatedResponse(
        code=code,
        short_url=settings.base_url + "/" + code,
        target_url=target_url,
        created_at=to_rfc3339(created_at),
        expires_at=to_rfc3339(expires_at) if expires_at is not None else None,
    )


@router.get(
    "/api/links/{code}/stats",
    response_model=LinkStatsResponse,
    status_code=status.HTTP_200_OK,
    tags=["links"],
    summary="Read click statistics for a short link",
)
def link_stats(code: str, request: Request) -> LinkStatsResponse:
    """Return click statistics and expiry state for a short code.

    Args:
        code: The short code to describe.
        request: The incoming request, used for configuration access.

    Returns:
        A :class:`LinkStatsResponse`; ``expired`` is true for a lapsed link, which
        still reports its final click count.

    Raises:
        ApiError: 404 when no such code exists.
    """
    settings = _settings(request)
    if not _is_valid_code(code):
        raise ApiError(404, "not_found", NOT_FOUND_MESSAGE)
    now = utc_now()
    with closing(connect(settings.db_path)) as conn:
        row = get_link(conn, code)
        if row is None:
            raise ApiError(404, "not_found", NOT_FOUND_MESSAGE)
        click_count, last_clicked_at = get_click_stats(conn, code)
    expires_at: Optional[str] = row["expires_at"]
    return LinkStatsResponse(
        code=str(row["code"]),
        target_url=str(row["target_url"]),
        created_at=str(row["created_at"]),
        expires_at=expires_at,
        expired=is_expired(expires_at, now),
        click_count=click_count,
        last_clicked_at=last_clicked_at,
    )


@router.get(
    "/{code}",
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    response_class=RedirectResponse,
    include_in_schema=False,
)
def follow_link(code: str, request: Request) -> RedirectResponse:
    """Redirect a visitor to the stored destination and count the click.

    The destination comes only from the validated row stored at creation time; no
    part of the incoming request can influence it.

    Args:
        code: The short code taken from the path.
        request: The incoming request, used for configuration access.

    Returns:
        A 307 :class:`RedirectResponse` to the stored target URL.

    Raises:
        ApiError: 404 with an identical body for unknown and expired codes alike.
    """
    settings = _settings(request)
    if not _is_valid_code(code):
        raise ApiError(404, "not_found", NOT_FOUND_MESSAGE)
    now = utc_now()
    with closing(connect(settings.db_path)) as conn:
        target_url = resolve_redirect(conn, code, now)
    if target_url is None:
        raise ApiError(404, "not_found", NOT_FOUND_MESSAGE)
    return RedirectResponse(
        url=target_url,
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Cache-Control": "no-store"},
    )
