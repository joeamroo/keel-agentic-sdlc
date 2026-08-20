"""Application factory and ASGI entry point for the URL shortener."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .apikeys import parse_api_key_quotas
from .config import Settings, load_settings
from .db import init_db
from .errors import api_error_handler, unhandled_error_handler, validation_error_handler
from .ratelimit import RateLimiter, RateLimitMiddleware
from .routes import router

LOGGER = logging.getLogger("shortener")

API_KEYS_ENV_VAR = "SHORTENER_API_KEYS"


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Build a configured FastAPI application.

    Configuration is read from the environment (or from the supplied settings), the
    API key quota table is parsed once here, and the rate limiter is installed as
    ASGI middleware in front of routing.

    Args:
        settings: Optional pre-built settings; defaults to reading the environment.

    Returns:
        The configured :class:`FastAPI` application.

    Raises:
        sqlite3.Error: If the database schema cannot be created.
        OSError: If the database directory cannot be created.
    """
    resolved = settings if settings is not None else load_settings()
    quotas = parse_api_key_quotas(os.environ.get(API_KEYS_ENV_VAR, ""))
    limiter = RateLimiter(
        enabled=resolved.rate_limit_enabled,
        max_requests=resolved.rate_limit_max,
        window_seconds=resolved.rate_limit_window_seconds,
        trust_forwarded_for=resolved.trust_forwarded_for,
        api_key_quotas=quotas,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """Prepare the database before serving and release nothing on shutdown.

        Args:
            application: The application being started.

        Returns:
            An async context manager yielding once the service is ready.

        Raises:
            sqlite3.Error: If the schema cannot be created.
        """
        init_db(application.state.settings.db_path)
        yield

    application = FastAPI(
        title="URL Shortener",
        version="1.1.0",
        summary="Create short links, follow them, and read their click statistics.",
        lifespan=lifespan,
    )
    application.state.settings = resolved
    application.state.rate_limiter = limiter
    application.state.api_key_quotas = quotas

    application.add_exception_handler(StarletteHTTPException, api_error_handler)
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    application.add_exception_handler(Exception, unhandled_error_handler)

    application.include_router(router)
    application.add_middleware(RateLimitMiddleware, limiter=limiter)

    init_db(resolved.db_path)
    return application


app = create_app()


if __name__ == "__main__":  # pragma: no cover - manual execution helper
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
