"""FastAPI application for the URL shortener service.

Endpoints:
    POST /api/links               create a short link
    GET  /api/links/{code}/stats  per-link click analytics
    GET  /health                  liveness probe
    GET  /{code}                  redirect to the stored destination

The redirect destination always comes from the validated, stored row; it can
never come from a query parameter, header or path segment of the request.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.parse import quote

from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import codes as codes_module
from .config import Settings
from .db import get_connection, init_db
from .errors import ApiError, error_response
from .models import CreateLinkRequest, CreateLinkResponse, HealthResponse, StatsResponse
from .ratelimit import RateLimitMiddleware
from .validation import validate_destination_url

logger = logging.getLogger("links")

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
MAX_HEADER_STORE_LENGTH = 2048
_LOCATION_SAFE_CHARACTERS = ":/?#[]@!$&'()*+,;=%~-._"


class ClientAddressRedactionFilter(logging.Filter):
    """Logging filter that removes the client address from access log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Replace the client address argument of an access log record.

        ``record`` is the log record being emitted. Returns ``True`` so the
        record is always kept, after blanking the first positional argument
        which uvicorn uses for the client address. Raises nothing.
        """
        if isinstance(record.args, tuple) and record.args:
            record.args = ("-",) + tuple(record.args[1:])
        return True


def configure_logging(level: str) -> None:
    """Configure root logging and strip client addresses from access logs.

    ``level`` is a logging level name such as ``INFO``. Returns nothing. Raises
    nothing.
    """
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger().setLevel(numeric)
    access_logger = logging.getLogger("uvicorn.access")
    if not any(
        isinstance(existing, ClientAddressRedactionFilter)
        for existing in access_logger.filters
    ):
        access_logger.addFilter(ClientAddressRedactionFilter())


def format_timestamp(value: datetime) -> str:
    """Render an aware datetime in the fixed stored UTC format.

    ``value`` is a timezone aware datetime (naive input is treated as UTC).
    Returns a string like ``2024-01-31T12:00:00.000000Z``. Raises nothing.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_timestamp(value: str) -> datetime:
    """Parse a stored timestamp back into an aware UTC datetime.

    ``value`` is a stored timestamp string. Returns an aware UTC datetime.
    Raises :class:`ValueError` when the string cannot be parsed.
    """
    try:
        return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_client_expiry(raw: str) -> datetime:
    """Parse a client supplied ISO-8601 expiry into an aware UTC datetime.

    ``raw`` is the ``expires_at`` value from the request body. Returns the
    instant normalized to UTC. Raises :class:`app.errors.ApiError` with status
    400 and code ``invalid_expiry`` when the value is not a valid ISO-8601
    timestamp.
    """
    text = raw.strip()
    if not text:
        raise ApiError(
            400, "invalid_expiry", "expires_at must be a valid ISO-8601 timestamp."
        )
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise ApiError(
            400, "invalid_expiry", "expires_at must be a valid ISO-8601 timestamp."
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def generate_code(length: int) -> str:
    """Generate a CSPRNG short code.

    ``length`` is the number of base62 characters. Returns the generated code.
    Raises :class:`ValueError` when ``length`` is not positive.
    """
    return codes_module.generate_code(length)


def clip_header(value: Optional[str]) -> Optional[str]:
    """Truncate a header value for storage.

    ``value`` is a header value or ``None``. Returns the value truncated to
    2048 characters, or ``None`` when the header was absent. Raises nothing.
    """
    if value is None:
        return None
    return value[:MAX_HEADER_STORE_LENGTH]


def location_header_value(url: str) -> str:
    """Render a stored URL as a Location header value.

    ``url`` is the stored destination. Returns the URL byte-for-byte when it is
    ASCII, otherwise a percent-encoded form so the header stays transmittable.
    Raises nothing.
    """
    if url.isascii():
        return url
    return quote(url, safe=_LOCATION_SAFE_CHARACTERS)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Build the FastAPI application.

    ``settings`` overrides the environment derived configuration when given.
    Returns a fully wired :class:`fastapi.FastAPI` instance with the schema
    created. Raises nothing; a failure to create the schema at build time is
    logged and retried at startup and on each request.
    """
    resolved = settings if settings is not None else Settings.from_env()
    configure_logging(resolved.log_level)

    try:
        init_db(resolved.db_path)
    except (sqlite3.Error, OSError):
        logger.warning("Database initialisation deferred to startup", exc_info=True)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Create the schema at startup.

        Yields control to the running application. Raises nothing; database
        problems are logged and surfaced by the health endpoint.
        """
        try:
            init_db(resolved.db_path)
        except (sqlite3.Error, OSError):
            logger.error("Database initialisation failed at startup", exc_info=True)
        yield

    app = FastAPI(
        title="URL Shortener",
        version="1.0.0",
        description="Short links with expiry and IP-free click analytics.",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.code_pattern = re.compile(r"^[A-Za-z0-9]{%d}$" % resolved.code_length)
    app.add_middleware(RateLimitMiddleware, settings=resolved)

    # ---------------------------------------------------------------- errors

    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        """Render an :class:`ApiError` using the stable envelope.

        Returns the JSON error response with the error's status and headers.
        Raises nothing.
        """
        return error_response(exc.status_code, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Render a request validation failure as 422 ``validation_error``.

        Returns the JSON error response. Raises nothing.
        """
        details: List[str] = []
        for error in exc.errors()[:5]:
            location = ".".join(
                str(part) for part in error.get("loc", ()) if part != "body"
            )
            details.append(f"{location or 'body'}: {error.get('msg', 'invalid value')}")
        message = "Request validation failed."
        if details:
            message = f"{message} {'; '.join(details)}"
        return error_response(422, "validation_error", message)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        _: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Render framework HTTP exceptions using the stable envelope.

        Returns the JSON error response. Raises nothing.
        """
        if exc.status_code == 404:
            code = "not_found"
            message = "The requested resource does not exist."
        elif exc.status_code == 405:
            code = "method_not_allowed"
            message = "The HTTP method is not allowed for this resource."
        else:
            code = "http_error"
            message = "The request could not be completed."
        headers = dict(getattr(exc, "headers", None) or {})
        return error_response(exc.status_code, code, message, headers)

    @app.exception_handler(sqlite3.Error)
    async def handle_database_error(_: Request, __: sqlite3.Error) -> JSONResponse:
        """Render a database failure as 503 without leaking driver detail.

        Returns the JSON error response. Raises nothing.
        """
        logger.error("Database error while handling request", exc_info=True)
        return error_response(
            503,
            "service_unavailable",
            "The service is temporarily unable to handle the request.",
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, __: Exception) -> JSONResponse:
        """Render an unexpected failure as a generic 500.

        Returns the JSON error response with no internal detail. Raises nothing.
        """
        logger.error("Unhandled error while handling request", exc_info=True)
        return error_response(
            500, "internal_error", "An unexpected error occurred."
        )

    # ---------------------------------------------------------------- routes

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health(request: Request) -> JSONResponse:
        """Report liveness without reading any user data.

        Returns ``{"status": "ok"}`` with 200 when SQLite answers ``SELECT 1``,
        or ``{"status": "degraded"}`` with 503 otherwise. Raises nothing.
        """
        current: Settings = request.app.state.settings
        try:
            with get_connection(current.db_path) as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception:  # noqa: BLE001 - health must never raise
            logger.warning("Health check failed", exc_info=True)
            return JSONResponse(status_code=503, content={"status": "degraded"})
        return JSONResponse(status_code=200, content={"status": "ok"})

    @app.post(
        "/api/links",
        status_code=201,
        response_model=CreateLinkResponse,
        tags=["links"],
    )
    def create_link(payload: CreateLinkRequest, request: Request) -> Dict[str, Any]:
        """Create a short link for a validated public http(s) destination.

        ``payload`` is the validated request body. Returns the created link
        representation (code, short_url, url, created_at, expires_at). Raises
        :class:`ApiError` 422 ``validation_error`` for an over-long or blank
        url, 400 ``unsupported_scheme`` / ``invalid_url`` / ``blocked_destination``
        for a rejected destination, 400 ``invalid_expiry`` for a bad expiry, and
        503 ``code_generation_failed`` when no unique code could be allocated.
        """
        current: Settings = request.app.state.settings
        url = payload.url
        if len(url) > current.max_url_length:
            raise ApiError(
                422,
                "validation_error",
                f"url must be at most {current.max_url_length} characters.",
            )
        if not url.strip():
            raise ApiError(422, "validation_error", "url must not be empty.")

        validate_destination_url(url, dns_enabled=current.dns_resolution_enabled)

        now = datetime.now(timezone.utc)
        if payload.expires_at is None:
            expires = now + timedelta(days=current.default_ttl_days)
        else:
            expires = parse_client_expiry(payload.expires_at)
            if expires <= now:
                raise ApiError(
                    400, "invalid_expiry", "expires_at must be in the future."
                )

        created_text = format_timestamp(now)
        expires_text = format_timestamp(expires)

        code: Optional[str] = None
        with get_connection(current.db_path) as conn:
            for _ in range(current.code_max_attempts):
                candidate = generate_code(current.code_length)
                try:
                    conn.execute(
                        "INSERT INTO links (code, url, created_at, expires_at) "
                        "VALUES (?, ?, ?, ?)",
                        (candidate, url, created_text, expires_text),
                    )
                    conn.commit()
                except sqlite3.IntegrityError:
                    conn.rollback()
                    continue
                code = candidate
                break

        if code is None:
            raise ApiError(
                503,
                "code_generation_failed",
                "Could not allocate a unique short code. Please retry.",
            )

        return {
            "code": code,
            "short_url": f"{current.base_url}/{code}",
            "url": url,
            "created_at": created_text,
            "expires_at": expires_text,
        }

    @app.get(
        "/api/links/{code}/stats",
        response_model=StatsResponse,
        tags=["links"],
    )
    def link_stats(
        code: str,
        request: Request,
        limit: Optional[int] = Query(default=None, description="Clicks per page."),
        offset: int = Query(default=0, description="Clicks to skip."),
    ) -> Dict[str, Any]:
        """Return per-link analytics, including for expired links.

        ``code`` identifies the link; ``limit`` and ``offset`` page the newest
        first clicks array. Returns the link metadata, the total click count and
        the requested page of clicks. Raises :class:`ApiError` 404 ``not_found``
        for an unknown code and 422 ``validation_error`` for out of range
        pagination parameters.
        """
        current: Settings = request.app.state.settings
        pattern: re.Pattern[str] = request.app.state.code_pattern
        if not pattern.match(code):
            raise ApiError(404, "not_found", "No link exists for that code.")

        effective_limit = current.stats_default_limit if limit is None else limit
        if effective_limit < 1 or effective_limit > current.stats_max_limit:
            raise ApiError(
                422,
                "validation_error",
                f"limit must be between 1 and {current.stats_max_limit}.",
            )
        if offset < 0:
            raise ApiError(422, "validation_error", "offset must be zero or greater.")

        with get_connection(current.db_path) as conn:
            link_row = conn.execute(
                "SELECT id, code, url, created_at, expires_at FROM links "
                "WHERE code = ?",
                (code,),
            ).fetchone()
            if link_row is None:
                raise ApiError(404, "not_found", "No link exists for that code.")
            total_row = conn.execute(
                "SELECT COUNT(*) AS total FROM clicks WHERE link_id = ?",
                (link_row["id"],),
            ).fetchone()
            click_rows = conn.execute(
                "SELECT clicked_at, referrer, user_agent FROM clicks "
                "WHERE link_id = ? ORDER BY clicked_at DESC, id DESC "
                "LIMIT ? OFFSET ?",
                (link_row["id"], effective_limit, offset),
            ).fetchall()

        clicks = [
            {
                "timestamp": row["clicked_at"],
                "referrer": row["referrer"],
                "user_agent": row["user_agent"],
            }
            for row in click_rows
        ]
        return {
            "code": link_row["code"],
            "url": link_row["url"],
            "created_at": link_row["created_at"],
            "expires_at": link_row["expires_at"],
            "total_clicks": int(total_row["total"]) if total_row else 0,
            "clicks": clicks,
        }

    @app.get("/{code}", include_in_schema=False)
    def redirect_to_target(code: str, request: Request) -> Response:
        """Redirect a visitor to the stored destination and record the click.

        ``code`` is the short code from the path; the destination is read from
        the stored, previously validated row and never from request input.
        Returns a 307 response whose Location is the stored URL, with
        ``Cache-Control: no-store``. Raises :class:`ApiError` 404 ``not_found``
        for unknown or malformed codes and 410 ``link_expired`` for lapsed
        links; neither writes a click row.
        """
        current: Settings = request.app.state.settings
        pattern: re.Pattern[str] = request.app.state.code_pattern
        if not pattern.match(code):
            raise ApiError(404, "not_found", "No link exists for that code.")

        now = datetime.now(timezone.utc)
        with get_connection(current.db_path) as conn:
            row = conn.execute(
                "SELECT id, url, expires_at FROM links WHERE code = ?",
                (code,),
            ).fetchone()
            if row is None:
                raise ApiError(404, "not_found", "No link exists for that code.")

            try:
                expires = parse_timestamp(row["expires_at"])
            except ValueError:
                # Fail closed: an unreadable expiry is treated as expired.
                logger.error("Unparsable expires_at for a stored link")
                raise ApiError(410, "link_expired", "This link has expired.")

            if now >= expires:
                raise ApiError(410, "link_expired", "This link has expired.")

            conn.execute(
                "INSERT INTO clicks (link_id, clicked_at, referrer, user_agent) "
                "VALUES (?, ?, ?, ?)",
                (
                    row["id"],
                    format_timestamp(now),
                    clip_header(request.headers.get("referer")),
                    clip_header(request.headers.get("user-agent")),
                ),
            )
            conn.commit()
            destination = row["url"]

        return Response(
            status_code=307,
            headers={
                "Location": location_header_value(destination),
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover - manual entry point
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
