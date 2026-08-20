"""FastAPI application for the link shortener.

Routes:

* ``POST /api/links``               - create a short link (validated target)
* ``GET  /{code}``                  - 307 redirect to the stored target
* ``GET  /api/links/{code}/stats``  - click statistics for one code
* ``GET  /health``                  - liveness, touches no user data
"""

from __future__ import annotations

import logging
import re
import secrets
import sqlite3
import string
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings, configure_logging, load_settings
from .db import (
    connect,
    fetch_click_stats,
    fetch_link_by_code,
    init_db,
    insert_link,
    record_click,
)
from .errors import ApiError, default_error_message, error_payload, status_error_code
from .models import (
    CreateLinkRequest,
    HealthResponse,
    LinkCreatedResponse,
    LinkStatsResponse,
)
from .ratelimit import RateLimiter, RateLimitMiddleware
from .timeutils import now_utc, parse_rfc3339, to_rfc3339
from .urls import UrlValidationError, validate_target_url

LOGGER = logging.getLogger("app.main")

CODE_ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase
CODE_PATTERN = re.compile(r"^[0-9A-Za-z]{1,64}$")
NO_STORE = {"Cache-Control": "no-store"}


def generate_code(length: int) -> str:
    """Generate a random base62 short code.

    Returns a string of ``length`` characters drawn from a CSPRNG.
    Raises nothing.
    """
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(max(1, length)))


