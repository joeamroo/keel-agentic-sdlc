"""In-process sliding window rate limiting ASGI middleware.

The middleware runs before routing and body parsing, so a refused request never
opens a database connection.  Three bucket namespaces share one bounded map:

``ip:``   per client address budget for ``POST /api/links``
``rip:``  per client address budget for redirects
``key:``  per recognised API key budget for ``POST /api/links``

Bucket identities are per-boot salted SHA-256 digests; neither a raw client
address nor a raw API key is ever stored, logged or returned.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, MutableMapping, Optional, Tuple

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Config
from .errors import error_response

#: Upper bound on the number of tracked buckets across every namespace.
MAX_TRACKED_KEYS: int = 10_000

#: Redirects are allowed this multiple of the per-IP creation allowance.
REDIRECT_LIMIT_MULTIPLIER: int = 10

#: API key quotas are declared per minute; the key window is fixed.
KEY_WINDOW_SECONDS: float = 60.0

API_KEY_HEADER = b"x-api-key"
CREATE_PATH = "/api/links"
REDIRECT_METHODS = frozenset({"GET", "HEAD"})
_CODE_PATH_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RESERVED_SEGMENTS = frozenset({"health", "docs", "redoc", "openapi", "favicon"})

RATE_LIMITED_MESSAGE = "Rate limit exceeded. Please retry later."


@dataclass
class RateLimitBucket:
    """Sliding window state for one bucket identity."""

    limit: int
    window_seconds: float
    last_seen: float
    timestamps: Deque[float] = field(default_factory=deque)


@dataclass(frozen=True)
class _BucketSpec:
    """Which bucket a request should be charged against."""

    bucket_id: str
    limit: int
    window_seconds: float


def _normalise_path(raw_path: str) -> str:
    """Normalise a request path for matching.

    Returns the path without a trailing slash (the root path stays ``"/"``).
    Raises nothing.
    """
    if not raw_path:
        return "/"
    if len(raw_path) > 1 and raw_path.endswith("/"):
        return raw_path.rstrip("/") or "/"
    return raw_path


class RateLimitMiddleware:
    """ASGI middleware enforcing per-IP and per-API-key request budgets."""

    def __init__(self, app: ASGIApp, config: Config) -> None:
        """Create the middleware with a per-boot salt and empty bucket map.

        Returns ``None``.  Raises nothing.
        """
        self.app = app
        self.config = config
        self._buckets: MutableMapping[str, RateLimitBucket] = {}
        self._lock = threading.Lock()
        self._salt = secrets.token_hex(16)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Charge the request against its bucket, or refuse it with 429.

        Returns ``None``; either forwards the request downstream or sends a 429
        response carrying ``Retry-After`` and ``Cache-Control: no-store``.
        Raises whatever the downstream application raises.
        """
        if scope.get("type") != "http" or not self.config.rate_limit_enabled:
            await self.app(scope, receive, send)
            return

        spec = self._classify(scope)
        if spec is None:
            await self.app(scope, receive, send)
            return

        retry_after = self._consume(spec)
        if retry_after is None:
            await self.app(scope, receive, send)
            return

        response = error_response(
            429,
            "rate_limited",
            RATE_LIMITED_MESSAGE,
            headers={"Retry-After": str(retry_after)},
        )
        await response(scope, receive, self._wrap_send(send))

    @staticmethod
    def _wrap_send(send: Send) -> Send:
        """Return the send callable unchanged.

        Exists as a single seam for response instrumentation.  Returns the send
        callable.  Raises nothing.
        """

        async def _send(message: Message) -> None:
            """Forward an ASGI message downstream.

            Returns ``None``.  Raises whatever the wrapped send raises.
            """
            await send(message)

        return _send

    def _hash(self, namespace: str, value: str) -> str:
        """Build a namespaced, salted bucket identity.

        Returns ``"<namespace>:<sha256 hex digest>"``; the raw value never
        appears in the result.  Raises nothing.
        """
        digest = hashlib.sha256(f"{self._salt}{value}".encode("utf-8")).hexdigest()
        return f"{namespace}:{digest}"

    @staticmethod
    def _client_host(scope: Scope) -> str:
        """Extract the peer address from the ASGI scope.

        Returns the client host, or ``"unknown"`` when the server did not supply
        one.  The value comes from the transport, not from a forgeable header.
        Raises nothing.
        """
        client: Any = scope.get("client")
        if isinstance(client, (tuple, list)) and client:
            host = client[0]
            if isinstance(host, str) and host:
                return host
        return "unknown"

    @staticmethod
    def _extract_api_key(scope: Scope) -> Optional[str]:
        """Read the X-API-Key header value from the scope.

        The header name is matched case-insensitively (ASGI lowercases it) and
        the value is stripped; a blank value is treated as absent.  Returns the
        stripped key or ``None``.  Raises nothing.
        """
        headers = scope.get("headers") or []
        for raw_name, raw_value in headers:
            if raw_name.lower() == API_KEY_HEADER:
                try:
                    value = raw_value.decode("latin-1").strip()
                except (UnicodeDecodeError, AttributeError):
                    return None
                return value or None
        return None

    def _classify(self, scope: Scope) -> Optional[_BucketSpec]:
        """Decide which bucket, if any, a request must be charged against.

        Returns a :class:`_BucketSpec` for rate limited routes and ``None`` for
        everything else (health, stats, docs, unknown routes).  Raises nothing.
        """
        method = str(scope.get("method", "")).upper()
        path = _normalise_path(str(scope.get("path", "/")))

        if method == "POST" and path == CREATE_PATH:
            api_key = self._extract_api_key(scope)
            if api_key is not None:
                quota = self.config.api_key_quotas.get(api_key)
                if quota is not None and quota >= 1:
                    return _BucketSpec(
                        bucket_id=self._hash("key", api_key),
                        limit=int(quota),
                        window_seconds=KEY_WINDOW_SECONDS,
                    )
            return _BucketSpec(
                bucket_id=self._hash("ip", self._client_host(scope)),
                limit=self.config.rate_limit_max,
                window_seconds=self.config.rate_limit_window_seconds,
            )

        if method in REDIRECT_METHODS:
            segments = [segment for segment in path.split("/") if segment]
            if len(segments) == 1:
                segment = segments[0]
                if segment.lower() not in _RESERVED_SEGMENTS and _CODE_PATH_RE.match(segment):
                    return _BucketSpec(
                        bucket_id=self._hash("rip", self._client_host(scope)),
                        limit=max(1, self.config.rate_limit_max * REDIRECT_LIMIT_MULTIPLIER),
                        window_seconds=self.config.rate_limit_window_seconds,
                    )
        return None

    def _consume(self, spec: _BucketSpec) -> Optional[int]:
        """Record one request against a bucket.

        Returns ``None`` when the request is within budget, otherwise the whole
        number of seconds (>= 1) the caller must wait before capacity is
        released.  Raises nothing.
        """
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(spec.bucket_id)
            if bucket is None:
                bucket = RateLimitBucket(
                    limit=spec.limit,
                    window_seconds=spec.window_seconds,
                    last_seen=now,
                )
                self._buckets[spec.bucket_id] = bucket
            else:
                bucket.limit = spec.limit
                bucket.window_seconds = spec.window_seconds

            cutoff = now - bucket.window_seconds
            while bucket.timestamps and bucket.timestamps[0] <= cutoff:
                bucket.timestamps.popleft()

            bucket.last_seen = now
            if len(bucket.timestamps) >= bucket.limit:
                oldest = bucket.timestamps[0]
                remaining = (oldest + bucket.window_seconds) - now
                retry_after = max(1, int(math.ceil(remaining)))
                retry_after = min(retry_after, max(1, int(math.ceil(bucket.window_seconds))))
                self._prune_locked()
                return retry_after

            bucket.timestamps.append(now)
            self._prune_locked()
            return None

    def _prune_locked(self) -> None:
        """Evict least-recently-seen buckets when the map exceeds its bound.

        Must be called while holding the lock.  Returns ``None``.  Raises
        nothing.
        """
        overflow = len(self._buckets) - MAX_TRACKED_KEYS
        if overflow <= 0:
            return
        victims: list[Tuple[str, float]] = sorted(
            ((bucket_id, bucket.last_seen) for bucket_id, bucket in self._buckets.items()),
            key=lambda item: item[1],
        )
        for bucket_id, _ in victims[:overflow]:
            self._buckets.pop(bucket_id, None)

    def snapshot_size(self) -> int:
        """Report how many buckets are currently tracked.

        Returns the bucket count; exposed for diagnostics and tests only, never
        the bucket contents.  Raises nothing.
        """
        with self._lock:
            return len(self._buckets)


__all__ = [
    "MAX_TRACKED_KEYS",
    "REDIRECT_LIMIT_MULTIPLIER",
    "KEY_WINDOW_SECONDS",
    "RateLimitBucket",
    "RateLimitMiddleware",
]


def _unused(_message: Dict[str, Any]) -> None:
    """Placeholder-free no-op kept out of the hot path.

    Returns ``None``.  Raises nothing.
    """
    return None
