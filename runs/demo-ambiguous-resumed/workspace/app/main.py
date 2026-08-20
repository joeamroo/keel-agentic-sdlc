"""FastAPI application for the public URL shortening service."""
from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import AsyncIterator, Awaitable, Callable, FrozenSet, Optional, Tuple

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .codes import generate_code, is_plausible_code
from .config import Settings, configure_logging, load_settings
from .db import Database, ExpiryPurger
from .errors import ApiError, error_response, register_exception_handlers
from .models import CreateLinkResponse, CreateLinkRequest, HealthResponse
from .ratelimit import FixedWindowRateLimiter
from .timeutil import to_iso_z, utc_now
from .validation import DestinationError, DestinationValidator, host_of, normalize_destination

LOGGER = logging.getLogger("links")

# Single segment paths that are owned by the service and therefore never treated
# as a short code for rate limiting purposes.
RESERVED_PATHS: FrozenSet[str] = frozenset(
    {"links", "health", "healthz", "livez", "readyz", "docs", "redoc", "openapi.json", "favicon.ico"}
)


def client_ip_for(request: Request, settings: Settings) -> str:
    """Determine the rate limiting identity of the caller.

    Args:
        request: The incoming request.
        settings: Active configuration; ``trust_proxy_header`` decides whether the
            last ``X-Forwarded-For`` entry may be used.

    Returns:
        A short string identifying the client; the socket peer address unless the
        deployment explicitly trusts a fronting proxy.

    Raises:
        Nothing.
    """
    peer = request.client.host if request.client is not None else "unknown"
    if settings.trust_proxy_header:
        header = request.headers.get("x-forwarded-for", "")
        entries = [entry.strip() for entry in header.split(",") if entry.strip()]
        if entries:
            return entries[-1][:64]
    return (peer or "unknown")[:64]


def _rate_limit_scope(request: Request, settings: Settings) -> Optional[Tuple[str, int]]:
    """Decide which rate limit bucket, if any, applies to a request.

    Args:
        request: The incoming request.
        settings: Active configuration supplying the allowances.

    Returns:
        A ``(scope_name, limit)`` tuple, or ``None`` when the path is not rate
        limited (health checks and documentation).

    Raises:
        Nothing.
    """
    segments = [segment for segment in request.url.path.split("/") if segment]
    if request.method == "POST" and len(segments) == 1 and segments[0] == "links":
        return ("create", settings.rate_limit_max)
    if (
        request.method in {"GET", "HEAD"}
        and len(segments) == 1
        and segments[0].lower() not in RESERVED_PATHS
    ):
        return ("redirect", settings.redirect_rate_limit_max)
    return None


def _record_click(database: Database, code: str) -> None:
    """Update best effort click analytics after a redirect has been written.

    Args:
        database: The datastore.
        code: The resolved short code.

    Returns:
        None.

    Raises:
        Nothing; datastore failures are logged and never break a working link.
    """
    try:
        database.record_click(code, to_iso_z(utc_now()))
    except sqlite3.Error:
        LOGGER.warning("click analytics update failed")


