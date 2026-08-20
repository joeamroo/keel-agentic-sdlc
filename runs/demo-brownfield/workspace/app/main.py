"""FastAPI application: link creation, redirects, stats and health.

Redirect destinations always come from the validated value stored in SQLite;
no query parameter, header or path segment of an incoming request can influence
a ``Location``.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import timedelta
from typing import Callable, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings, load_settings
from .db import MAX_TEXT_FIELD_LENGTH, connect, fetch_link, init_db, insert_link, record_click
from .errors import AppError, error_response
from .models import CreateLinkRequest, CreateLinkResponse, HealthResponse, StatsResponse
from .ratelimit import RateLimiterState, RateLimitMiddleware
from .urls import InvalidURLError, validate_target_url
from .utils import CODE_PATTERN, generate_code, to_rfc3339, truncate, utc_now

logger = logging.getLogger("app.main")

_HTTP_ERROR_CODES: Dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    410: "gone",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}


def configure_logging(level_name: str) -> None:
    """Apply the configured log level to the root and application loggers.

    Returns None. Raises nothing; an unknown level degrades to INFO.
    """
    level = getattr(logging, level_name.upper(), logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(level=level)
    logging.getLogger().setLevel(level)
    logging.getLogger("app").setLevel(level)


def _settings_of(request: Request) -> Settings:
    """Return the frozen settings snapshot attached to the running app.

    Raises AppError(500) when the application was not constructed correctly.
    """
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        raise AppError(500, "internal_error", "The service is not configured.")
    return settings


def _is_expired(expires_at: Optional[str], now_iso: str) -> bool:
    """Report whether a stored expiry timestamp has passed.

    Both values are fixed width RFC3339 UTC strings, so the lexicographic
    comparison is chronological. NULL means the link never expires. Returns the
    comparison result. Raises nothing.
    """
    if not expires_at:
        return False
    return expires_at <= now_iso


def create_app(
    settings: Optional[Settings] = None,
    clock: Optional[Callable[[], float]] = None,
) -> FastAPI:
    """Build the FastAPI application.

    ``settings`` defaults to a snapshot read from the process environment and
    ``clock`` to ``time.monotonic``; both exist so tests can drive the limiter
    deterministically. Returns the configured application. Raises sqlite3.Error
    or OSError when the database cannot be initialised.
    """
    active = settings if settings is not None else load_settings()
    configure_logging(active.log_level)
    init_db(active.db_path)

    app = FastAPI(
        title="URL Shortener",
        version="1.1.0",
        description="Short links with SSRF-hardened targets and per-key creation quotas.",
    )
    app.state.settings = active
    limiter = RateLimiterState(active, clock if clock is not None else time.monotonic)
    app.state.rate_limiter = limiter
    app.add_middleware(RateLimitMiddleware, state=limiter)

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        """Render an application error in the stable envelope.

        Returns the JSON error response. Raises nothing.
        """
        return error_response(exc.status_code, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Render a boundary validation failure as 422 validation_error.

        Only field locations and messages are echoed, never submitted values.
        Returns the JSON error response. Raises nothing.
        """
        details = []
        for error in exc.errors()[:5]:
            location = ".".join(
                str(part) for part in error.get("loc", ()) if part not in ("body",)
            )
            details.append(
                "%s: %s" % (location or "body", error.get("msg", "invalid value"))
            )
        message = "; ".join(details) or "The request body failed validation."
        return error_response(422, "validation_error", message)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Render framework HTTP errors in the stable envelope.

        Returns the JSON error response. Raises nothing.
        """
        code = _HTTP_ERROR_CODES.get(exc.status_code, "http_error")
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail else "Request failed."
        headers = getattr(exc, "headers", None)
        return error_response(exc.status_code, code, detail, headers)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """Render any unhandled error as a generic 500.

        The detail is logged server side only. Returns the JSON error response.
        Raises nothing.
        """
        logger.exception("unhandled error while serving a request")
        return error_response(500, "internal_error", "An unexpected error occurred.")

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health() -> HealthResponse:
        """Report process liveness without touching user data.

        Returns a small status document. Raises nothing.
        """
        return HealthResponse(
            status="ok", service="url-shortener", time=to_rfc3339(utc_now())
        )

    @app.post(
        "/api/links",
        response_model=CreateLinkResponse,
        status_code=201,
        tags=["links"],
    )
    def create_link(payload: CreateLinkRequest, request: Request) -> CreateLinkResponse:
        """Create a short link for a validated http(s) target.

        Returns the created link with its short URL and expiry. Raises AppError
        with 422 validation_error for out-of-range input, 400 invalid_url for a
        target that fails URL policy, and 503 code_generation_failed when no
        free short code could be allocated.
        """
        settings = _settings_of(request)
        raw_url = payload.url.strip()
        if len(raw_url) > settings.max_url_length:
            raise AppError(
                422,
                "validation_error",
                "url must be at most %d characters." % settings.max_url_length,
            )
        try:
            target_url = validate_target_url(raw_url, settings.max_url_length)
        except InvalidURLError as exc:
            raise AppError(400, "invalid_url", str(exc)) from exc

        if payload.ttl_seconds is not None:
            ttl = payload.ttl_seconds
            if ttl > settings.max_ttl_seconds:
                raise AppError(
                    422,
                    "validation_error",
                    "ttl_seconds must be at most %d." % settings.max_ttl_seconds,
                )
        else:
            ttl = min(settings.default_ttl_seconds, settings.max_ttl_seconds)

        created = utc_now()
        created_at = to_rfc3339(created)
        expires_at = to_rfc3339(created + timedelta(seconds=ttl)) if ttl > 0 else None

        code: Optional[str] = None
        with connect(settings.db_path) as conn:
            for _ in range(settings.code_max_attempts):
                candidate = generate_code(settings.code_length)
                try:
                    insert_link(conn, candidate, target_url, created_at, expires_at)
                except sqlite3.IntegrityError:
                    logger.warning("short code collision; retrying")
                    continue
                code = candidate
                break

        if code is None:
            raise AppError(
                503,
                "code_generation_failed",
                "Could not allocate a unique short code. Please retry.",
            )

        return CreateLinkResponse(
            code=code,
            short_url="%s/%s" % (settings.base_url, code),
            target_url=target_url,
            created_at=created_at,
            expires_at=expires_at,
        )

    @app.get(
        "/api/links/{code}/stats",
        response_model=StatsResponse,
        tags=["links"],
    )
    def link_stats(code: str, request: Request) -> StatsResponse:
        """Return click statistics for a stored link.

        Returns the stats document. Raises AppError with 404 not_found when the
        code is unknown or malformed and 410 link_expired when the link's TTL
        has passed.
        """
        settings = _settings_of(request)
        if not CODE_PATTERN.match(code):
            raise AppError(404, "not_found", "Short link not found.")
        now_iso = to_rfc3339(utc_now())
        with connect(settings.db_path) as conn:
            row = fetch_link(conn, code)
            if row is None:
                raise AppError(404, "not_found", "Short link not found.")
            if _is_expired(row["expires_at"], now_iso):
                raise AppError(410, "link_expired", "This short link has expired.")
            return StatsResponse(
                code=str(row["code"]),
                target_url=str(row["target_url"]),
                created_at=str(row["created_at"]),
                expires_at=row["expires_at"],
                click_count=int(row["click_count"]),
                last_clicked_at=row["last_clicked_at"],
            )

    @app.get("/{code}", include_in_schema=False)
    def follow(code: str, request: Request) -> RedirectResponse:
        """Redirect to the stored target for a short code.

        The destination is always the value validated at creation time and read
        back from the database. Returns a 307 redirect. Raises AppError with
        404 not_found for an unknown or malformed code and 410 link_expired for
        an expired link.
        """
        settings = _settings_of(request)
        if not CODE_PATTERN.match(code):
            raise AppError(404, "not_found", "Short link not found.")
        now = utc_now()
        now_iso = to_rfc3339(now)
        with connect(settings.db_path) as conn:
            row = fetch_link(conn, code)
            if row is None:
                raise AppError(404, "not_found", "Short link not found.")
            if _is_expired(row["expires_at"], now_iso):
                raise AppError(410, "link_expired", "This short link has expired.")
            target_url = str(row["target_url"])
            record_click(
                conn,
                int(row["id"]),
                now_iso,
                truncate(request.headers.get("referer"), MAX_TEXT_FIELD_LENGTH),
                truncate(request.headers.get("user-agent"), MAX_TEXT_FIELD_LENGTH),
            )
        return RedirectResponse(
            url=target_url,
            status_code=307,
            headers={"Cache-Control": "no-store"},
        )

    return app


app = create_app()
