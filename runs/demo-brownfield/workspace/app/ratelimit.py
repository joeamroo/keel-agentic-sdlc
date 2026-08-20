"""Fixed-window rate limiting for POST /api/links.

Enforcement lives in one place: a pure ASGI middleware that runs before routing and
before body parsing, scoped to exactly ``POST /api/links``. A throttled request
therefore never opens a database connection and never writes a row, and no other
route (``/health``, ``/{code}``, the stats endpoint) can ever be refused with 429.
"""

from __future__ import annotations

import json
import math
import threading
import time
from typing import Callable, Dict, Mapping, NamedTuple, Optional, Tuple

from starlette.types import ASGIApp, Receive, Scope, Send

from .apikeys import ApiKeyEntry, lookup_api_key, salted_digest

#: Upper bound on entries tracked in each bucket map, applied independently.
MAX_TRACKED_KEYS: int = 10000

#: The only path the limiter guards.
CREATE_LINK_PATH: str = "/api/links"

RATE_LIMITED_BODY: bytes = json.dumps(
    {"error": {"code": "rate_limited", "message": "Rate limit exceeded. Please retry later."}},
    separators=(",", ":"),
).encode("utf-8")


class RateLimitDecision(NamedTuple):
    """Outcome of a limiter consultation.

    Attributes:
        allowed: Whether the request may proceed.
        retry_after: Whole seconds to wait; meaningful only when refused.
    """

    allowed: bool
    retry_after: int