def _run_purge(purger: ExpiryPurger) -> None:
    """Run the rate limited expired-link purge in the background.

    Args:
        purger: The housekeeping helper.

    Returns:
        None.

    Raises:
        Nothing.
    """
    purger.maybe_purge()


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Build and wire the FastAPI application.

    Args:
        settings: Optional pre-built configuration; loaded from the environment when omitted.

    Returns:
        A configured :class:`FastAPI` instance with routes, middleware and error handlers.

    Raises:
        sqlite3.Error: If the database schema cannot be created at startup.
        OSError: If the database directory cannot be created.
    """
    active = settings if settings is not None else load_settings()
    configure_logging(active)

    database = Database(active.db_path)
    database.initialize()
    limiter = FixedWindowRateLimiter(active.rate_limit_window_seconds)
    validator = DestinationValidator(active)
    purger = ExpiryPurger(database)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Manage process scoped resources.

        Returns:
            An async context manager that releases the resolver pool on shutdown.

        Raises:
            Nothing.
        """
        LOGGER.info("url shortener starting")
        try:
            yield
        finally:
            validator.close()
            LOGGER.info("url shortener stopped")

    app = FastAPI(
        title="URL Shortener",
        version="1.0.0",
        description="Issues short redirect links to validated public http(s) destinations.",
        lifespan=lifespan,
    )
    # Never emit a redirect whose Location is derived from the incoming path.
    app.router.redirect_slashes = False

    app.state.settings = active
    app.state.db = database
    app.state.rate_limiter = limiter
    app.state.validator = validator
    app.state.purger = purger

    register_exception_handlers(app)

    @app.middleware("http")
    async def rate_limit_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Enforce per-IP rate limits for creation and redirects in one place.

        Args:
            request: The incoming request.
            call_next: The downstream ASGI handler.

        Returns:
            The downstream response, or a 429 JSON error carrying ``Retry-After``
            when the caller exceeded the allowance for the scope. The limiter runs
            before body parsing, validation, DNS and any insert.

        Raises:
            Nothing directly; downstream exceptions reach the registered handlers.
        """
        scope = _rate_limit_scope(request, active)
        if scope is not None:
            name, limit = scope
            key = "{0}:{1}".format(name, client_ip_for(request, active))
            decision = limiter.check(key, limit)
            if not decision.allowed:
                return error_response(
                    429,
                    "rate_limited",
                    "Too many requests; please retry later.",
                    {"Retry-After": str(decision.retry_after)},
                )
        return await call_next(request)

    def _health_body() -> HealthResponse:
        """Build the liveness payload without touching user data.

        Returns:
            A :class:`HealthResponse` describing process liveness.

        Raises:
            Nothing.
        """
        return HealthResponse(status="ok", service="links", time=to_iso_z(utc_now()))

    @app.get("/healthz", response_model=HealthResponse, tags=["ops"])
    def healthz(response: Response) -> HealthResponse:
        """Report process liveness.

        Args:
            response: Injected response used to set cache headers.

        Returns:
            A payload with ``status``, ``service`` and the current UTC time. No user
            data or datastore is touched.

        Raises:
            Nothing.
        """
        response.headers["Cache-Control"] = "no-store"
        return _health_body()

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health(response: Response) -> HealthResponse:
        """Alias of :func:`healthz` for deployments that probe ``/health``.

        Args:
            response: Injected response used to set cache headers.

        Returns:
            The same liveness payload as ``/healthz``.

        Raises:
            Nothing.
        """
        response.headers["Cache-Control"] = "no-store"
        return _health_body()

    @app.post("/links", response_model=CreateLinkResponse, status_code=201, tags=["links"])
    def create_link(
        payload: CreateLinkRequest,
        request: Request,
        response: Response,
        background: BackgroundTasks,
    ) -> CreateLinkResponse:
        """Create a short link for a validated public http(s) destination.

        Args:
            payload: Validated request body.
            request: The incoming request (used for the creator IP only).
            response: Injected response used to set cache headers.
            background: Background task queue used for housekeeping.

        Returns:
            A 201 payload with the issued code, the full short URL, the stored
            (normalised) destination and ISO-8601 UTC timestamps.

        Raises:
            ApiError: 400 for an invalid expiry or an invalid/unsafe destination,
                503 when no unique code could be allocated, 500 on datastore failure.
        """
        days = active.default_expiry_days if payload.expires_in_days is None else payload.expires_in_days
        if days < 1 or days > active.max_expiry_days:
            raise ApiError(
                400,
                "expiry_out_of_range",
                "expires_in_days must be a whole number between 1 and {0}.".format(active.max_expiry_days),
            )

        try:
            destination, host = normalize_destination(payload.url, active)
            validator.assert_routable(host)
        except DestinationError as exc:
            raise ApiError(400, exc.code, exc.message)

        created = utc_now()
        expires = created + timedelta(days=days)
        created_at = to_iso_z(created)
        expires_at = to_iso_z(expires)
        creator_ip = client_ip_for(request, active)

        issued_code: Optional[str] = None
        for _ in range(max(1, active.code_max_attempts)):
            candidate = generate_code(active.code_length)
            try:
                database.insert_link(
                    code=candidate,
                    destination=destination,
                    created_at=created_at,
                    expires_at=expires_at,
                    creator_ip=creator_ip,
                )
            except sqlite3.IntegrityError:
                LOGGER.info("short code collision; retrying")
                continue
            except sqlite3.Error:
                LOGGER.error("link insert failed")
                raise ApiError(500, "internal_error", "An internal error occurred.")
            issued_code = candidate
            break

        if issued_code is None:
            raise ApiError(503, "code_generation_failed", "Could not allocate a unique short code; please retry.")

        background.add_task(_run_purge, purger)
        response.headers["Cache-Control"] = "no-store"
        return CreateLinkResponse(
            code=issued_code,
            short_url="{0}/{1}".format(active.public_base_url, issued_code),
            destination=destination,
            created_at=created_at,
            expires_at=expires_at,
        )

    @app.get("/{code}", tags=["links"])
    def resolve_link(code: str, background: BackgroundTasks) -> Response:
        """Resolve a short code to its stored destination.

        The Location header is always the destination this service validated and
        stored itself; nothing from the incoming query string, headers or path is
        ever used to build it.

        Args:
            code: The short code from the request path.
            background: Background task queue used for click analytics.

        Returns:
            A 302 response whose ``Location`` is the exact stored destination, with
            ``Cache-Control: no-store``.

        Raises:
            ApiError: 404 when the code was never issued, 410 when the link has
                expired, 502 when the destination no longer resolves to routable
                space, 500 on datastore failure.
        """
        if not is_plausible_code(code):
            raise ApiError(404, "not_found", "No link exists for that code.")

        try:
            row = database.get_link(code)
        except sqlite3.Error:
            LOGGER.error("link lookup failed")
            raise ApiError(500, "internal_error", "An internal error occurred.")

        if row is None:
            raise ApiError(404, "not_found", "No link exists for that code.")

        now_iso = to_iso_z(utc_now())
        if str(row["expires_at"]) <= now_iso:
            raise ApiError(410, "link_expired", "This link has expired.")

        destination = str(row["destination"])
        host = host_of(destination)
        try:
            validator.assert_routable(host or "")
        except DestinationError as exc:
            LOGGER.warning("stored destination failed re-validation: %s", exc.code)
            raise ApiError(502, exc.code, "The destination could not be served.")

        background.add_task(_record_click, database, code)
        return Response(
            status_code=302,
            headers={
                "Location": destination,
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return app


app = create_app()


def run() -> None:
    """Start the ASGI server using the configured bind host and port.

    Returns:
        None; blocks until the server exits.

    Raises:
        ImportError: If uvicorn is not installed.
    """
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        app,
        host=settings.bind_host,
        port=settings.bind_port,
        log_level=settings.uvicorn_log_level,
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    run()
