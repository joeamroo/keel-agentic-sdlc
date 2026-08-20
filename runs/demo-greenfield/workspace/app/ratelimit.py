"""In-process sliding window rate limiting, applied as ASGI middleware.

The limiter runs before routing and body parsing, so a flood is rejected
without touching the database. Keys are a per-boot salted SHA-256 of the client
address: the address itself is never stored, persisted or logged, and all state
vanishes on restart.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from typing import Deque, Optional, Tuple

from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings
from .errors import error_response

#: Redirects are far cheaper and far more frequent than creations, so the
#: redirect bucket uses a multiple of the configured creation budget. The
#: design pins only the creation limit; this keeps redirects protected without
#: introducing a new environment variable.
REDIRECT_LIMIT_MULTIPLIER = 100

#: Upper bound on tracked keys so the limiter cannot grow without limit.
MAX_TRACKED_KEYS = 20000

CREATE_PATH = "/api/links"


class RateLimitMiddleware:
    """Rate limit link creation and redirects in a single place."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        """Wrap an ASGI application with the limiter.

        ``app`` is the downstream ASGI application and ``settings`` the service
        configuration. Returns nothing. Raises nothing.
        """
        self.app = app
        self.settings = settings
        self._salt = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._hits: "OrderedDict[str, Deque[float]]" = OrderedDict()
        self._code_pattern = re.compile(
            r"^/[A-Za-z0-9]{%d}$" % settings.code_length
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entry point.

        ``scope``, ``receive`` and ``send`` are the standard ASGI arguments.
        Returns nothing; either forwards the call downstream or emits a 429 with
        a ``Retry-After`` header. Raises nothing of its own.
        """
        if scope.get("type") != "http" or not self.settings.rate_limit_enabled:
            await self.app(scope, receive, send)
            return

        rule = self._rule_for(str(scope.get("method", "")), str(scope.get("path", "")))
        if rule is None:
            await self.app(scope, receive, send)
            return

        bucket, limit = rule
        allowed, retry_after = self._consume(bucket, self._client_key(scope, bucket), limit)
        if allowed:
            await self.app(scope, receive, send)
            return

        response = error_response(
            429,
            "rate_limited",
            "Too many requests. Please retry later.",
            headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
        )
        await response(scope, receive, send)

    def _rule_for(self, method: str, path: str) -> Optional[Tuple[str, int]]:
        """Decide which limiter bucket applies to a request.

        ``method`` is the HTTP method and ``path`` the request path. Returns a
        ``(bucket, limit)`` pair for rate limited routes, or ``None`` when the
        route is not limited. Raises nothing.
        """
        normalized = path.rstrip("/") or "/"
        if method.upper() == "POST" and normalized == CREATE_PATH:
            return "create", self.settings.rate_limit_max
        if method.upper() in {"GET", "HEAD"} and self._code_pattern.match(path):
            return "redirect", self.settings.rate_limit_max * REDIRECT_LIMIT_MULTIPLIER
        return None

    def _client_address(self, scope: Scope) -> str:
        """Determine the client address used for keying.

        ``scope`` is the ASGI scope. Returns the first ``X-Forwarded-For`` entry
        when the service is configured to trust it, otherwise the peer address,
        or ``"unknown"``. The value is only hashed, never stored or logged.
        Raises nothing.
        """
        if self.settings.trust_forwarded_for:
            for name, value in scope.get("headers") or []:
                if name == b"x-forwarded-for":
                    first = value.decode("latin-1", "ignore").split(",")[0].strip()
                    if first:
                        return first[:128]
                    break
        client = scope.get("client")
        if client:
            try:
                return str(client[0])[:128]
            except (IndexError, TypeError):  # pragma: no cover - defensive
                return "unknown"
        return "unknown"

    def _client_key(self, scope: Scope, bucket: str) -> str:
        """Build the salted hash key for a client and bucket.

        ``scope`` is the ASGI scope and ``bucket`` the limiter bucket name.
        Returns a hex SHA-256 digest that cannot be reversed to an IP address.
        Raises nothing.
        """
        digest = hashlib.sha256()
        digest.update(self._salt)
        digest.update(bucket.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self._client_address(scope).encode("utf-8", "ignore"))
        return digest.hexdigest()

    def _consume(self, bucket: str, key: str, limit: int) -> Tuple[bool, int]:
        """Record a request against the rolling window.

        ``bucket`` is the limiter bucket (used only for readability), ``key``
        the hashed client key and ``limit`` the allowed number of requests per
        window. Returns ``(allowed, retry_after_seconds)`` where
        ``retry_after_seconds`` is at least 1 when the request is denied.
        Raises nothing.
        """
        now = time.monotonic()
        window = float(self.settings.rate_limit_window_seconds)
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = deque()
                self._hits[key] = hits
            self._hits.move_to_end(key)
            while hits and (now - hits[0]) >= window:
                hits.popleft()
            if len(hits) >= limit:
                retry_after = max(1, math.ceil(window - (now - hits[0])))
                return False, retry_after
            hits.append(now)
            self._prune_locked()
            return True, 0

    def _prune_locked(self) -> None:
        """Drop stale limiter state while the lock is held.

        Returns nothing. Raises nothing.
        """
        if len(self._hits) <= MAX_TRACKED_KEYS:
            return
        for stale_key in [k for k, v in self._hits.items() if not v]:
            self._hits.pop(stale_key, None)
        while len(self._hits) > MAX_TRACKED_KEYS:
            self._hits.popitem(last=False)
