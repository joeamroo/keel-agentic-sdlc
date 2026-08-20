"""FastAPI application for the URL shortener.

Routes:

* ``POST /api/links``               create a short link
* ``GET  /{code}``                  redirect to the stored, validated target
* ``GET  /api/links/{code}/stats``  click statistics for a code
* ``GET  /health``                  liveness, touches no user data

Redirect destinations always come from the validated value stored at creation
time, never from a query parameter, header or path segment of the incoming
request.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, cast

from fastapi import FastAPI, Request, Response

from .config import Config, load_config
from .db import get_connection, init_db
from .errors import ApiError, register_exception_handlers
from .models import CreateLinkRequest, CreateLinkResponse, HealthResponse, StatsResponse
from .ratelimit import RateLimitMiddleware
from .urls import UrlValidationError, validate_target_url

CODE_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
CODE_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
NOT_FOUND_MESSAGE = "Not found."


def utc_now() -> datetime:
    """Return the current time as a timezone aware UTC datetime.

    Returns an aware :class:`datetime`.  Raises nothing.
    """
    return datetime.now(timezone.utc)


def to_rfc3339(value: datetime) -> str:
    """Render an aware datetime as an RFC3339 UTC string.

    Returns the ISO 8601 string in UTC.  Raises :class:`ValueError` when the
    datetime is naive.
    """
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone aware")
    return value.astimezone(timezone.utc).isoformat()


def parse_rfc3339(value: str) -> datetime:
    """Parse a stored RFC3339 timestamp into an aware UTC datetime.

    Accepts both ``+00:00`` and ``Z`` suffixes and treats a naive value as UTC.
    Returns an aware :class:`datetime`.  Raises :class:`ValueError` when the
    text cannot be parsed.
    """
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def generate_code(length: int) -> str:
    """Generate a random base62 short code.

    Returns a code of ``length`` characters drawn from a CSPRNG.  Raises
    :class:`ValueError` when ``length`` is not positive.
    """
    if length < 1:
        raise ValueError("code length must be positive")
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def is_expired(expires_at_text: Optional[str], now: datetime) -> bool:
    """Decide whether a stored expiry timestamp has passed.

    Returns ``True`` when the link is expired; a missing or unparseable expiry
    is treated as expired only when unparseable, never as "never expires" by
    accident.  Raises nothing.
    """
    if expires_at_text is None or not str(expires_at_text).strip():
        return False
    try:
        expires_at = parse_rfc3339(str(expires_at_text))
    except ValueError:
        # A corrupt timestamp fails closed: the link is not served.
        return True
    return now >= expires_at


def get_config(request: Request) -> Config:
    """Fetch the Config bound to the running application.

    Returns the application's :class:`Config`.  Raises
    :class:`AttributeError` when the application was not built by
    :func:`create_app`.
    """
    return cast(Config, request.app.state.config)


def create_app(config: Optional[Config] = None) -> FastAPI:
    """Build the FastAPI application.

    Loads configuration from the environment when none is supplied, initialises
    the SQLite schema, installs the rate limiting middleware and registers the
    routes.  Returns the application.  Raises :class:`sqlite3.Error` or
    :class:`OSError` when the database cannot be initialised.
    """
    settings = config if config is not None else load_config()
    application = FastAPI(title="URL Shortener", version="1.1.0")
    application.state.config = settings

    init_db(settings.db_path)
    register_exception_handlers(application)
    application.add_middleware(RateLimitMiddleware, config=settings)

    @application.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """Report process liveness.

        Returns ``{"status": "ok"}`` without touching any user data or the
        database.  Raises nothing.
        """
        return HealthResponse(status="ok")

    @application.post("/api/links", response_model=CreateLinkResponse, status_code=201)
    def create_link(payload: CreateLinkRequest, request: Request) -> CreateLinkResponse:
        """Create a short link for a validated target URL.

        Returns the created link with its short URL and expiry.  Raises
        :class:`ApiError` with status 400 when the URL is invalid or unsafe and
        503 when no unique code could be allocated within the configured number
        of attempts.
        """
        settings_local = get_config(request)
        try:
            target_url = validate_target_url(payload.target_url, settings_local.max_url_length)
        except UrlValidationError as exc:
            raise ApiError(400, "invalid_url", str(exc)) from exc

        ttl = (
            payload.expires_in_seconds
            if payload.expires_in_seconds is not None
            else settings_local.default_ttl_seconds
        )
        created_at = utc_now()
        expires_at: Optional[datetime] = (
            created_at + timedelta(seconds=ttl) if ttl and ttl > 0 else None
        )
        created_text = to_rfc3339(created_at)
        expires_text = to_rfc3339(expires_at) if expires_at is not None else None

        with get_connection(settings_local.db_path) as connection:
            for _attempt in range(settings_local.code_max_attempts):
                code = generate_code(settings_local.code_length)
                try:
                    connection.execute(
                        "INSERT INTO links (code, target_url, created_at, expires_at)"
                        " VALUES (?, ?, ?, ?)",
                        (code, target_url, created_text, expires_text),
                    )
                except sqlite3.IntegrityError:
                    continue
                return CreateLinkResponse(
                    code=code,
                    short_url=f"{settings_local.base_url}/{code}",
                    target_url=target_url,
                    created_at=created_at,
                    expires_at=expires_at,
                )

        raise ApiError(503, "unavailable", "Could not allocate a short code. Please retry.")

    @application.get("/api/links/{code}/stats", response_model=StatsResponse)
    def link_stats(code: str, request: Request) -> StatsResponse:
        """Return click statistics for a short code.

        Returns the stored link metadata plus click count, last click time and
        an ``expired`` flag.  Raises :class:`ApiError` with status 404 when the
        code does not exist.
        """
        settings_local = get_config(request)
        if not CODE_RE.match(code):
            raise ApiError(404, "not_found", NOT_FOUND_MESSAGE)

        now = utc_now()
        with get_connection(settings_local.db_path) as connection:
            row = connection.execute(
                "SELECT id, code, target_url, created_at, expires_at FROM links WHERE code = ?",
                (code,),
            ).fetchone()
            if row is None:
                raise ApiError(404, "not_found", NOT_FOUND_MESSAGE)
            stats_row = connection.execute(
                "SELECT COUNT(*) AS click_count, MAX(clicked_at) AS last_clicked_at"
                " FROM clicks WHERE link_id = ?",
                (int(row["id"]),),
            ).fetchone()

        expires_text = row["expires_at"]
        last_clicked_text = stats_row["last_clicked_at"] if stats_row is not None else None
        return StatsResponse(
            code=str(row["code"]),
            short_url=f"{settings_local.base_url}/{row['code']}",
            target_url=str(row["target_url"]),
            created_at=parse_rfc3339(str(row["created_at"])),
            expires_at=parse_rfc3339(str(expires_text)) if expires_text else None,
            expired=is_expired(expires_text, now),
            clicks=int(stats_row["click_count"]) if stats_row is not None else 0,
            last_clicked_at=parse_rfc3339(str(last_clicked_text)) if last_clicked_text else None,
        )

    @application.get("/{code}")
    def redirect(code: str, request: Request) -> Response:
        """Redirect to the stored target for a short code.

        Returns a 307 response whose Location header is the validated URL that
        was stored at creation time.  Raises :class:`ApiError` with status 404
        when the code is unknown or expired; the two cases are byte-identical so
        the service cannot be used as an enumeration oracle.
        """
        settings_local = get_config(request)
        if not CODE_RE.match(code):
            raise ApiError(404, "not_found", NOT_FOUND_MESSAGE)

        now = utc_now()
        with get_connection(settings_local.db_path) as connection:
            row = connection.execute(
                "SELECT id, target_url, expires_at FROM links WHERE code = ?",
                (code,),
            ).fetchone()
            if row is None or is_expired(row["expires_at"], now):
                raise ApiError(404, "not_found", NOT_FOUND_MESSAGE)
            connection.execute(
                "INSERT INTO clicks (link_id, clicked_at) VALUES (?, ?)",
                (int(row["id"]), to_rfc3339(now)),
            )
            target_url = str(row["target_url"])

        return Response(
            status_code=307,
            headers={"Location": target_url, "Cache-Control": "no-store"},
        )

    return application


app = create_app()


if __name__ == "__main__":  # pragma: no cover - manual entry point
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