def _is_expired(expires_at: Optional[str], reference: datetime) -> bool:
    """Decide whether a stored expiry timestamp has passed.

    Returns ``False`` when ``expires_at`` is ``None`` (never expires), ``True``
    when the stored instant is at or before ``reference`` or when the stored
    value cannot be parsed (fail closed).  Raises nothing.
    """
    if expires_at is None:
        return False
    try:
        expiry = parse_rfc3339(str(expires_at))
    except ValueError:
        LOGGER.warning("stored expiry timestamp is unparsable; treating link as expired")
        return True
    return expiry <= reference


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Build and configure the FastAPI application.

    Reads configuration from the environment when ``settings`` is not given,
    initialises the SQLite schema, installs the rate limiting middleware and
    registers every route and error handler.
    Returns the application instance.
    Raises :class:`OSError` or :class:`sqlite3.Error` when the database cannot
    be created.
    """
    active = settings if settings is not None else load_settings()
    configure_logging(active.log_level)
    init_db(active.db_path)

    limiter = RateLimiter(
        enabled=active.rate_limit_enabled,
        window_seconds=active.rate_limit_window_seconds,
        create_max=active.rate_limit_max,
        redirect_multiplier=active.rate_limit_redirect_multiplier,
        api_key_entries=active.api_key_entries,
    )

    app = FastAPI(title="link-shortener", version="1.0.0")
    app.state.settings = active
    app.state.limiter = limiter
    app.state.rate_limiter = limiter
    app.add_middleware(RateLimitMiddleware, limiter=limiter)

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        """Render an :class:`ApiError` in the standard envelope.

        Returns a JSON response with the error's status code.  Raises nothing.
        """
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.code, exc.message),
            headers=dict(NO_STORE),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Render framework HTTP errors (404, 405, ...) in the standard envelope.

        Returns a JSON response with the exception's status code.  Raises nothing.
        """
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail else None
        message = detail or default_error_message(exc.status_code)
        headers = dict(NO_STORE)
        if exc.headers:
            headers.update(exc.headers)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(status_error_code(exc.status_code), message),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Render request validation failures without echoing internals.

        Returns a 422 JSON response in the standard envelope.  Raises nothing.
        """
        return JSONResponse(
            status_code=422,
            content=error_payload(
                "invalid_request", "The request body or parameters failed validation."
            ),
            headers=dict(NO_STORE),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """Render an unexpected failure as a generic 500.

        Returns a JSON response carrying no stack trace, database string or
        filesystem path.  Raises nothing.
        """
        LOGGER.error("unhandled error of type %s", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content=error_payload("internal_error", "An internal error occurred."),
            headers=dict(NO_STORE),
        )

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health() -> HealthResponse:
        """Report process liveness.

        Returns the literal status ``ok`` and the current UTC time; no user
        data and no database connection is involved.  Raises nothing.
        """
        return HealthResponse(status="ok", time=to_rfc3339(now_utc()))

    @app.post(
        "/api/links",
        response_model=LinkCreatedResponse,
        status_code=201,
        tags=["links"],
    )
    def create_link(payload: CreateLinkRequest) -> LinkCreatedResponse:
        """Create a short link for a validated target URL.

        Returns the created code, its absolute short URL, the stored target and
        the creation/expiry timestamps.
        Raises :class:`ApiError` 400 when the URL is not an acceptable public
        http/https target, 422 when the requested TTL exceeds the configured
        maximum, and 503 when no unique code could be allocated or the database
        is unavailable.
        """
        try:
            target = validate_target_url(payload.url)
        except UrlValidationError as exc:
            raise ApiError(400, "invalid_url", str(exc)) from exc

        ttl = payload.expires_in_seconds
        if ttl is not None and ttl > active.max_ttl_seconds:
            raise ApiError(
                422,
                "invalid_request",
                "expires_in_seconds must be at most %d." % active.max_ttl_seconds,
            )

        created = now_utc()
        created_text = to_rfc3339(created)
        expires_text = (
            to_rfc3339(created + timedelta(seconds=ttl)) if ttl is not None else None
        )

        chosen_code = ""
        try:
            with connect(active.db_path) as conn:
                for _ in range(active.code_max_attempts):
                    candidate = generate_code(active.code_length)
                    try:
                        insert_link(conn, candidate, target, created_text, expires_text)
                    except sqlite3.IntegrityError:
                        continue
                    chosen_code = candidate
                    break
        except sqlite3.Error as exc:
            LOGGER.error("database failure creating a link: %s", type(exc).__name__)
            raise ApiError(
                503, "service_unavailable", "The service is temporarily unavailable."
            ) from exc

        if not chosen_code:
            LOGGER.warning("exhausted short code attempts while creating a link")
            raise ApiError(
                503,
                "service_unavailable",
                "Could not allocate a unique short code; please retry.",
            )

        return LinkCreatedResponse(
            code=chosen_code,
            short_url="%s/%s" % (active.base_url, chosen_code),
            url=target,
            created_at=created_text,
            expires_at=expires_text,
        )

    @app.get(
        "/api/links/{code}/stats", response_model=LinkStatsResponse, tags=["links"]
    )
    def link_stats(code: str) -> LinkStatsResponse:
        """Report click statistics for one short code.

        Returns the stored target, timestamps, whether the link has expired and
        the click totals.  Expiry is reported here (the owner's view) rather
        than hidden.
        Raises :class:`ApiError` 404 when the code does not exist and 503 when
        the database is unavailable.
        """
        if not CODE_PATTERN.match(code):
            raise ApiError(404, "not_found", "No link exists for that code.")
        try:
            with connect(active.db_path) as conn:
                row = fetch_link_by_code(conn, code)
                if row is None:
                    raise ApiError(404, "not_found", "No link exists for that code.")
                total, last_clicked = fetch_click_stats(conn, int(row["id"]))
        except sqlite3.Error as exc:
            LOGGER.error("database failure reading stats: %s", type(exc).__name__)
            raise ApiError(
                503, "service_unavailable", "The service is temporarily unavailable."
            ) from exc

        expires_at = row["expires_at"]
        return LinkStatsResponse(
            code=str(row["code"]),
            url=str(row["url"]),
            created_at=str(row["created_at"]),
            expires_at=str(expires_at) if expires_at is not None else None,
            expired=_is_expired(expires_at, now_utc()),
            clicks=total,
            last_clicked_at=last_clicked,
        )

    @app.get("/{code}", include_in_schema=False, tags=["links"])
    def follow_link(code: str) -> RedirectResponse:
        """Redirect to the stored, previously validated target.

        The destination is read from the database only; it never comes from a
        query parameter, header or path segment of this request.
        Returns a 307 redirect for a live link.
        Raises :class:`ApiError` 404 for an unknown or expired code (identical
        response either way) and 503 when the database is unavailable.
        """
        if not CODE_PATTERN.match(code):
            raise ApiError(404, "not_found", "No link exists for that code.")
        try:
            with connect(active.db_path) as conn:
                row = fetch_link_by_code(conn, code)
                if row is None:
                    raise ApiError(404, "not_found", "No link exists for that code.")
                if _is_expired(row["expires_at"], now_utc()):
                    raise ApiError(404, "not_found", "No link exists for that code.")
                target = str(row["url"])
                try:
                    record_click(conn, int(row["id"]), to_rfc3339(now_utc()))
                except sqlite3.Error:
                    LOGGER.warning("could not record a click; serving redirect anyway")
        except sqlite3.Error as exc:
            LOGGER.error("database failure serving a redirect: %s", type(exc).__name__)
            raise ApiError(
                503, "service_unavailable", "The service is temporarily unavailable."
            ) from exc

        return RedirectResponse(url=target, status_code=307, headers=dict(NO_STORE))

    return app


app = create_app()
