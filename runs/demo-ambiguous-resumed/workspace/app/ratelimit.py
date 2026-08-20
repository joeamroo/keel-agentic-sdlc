"""In-process fixed window rate limiting.

The limiter is a per-node hash map of ``scope:client_ip`` to a window counter. It
is intentionally not persisted: it must not add a write to the hot creation path.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

_MAX_TRACKED_KEYS = 100000


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of a rate limit check."""

    allowed: bool
    retry_after: int
    remaining: int


class FixedWindowRateLimiter:
    """Thread safe fixed window counter keyed by an opaque string."""

    def __init__(self, window_seconds: int) -> None:
        """Create a limiter.

        Args:
            window_seconds: Length of the fixed window; values below 1 are clamped to 1.

        Returns:
            None.

        Raises:
            Nothing.
        """
        self._window = max(1, int(window_seconds))
        self._buckets: Dict[str, Tuple[int, int]] = {}
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    @property
    def window_seconds(self) -> int:
        """Return the configured window length in seconds.

        Returns:
            The window length used for bucketing and for ``Retry-After``.

        Raises:
            Nothing.
        """
        return self._window

    def check(self, key: str, limit: int, now: Optional[float] = None) -> RateLimitDecision:
        """Count one request against ``key`` and decide whether it may proceed.

        Args:
            key: Opaque bucket key, e.g. ``"create:198.51.100.7"``.
            limit: Maximum requests allowed inside the current window.
            now: Unix seconds; defaults to the current wall clock.

        Returns:
            A :class:`RateLimitDecision`; when ``allowed`` is ``False`` the
            ``retry_after`` field holds the whole seconds remaining in the window
            (minimum 1).

        Raises:
            Nothing.
        """
        moment = time.time() if now is None else now
        window_start = int(moment // self._window) * self._window
        retry_after = max(1, int(math.ceil(window_start + self._window - moment)))
        effective_limit = max(0, int(limit))

        with self._lock:
            self._sweep_locked(window_start, moment)
            bucket = self._buckets.get(key)
            count = bucket[1] + 1 if bucket is not None and bucket[0] == window_start else 1
            if count > effective_limit:
                self._buckets[key] = (window_start, effective_limit + 1)
                return RateLimitDecision(allowed=False, retry_after=retry_after, remaining=0)
            self._buckets[key] = (window_start, count)
            return RateLimitDecision(
                allowed=True,
                retry_after=retry_after,
                remaining=max(0, effective_limit - count),
            )

    def reset(self) -> None:
        """Drop all counters.

        Returns:
            None.

        Raises:
            Nothing.
        """
        with self._lock:
            self._buckets.clear()
            self._last_sweep = 0.0

    def _sweep_locked(self, window_start: int, moment: float) -> None:
        """Evict buckets belonging to elapsed windows; caller must hold the lock.

        Args:
            window_start: Start epoch of the current window.
            moment: Current unix seconds.

        Returns:
            None.

        Raises:
            Nothing.
        """
        if moment - self._last_sweep < self._window and len(self._buckets) < _MAX_TRACKED_KEYS:
            return
        self._last_sweep = moment
        stale = [key for key, value in self._buckets.items() if value[0] != window_start]
        for key in stale:
            self._buckets.pop(key, None)
