"""Sliding window rate limiting, enforced in one ASGI middleware.

The middleware runs before routing, before body parsing and before any
database connection is opened, so a throttled request costs almost nothing and
can never leave a half written row.

Buckets are namespaced so a key bucket and an IP bucket can never alias:

* ``post:key:<hash>`` - per API key creation quota (recognised keys only)
* ``post:ip:<hash>``  - per client address creation quota
* ``get:ip:<hash>``   - per client address read/redirect quota

All bucket identity is derived from a per-boot salted blake2b digest, so no
raw API key and no raw client address is ever held in limiter state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    Deque,
    Dict,
    Iterable,
    List,
    MutableMapping,
    Optional,
    Tuple,
)

LOGGER = logging.getLogger("app.ratelimit")

MAX_TRACKED_KEYS = 10000
KEY_BUCKET_PREFIX = "post:key:"
IP_CREATE_BUCKET_PREFIX = "post:ip:"
IP_READ_BUCKET_PREFIX = "get:ip:"
MAX_API_KEY_HEADER_BYTES = 512
API_KEY_HEADER = b"x-api-key"

CATEGORY_CREATE = "create"
CATEGORY_READ = "read"

_UNLIMITED_GET_SEGMENTS = frozenset(
    {"health", "docs", "redoc", "openapi.json", "favicon.ico"}
)

_RATE_LIMITED_BODY = json.dumps(
    {
        "error": {
            "code": "rate_limited",
            "message": "Rate limit exceeded. Please retry later.",
        }
    },
    separators=(",", ":"),
).encode("utf-8")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


@dataclass
class Decision:
    """Outcome of a single limiter consultation."""

    allowed: bool
    retry_after: int


@dataclass
class _Bucket:
    """Sliding window counter for one namespaced bucket id."""

    hits: Deque[float]
    allowance: int
    last_seen: float


def classify_request(method: str, path: str) -> Optional[str]:
    """Decide which limiter category a request belongs to.

    Returns ``"create"`` for ``POST /api/links``, ``"read"`` for ``GET /{code}``
    and ``GET /api/links/{code}/stats``, and ``None`` for everything else
    (health checks and documentation are never limited).  Raises nothing.
    """
    normalized = path.rstrip("/") or "/"
    if method == "POST":
        return CATEGORY_CREATE if normalized == "/api/links" else None
    if method != "GET":
        return None
    segments = [segment for segment in normalized.split("/") if segment]
    if len(segments) == 1 and segments[0] not in _UNLIMITED_GET_SEGMENTS:
        return CATEGORY_READ
    if (
        len(segments) == 4
        and segments[0] == "api"
        and segments[1] == "links"
        and segments[3] == "stats"
    ):
        return CATEGORY_READ
    return None


def extract_api_key(headers: Iterable[Tuple[bytes, bytes]]) -> Optional[str]:
    """Pull the ``X-API-Key`` header value out of a raw ASGI header list.

    Header name matching is case-insensitive (ASGI lower-cases names); the
    value is returned verbatim for exact, case-sensitive comparison.
    Returns ``None`` when the header is absent, empty, longer than
    ``MAX_API_KEY_HEADER_BYTES`` or not valid UTF-8.  Raises nothing.
    """
    for name, value in headers:
        if name.lower() != API_KEY_HEADER:
            continue
        if not value or len(value) > MAX_API_KEY_HEADER_BYTES:
            return None
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def client_host_of(scope: Scope) -> str:
    """Return the peer address of a connection.

    Only the transport level peer is used; forwarding headers are ignored
    because a client can trivially forge them.  Returns ``"unknown"`` when the
    server does not expose a client tuple.  Raises nothing.
    """
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        host = client[0]
        if host:
            return str(host)
    return "unknown"


class RateLimiter:
    """In-process sliding window limiter shared by every limited route."""

    def __init__(
        self,
        *,
        enabled: bool,
        window_seconds: int,
        create_max: int,
        redirect_multiplier: int,
        api_key_entries: Tuple[Tuple[str, int], ...] = (),
    ) -> None:
        """Build a limiter.

        Hashes the configured API key names with a per-boot salt and discards
        the plaintext immediately, so no credential survives in memory.
        Raises nothing.
        """
        self._enabled = bool(enabled)
        self._window = float(max(1, window_seconds))
        self._create_max = max(1, create_max)
        self._read_max = max(1, create_max * max(1, redirect_multiplier))
        self._salt = secrets.token_bytes(16)
        self._lock = threading.Lock()
        self._buckets: Dict[str, _Bucket] = {}
        quotas: Dict[str, int] = {}
        for name, quota in api_key_entries:
            if quota < 1:
                continue
            quotas[self.digest(name)] = int(quota)
        self._key_quotas: Dict[str, int] = quotas
        if quotas:
            LOGGER.info("api key quotas loaded for %d key(s)", len(quotas))

    @property
    def enabled(self) -> bool:
        """Report whether limiting is active.

        Returns ``True`` when buckets are consulted.  Raises nothing.
        """
        return self._enabled

    @property
    def window_seconds(self) -> float:
        """Return the sliding window length in seconds.  Raises nothing."""
        return self._window

    @property
    def create_allowance(self) -> int:
        """Return the per-IP creation allowance.  Raises nothing."""
        return self._create_max

    @property
    def read_allowance(self) -> int:
        """Return the per-IP read/redirect allowance.  Raises nothing."""
        return self._read_max

    def digest(self, value: str) -> str:
        """Hash a credential or address with the per-boot salt.

        Returns a 32 character hex digest.  Raises nothing.
        """
        return hashlib.blake2b(
            value.encode("utf-8", errors="replace"), salt=self._salt, digest_size=16
        ).hexdigest()

    def resolve_key(self, presented: Optional[str]) -> Optional[Tuple[str, int]]:
        """Look up a presented API key value.

        Returns ``(key_hash, quota)`` when the key is recognised, otherwise
        ``None`` - an unknown key is indistinguishable from no key at all.
        Raises nothing.
        """
        if presented is None or not self._key_quotas:
            return None
        key_hash = self.digest(presented)
        quota = self._key_quotas.get(key_hash)
        if quota is None:
            return None
        return key_hash, quota

    def bucket_ids(self) -> Tuple[str, ...]:
        """Return a snapshot of the currently tracked bucket ids.

        Intended for diagnostics and tests only.  Raises nothing.
        """
        with self._lock:
            return tuple(self._buckets.keys())

    def bucket_count(self) -> int:
        """Return how many buckets are currently tracked.  Raises nothing."""
        with self._lock:
            return len(self._buckets)

    def reset(self) -> None:
        """Drop all counters.

        Returns ``None``.  Raises nothing.
        """
        with self._lock:
            self._buckets.clear()

    def _evict_locked(self) -> None:
        """Evict least recently seen IP buckets when the dict is full.

        Key buckets are never evicted because their cardinality is bounded by
        the configuration.  Must be called with the lock held.  Returns
        ``None``.  Raises nothing.
        """
        if len(self._buckets) < MAX_TRACKED_KEYS:
            return
        evictable: List[Tuple[float, str]] = [
            (bucket.last_seen, bucket_id)
            for bucket_id, bucket in self._buckets.items()
            if not bucket_id.startswith(KEY_BUCKET_PREFIX)
        ]
        if not evictable:
            return
        evictable.sort()
        excess = len(self._buckets) - MAX_TRACKED_KEYS + 1
        for _, bucket_id in evictable[:excess]:
            self._buckets.pop(bucket_id, None)

    def consume(self, bucket_id: str, allowance: int) -> Decision:
        """Record one hit against a bucket if the allowance permits it.

        Returns a :class:`Decision`; when it is not allowed, ``retry_after`` is
        the whole number of seconds (rounded up, at least 1, never more than
        the window) until the oldest recorded hit leaves the window.  A denied
        request is not recorded, so denials never extend the window.
        Raises nothing.
        """
        effective = max(1, allowance)
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._buckets.get(bucket_id)
            if bucket is None:
                self._evict_locked()
                bucket = _Bucket(hits=deque(), allowance=effective, last_seen=now)
                self._buckets[bucket_id] = bucket
            bucket.allowance = effective
            bucket.last_seen = now
            while bucket.hits and bucket.hits[0] <= cutoff:
                bucket.hits.popleft()
            if len(bucket.hits) >= effective:
                oldest = bucket.hits[0]
                remaining = (oldest + self._window) - now
                retry_after = int(math.ceil(remaining)) if remaining > 0 else 1
                retry_after = max(1, min(retry_after, int(math.ceil(self._window))))
                return Decision(allowed=False, retry_after=retry_after)
            bucket.hits.append(now)
            return Decision(allowed=True, retry_after=0)

    def check_request(self, scope: Scope) -> Decision:
        """Apply the limiter to one HTTP scope.

        Chooses the per-key creation bucket for a recognised ``X-API-Key`` on
        ``POST /api/links``, the per-IP creation bucket otherwise, and the
        per-IP read bucket for redirect and stats requests.  Unlimited routes
        always return an allowed decision.  Raises nothing.
        """
        if not self._enabled:
            return Decision(allowed=True, retry_after=0)
        method = str(scope.get("method", ""))
        path = str(scope.get("path", "/"))
        category = classify_request(method, path)
        if category is None:
            return Decision(allowed=True, retry_after=0)
        if category == CATEGORY_CREATE:
            headers = scope.get("headers") or []
            resolved = self.resolve_key(extract_api_key(headers))
            if resolved is not None:
                key_hash, quota = resolved
                return self.consume(KEY_BUCKET_PREFIX + key_hash, quota)
            host_hash = self.digest(client_host_of(scope))
            return self.consume(IP_CREATE_BUCKET_PREFIX + host_hash, self._create_max)
        host_hash = self.digest(client_host_of(scope))
        return self.consume(IP_READ_BUCKET_PREFIX + host_hash, self._read_max)


async def send_rate_limited(send: Send, retry_after: int) -> None:
    """Emit the canned 429 response.

    Returns ``None``.  Raises whatever the ASGI ``send`` callable raises.
    """
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(_RATE_LIMITED_BODY)).encode("ascii")),
        (b"retry-after", str(max(1, retry_after)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    await send({"type": "http.response.start", "status": 429, "headers": headers})
    await send({"type": "http.response.body", "body": _RATE_LIMITED_BODY})


class RateLimitMiddleware:
    """Pure ASGI middleware enforcing every rate limit in one place."""

    def __init__(self, app: ASGIApp, limiter: RateLimiter) -> None:
        """Wrap an ASGI application with the limiter.

        Raises nothing.
        """
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI event stream.

        Consults the limiter before routing and body parsing; on refusal emits
        a 429 with ``Retry-After`` and ``Cache-Control: no-store`` and never
        calls the wrapped application.  Returns ``None``.  Raises whatever the
        wrapped application raises.
        """
        if scope.get("type") != "http" or not self.limiter.enabled:
            await self.app(scope, receive, send)
            return
        decision = self.limiter.check_request(scope)
        if not decision.allowed:
            await send_rate_limited(send, decision.retry_after)
            return
        await self.app(scope, receive, send)
