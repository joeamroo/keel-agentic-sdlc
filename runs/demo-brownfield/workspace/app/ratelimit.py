"""Fixed-window rate limiting, enforced once in ASGI middleware.

The middleware runs ahead of routing and body parsing, so a throttled request
never opens a database connection and never has its body read.

Two structurally identical but separate bucket maps are kept:

``ip_buckets``
    keyed on a per-boot salted hash of the peer address, used for the anonymous
    creation budget (``LINKS_RATE_LIMIT_MAX``) and for redirects
    (``LINKS_RATE_LIMIT_MAX * REDIRECT_LIMIT_MULTIPLIER``).

``api_key_buckets``
    keyed on a per-boot salted hash of a recognised ``X-API-Key`` value, used
    for that key's own creation quota.

Each map is pruned independently at ``LINKS_MAX_TRACKED_KEYS`` entries, so API
key traffic cannot evict per-IP buckets. Identities are never stored or logged
in the clear: bucket ids are ``class:blake2s(boot_salt || value)[:16]``.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings
from .errors import error_payload

logger = logging.getLogger("app.ratelimit")

# Redirects are far cheaper than creations and are shared by every visitor of a
# link, so the redirect bucket is a large multiple of the creation budget.
REDIRECT_LIMIT_MULTIPLIER = 100

CREATE_PATH = "/api/links"
BUCKET_CLASS_CREATE = "create"
BUCKET_CLASS_CREATE_KEY = "create_key"
BUCKET_CLASS_REDIRECT = "redirect"

API_KEY_HEADER = b"x-api-key"
MAX_API_KEY_HEADER_LENGTH = 256

_REDIRECT_PATH_PATTERN = re.compile(r"^/[0-9A-Za-z_-]{1,64}$")
_RESERVED_PATHS = frozenset({"/health", "/docs", "/redoc", "/openapi", "/favicon"})


def _default_clock() -> float:
    """Return the current monotonic time in seconds.

    Looked up through the ``time`` module on every call so tests can patch it.
    Raises nothing.
    """
    return time.monotonic()


@dataclass(frozen=True)
class Decision:
    """Outcome of a limiter check."""

    allowed: bool
    retry_after: int
    bucket_id: str


@dataclass(frozen=True)
class BucketPlan:
    """Which bucket a request is charged to, and at what limit."""

    bucket_class: str
    identity: str
    limit: int
    keyed: bool


class RateLimiterState:
    """In-process fixed-window counters shared by every worker thread.

    State is never serialised to SQLite, the WAL or any file.
    """

    def __init__(self, settings: Settings, clock: Optional[Callable[[], float]] = None) -> None:
        """Create limiter state for a settings snapshot.

        ``clock`` is an injectable monotonic time source (defaults to
        ``time.monotonic``) so tests can advance the window without sleeping.
        Raises nothing.
        """
        self.settings = settings
        self.clock: Callable[[], float] = clock if clock is not None else _default_clock
        self._lock = threading.Lock()
        self._boot_salt = secrets.token_bytes(16)
        self.ip_buckets: "OrderedDict[str, Tuple[float, int]]" = OrderedDict()
        self.api_key_buckets: "OrderedDict[str, Tuple[float, int]]" = OrderedDict()

    def bucket_id(self, bucket_class: str, identity: str) -> str:
        """Derive the opaque bucket id for an identity.

        Returns ``class:digest`` where digest is a truncated blake2s over the
        per-boot salt and the identity, so neither an API key nor a client
        address can be recovered from limiter state or logs. Raises nothing.
        """
        digest = hashlib.blake2s(
            self._boot_salt + identity.encode("utf-8", "replace")
        ).hexdigest()[:16]
        return "%s:%s" % (bucket_class, digest)

    def _prune(self, buckets: "OrderedDict[str, Tuple[float, int]]") -> None:
        """Evict least recently used entries beyond the configured cap.

        Returns None. Raises nothing. Caller must hold the lock.
        """
        limit = self.settings.max_tracked_keys
        while len(buckets) > limit:
            buckets.popitem(last=False)

    def consume(self, plan: BucketPlan) -> Decision:
        """Charge one request against the planned bucket.

        Returns a :class:`Decision`; when it is not allowed, ``retry_after`` is
        a whole number of seconds in ``[1, window]``. Raises nothing.
        """
        buckets = self.api_key_buckets if plan.keyed else self.ip_buckets
        bucket_id = self.bucket_id(plan.bucket_class, plan.identity)
        window = float(self.settings.rate_limit_window_seconds)
        with self._lock:
            now = float(self.clock())
            entry = buckets.get(bucket_id)
            if entry is None or (now - entry[0]) >= window or (now - entry[0]) < 0:
                buckets[bucket_id] = (now, 1)
                buckets.move_to_end(bucket_id)
                self._prune(buckets)
                return Decision(True, 0, bucket_id)
            window_start, count = entry
            if count >= plan.limit:
                remaining = window - (now - window_start)
                retry_after = max(1, min(int(window), int(math.ceil(remaining))))
                return Decision(False, retry_after, bucket_id)
            buckets[bucket_id] = (window_start, count + 1)
            buckets.move_to_end(bucket_id)
            return Decision(True, 0, bucket_id)

    def advance(self, seconds: float) -> None:
        """Pretend ``seconds`` have elapsed by rewinding every window start.

        Intended for tests that must cross a window boundary without sleeping.
        Returns None. Raises nothing.
        """
        with self._lock:
            for buckets in (self.ip_buckets, self.api_key_buckets):
                for bucket_id, (window_start, count) in list(buckets.items()):
                    buckets[bucket_id] = (window_start - seconds, count)

    def reset(self) -> None:
        """Drop all counters.

        Returns None. Raises nothing.
        """
        with self._lock:
            self.ip_buckets.clear()
            self.api_key_buckets.clear()


def _header_value(scope: Scope, name: bytes) -> Optional[str]:
    """Read a single request header from the raw ASGI scope.

    Returns the trimmed value of the first matching header, or None when the
    header is absent or undecodable. Raises nothing.
    """
    for raw_name, raw_value in scope.get("headers") or ():
        if raw_name == name:
            try:
                return raw_value.decode("latin-1").strip()
            except (UnicodeDecodeError, AttributeError):
                return None
    return None


def _client_identity(scope: Scope) -> str:
    """Return the peer address used as the per-IP limiter identity.

    Only the transport peer is used; forwarded headers are ignored because a
    client can trivially forge them. Returns ``"unknown"`` when the server does
    not expose a peer. Raises nothing.
    """
    client = scope.get("client")
    if client and isinstance(client, (tuple, list)) and client[0]:
        return str(client[0])
    return "unknown"


def _is_redirect_path(path: str) -> bool:
    """Report whether a path is a short-code redirect path.

    Returns True for single segment code-shaped paths that are not reserved
    application paths. Raises nothing.
    """
    if not _REDIRECT_PATH_PATTERN.match(path):
        return False
    return path not in _RESERVED_PATHS


class RateLimitMiddleware:
    """ASGI middleware applying the creation and redirect rate limits."""

    def __init__(self, app: ASGIApp, state: RateLimiterState) -> None:
        """Wrap ``app`` with the limiter backed by ``state``.

        Raises nothing.
        """
        self.app = app
        self.state = state

    def plan(self, scope: Scope) -> Optional[BucketPlan]:
        """Decide which bucket, if any, this request is charged to.

        Creation requests presenting a recognised ``X-API-Key`` are charged
        solely to that key's bucket at that key's quota; every other creation
        request is charged to the per-IP creation bucket. Redirects are always
        charged to the per-IP redirect bucket. Returns None for requests that
        are not rate limited (health, stats, docs, everything else). Raises
        nothing.
        """
        settings = self.state.settings
        method = scope.get("method", "")
        path = scope.get("path", "")
        if method == "POST" and path == CREATE_PATH:
            quotas = settings.api_key_quotas
            if quotas:
                presented = _header_value(scope, API_KEY_HEADER)
                if (
                    presented
                    and len(presented) <= MAX_API_KEY_HEADER_LENGTH
                    and presented in quotas
                ):
                    return BucketPlan(
                        bucket_class=BUCKET_CLASS_CREATE_KEY,
                        identity=presented,
                        limit=int(quotas[presented]),
                        keyed=True,
                    )
            return BucketPlan(
                bucket_class=BUCKET_CLASS_CREATE,
                identity=_client_identity(scope),
                limit=settings.rate_limit_max,
                keyed=False,
            )
        if method in ("GET", "HEAD") and _is_redirect_path(path):
            return BucketPlan(
                bucket_class=BUCKET_CLASS_REDIRECT,
                identity=_client_identity(scope),
                limit=settings.rate_limit_max * REDIRECT_LIMIT_MULTIPLIER,
                keyed=False,
            )
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Apply the limit and either forward the request or answer with 429.

        Returns None. Raises whatever the wrapped application raises.
        """
        if scope.get("type") != "http" or not self.state.settings.rate_limit_enabled:
            await self.app(scope, receive, send)
            return

        plan = self.plan(scope)
        if plan is None:
            await self.app(scope, receive, send)
            return

        decision = self.state.consume(plan)
        logger.debug(
            "rate limit check bucket=%s allowed=%s", decision.bucket_id, decision.allowed
        )
        if decision.allowed:
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            status_code=429,
            content=error_payload(
                "rate_limited", "Rate limit exceeded. Please retry later."
            ),
            headers={
                "Retry-After": str(decision.retry_after),
                "Cache-Control": "no-store",
            },
        )
        await response(scope, receive, send)