class RateLimiter:
    """In-process fixed-window counters for unkeyed and keyed link creation.

    Two namespaces are kept: ``ip_buckets`` for callers without a recognised API key
    and ``key_buckets`` for callers presenting one. A recognised key substitutes for
    the per-IP bucket rather than adding to it, so a configured quota means exactly
    what the operator wrote. State is lost on restart.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        max_requests: int,
        window_seconds: int,
        trust_forwarded_for: bool,
        api_key_quotas: Mapping[bytes, ApiKeyEntry],
    ) -> None:
        """Create a limiter.

        Args:
            enabled: Master switch; when false nothing is counted or refused.
            max_requests: Per-window allowance of the per-IP bucket.
            window_seconds: Fixed-window length, shared by all buckets.
            trust_forwarded_for: Whether to use the leftmost X-Forwarded-For entry
                as the per-IP identity.
            api_key_quotas: Digest-keyed quota table built at startup.

        Returns:
            None.

        Raises:
            Nothing.
        """
        self.enabled: bool = bool(enabled)
        self.max_requests: int = max(1, int(max_requests))
        self.window_seconds: int = max(1, int(window_seconds))
        self.trust_forwarded_for: bool = bool(trust_forwarded_for)
        self.api_key_quotas: Dict[bytes, ApiKeyEntry] = dict(api_key_quotas)
        self.ip_buckets: Dict[bytes, Tuple[float, int]] = {}
        self.key_buckets: Dict[bytes, Tuple[float, int]] = {}
        self.clock: Callable[[], float] = time.monotonic
        self.lock = threading.Lock()

    def reset(self) -> None:
        """Discard all counters.

        Returns:
            None.

        Raises:
            Nothing.
        """
        with self.lock:
            self.ip_buckets.clear()
            self.key_buckets.clear()

    def _prune_locked(self, buckets: Dict[bytes, Tuple[float, int]], now: float) -> None:
        """Bound a bucket map, dropping stale then oldest entries.

        Must be called with :attr:`lock` held.

        Args:
            buckets: The map to prune in place.
            now: Current clock reading.

        Returns:
            None.

        Raises:
            Nothing.
        """
        if len(buckets) <= MAX_TRACKED_KEYS:
            return
        stale = [key for key, (started, _) in buckets.items() if now - started >= self.window_seconds]
        for key in stale:
            del buckets[key]
        overflow = len(buckets) - MAX_TRACKED_KEYS
        if overflow <= 0:
            return
        oldest = sorted(buckets.items(), key=lambda item: item[1][0])[:overflow]
        for key, _ in oldest:
            del buckets[key]

    def _consume(
        self, buckets: Dict[bytes, Tuple[float, int]], identity: bytes, quota: int
    ) -> RateLimitDecision:
        """Count one request against a bucket and decide whether to allow it.

        Args:
            buckets: The namespace to charge (per-IP or per-key).
            identity: Salted digest identifying the caller within that namespace.
            quota: Requests permitted per window for this identity.

        Returns:
            A :class:`RateLimitDecision`.

        Raises:
            Nothing.
        """
        with self.lock:
            now = self.clock()
            started, count = buckets.get(identity, (now, 0))
            if now - started >= self.window_seconds:
                started, count = now, 0
            if count >= quota:
                remaining = (started + self.window_seconds) - now
                retry_after = int(math.ceil(remaining)) if remaining > 0 else 1
                retry_after = max(1, min(self.window_seconds, retry_after))
                buckets[identity] = (started, count)
                return RateLimitDecision(allowed=False, retry_after=retry_after)
            buckets[identity] = (started, count + 1)
            self._prune_locked(buckets, now)
            return RateLimitDecision(allowed=True, retry_after=0)

    def check_create(self, api_key: Optional[str], client_address: str) -> RateLimitDecision:
        """Consult the limiter for one POST /api/links request.

        A recognised API key is charged to its own bucket using its own quota and
        the per-IP bucket is neither read nor written. No key, a blank key or an
        unrecognised key falls through to the per-IP bucket and allocates nothing
        keyed by the header value.

        Args:
            api_key: Raw ``X-API-Key`` header value, if present.
            client_address: Client identity for the per-IP bucket.

        Returns:
            A :class:`RateLimitDecision`; always allowed when limiting is disabled.

        Raises:
            Nothing.
        """
        if not self.enabled:
            return RateLimitDecision(allowed=True, retry_after=0)
        entry = lookup_api_key(self.api_key_quotas, api_key)
        if entry is not None:
            return self._consume(self.key_buckets, entry.digest, entry.quota)
        identity = salted_digest(client_address.encode("utf-8", errors="replace"))
        return self._consume(self.ip_buckets, identity, self.max_requests)


def header_value(scope: Scope, name: bytes) -> Optional[str]:
    """Read a single request header from an ASGI scope.

    Args:
        scope: The ASGI HTTP scope.
        name: Lower-case header name as bytes.

    Returns:
        The decoded header value, or ``None`` when absent or undecodable.

    Raises:
        Nothing.
    """
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name == name:
            try:
                return raw_value.decode("utf-8")
            except UnicodeDecodeError:
                return None
    return None


def client_address(scope: Scope, trust_forwarded_for: bool) -> str:
    """Determine the per-IP bucket identity for a request.

    Args:
        scope: The ASGI HTTP scope.
        trust_forwarded_for: Whether to honour ``X-Forwarded-For``.

    Returns:
        The client address text, or ``"unknown"`` when it cannot be determined.

    Raises:
        Nothing.
    """
    if trust_forwarded_for:
        forwarded = header_value(scope, b"x-forwarded-for")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first[:255]
    client = scope.get("client")
    if client and isinstance(client, (tuple, list)) and client[0]:
        return str(client[0])[:255]
    return "unknown"


def _normalised_path(scope: Scope) -> str:
    """Return the request path without root prefix or trailing slash.

    Args:
        scope: The ASGI HTTP scope.

    Returns:
        The normalised path, always beginning with ``/``.

    Raises:
        Nothing.
    """
    path = scope.get("path") or "/"
    root_path = scope.get("root_path") or ""
    if root_path and path.startswith(root_path):
        path = path[len(root_path) :] or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return path


class RateLimitMiddleware:
    """ASGI middleware applying :class:`RateLimiter` to link creation only."""

    def __init__(self, app: ASGIApp, limiter: RateLimiter) -> None:
        """Wrap an ASGI application.

        Args:
            app: The next application in the stack.
            limiter: Shared limiter instance (also exposed on ``app.state``).

        Returns:
            None.

        Raises:
            Nothing.
        """
        self.app = app
        self.limiter = limiter

    def _guards(self, scope: Scope) -> bool:
        """Report whether this request is subject to limiting.

        Args:
            scope: The ASGI HTTP scope.

        Returns:
            ``True`` only for ``POST /api/links``.

        Raises:
            Nothing.
        """
        method = str(scope.get("method", "")).upper()
        return method == "POST" and _normalised_path(scope) == CREATE_LINK_PATH

    async def _send_rate_limited(self, send: Send, retry_after: int) -> None:
        """Emit the 429 refusal response.

        Args:
            send: The ASGI send callable.
            retry_after: Whole seconds for the ``Retry-After`` header.

        Returns:
            None.

        Raises:
            Nothing.
        """
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(RATE_LIMITED_BODY)).encode("ascii")),
            (b"retry-after", str(retry_after).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]
        await send({"type": "http.response.start", "status": 429, "headers": headers})
        await send({"type": "http.response.body", "body": RATE_LIMITED_BODY, "more_body": False})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI event stream.

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive callable.
            send: The ASGI send callable.

        Returns:
            None.

        Raises:
            Exception: Anything raised by the wrapped application.
        """
        if scope.get("type") != "http" or not self._guards(scope):
            await self.app(scope, receive, send)
            return
        decision = self.limiter.check_create(
            header_value(scope, b"x-api-key"),
            client_address(scope, self.limiter.trust_forwarded_for),
        )
        if decision.allowed:
            await self.app(scope, receive, send)
            return
        await self._send_rate_limited(send, decision.retry_after)
