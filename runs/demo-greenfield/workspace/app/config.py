"""Environment driven configuration for the URL shortener service.

All settings come from environment variables with the exact names and defaults
fixed by the design document. No secrets are read or stored here.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Mapping, Optional

logger = logging.getLogger("links.config")

#: Hard ceiling applied to the accepted ``url`` length regardless of
#: ``LINKS_MAX_URL_LENGTH``.  It bounds the string that ever reaches the
#: database and the pydantic request model.
ABSOLUTE_MAX_URL_LENGTH = 8192

#: Hard ceiling for the client supplied ``expires_at`` string.
MAX_EXPIRES_AT_LENGTH = 64

#: Hard ceiling for ``LINKS_BASE_URL``.
MAX_BASE_URL_LENGTH = 512

_FALSE_VALUES = frozenset({"false", "0", "no"})
_TRUE_VALUES = frozenset({"true", "1", "yes"})

_VALID_LOG_LEVELS = frozenset(
    {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
)


def _get(env: Mapping[str, str], name: str, default: str) -> str:
    """Read a raw environment value.

    Returns the value for ``name`` in ``env`` or ``default`` when the variable
    is missing or empty. Raises nothing.
    """
    raw = env.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
        return default
    return raw


def _get_int(
    env: Mapping[str, str],
    name: str,
    default: str,
    minimum: int,
    maximum: int,
) -> int:
    """Read an integer setting, clamped to a safe range.

    Returns the parsed integer, or the parsed default when the environment
    value is not an integer or falls outside ``[minimum, maximum]``. Raises
    nothing; an unusable value is logged and the default is used so a typo in
    deployment configuration cannot take the service down.
    """
    raw = _get(env, name, default)
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s; using default %s", name, default)
        value = int(default)
    if value < minimum or value > maximum:
        logger.warning(
            "Value for %s outside [%d, %d]; using default %s",
            name,
            minimum,
            maximum,
            default,
        )
        value = int(default)
    return value


def _get_bool_default_true(env: Mapping[str, str], name: str) -> bool:
    """Read a boolean setting that defaults to true.

    Returns ``False`` only for the case-insensitive values ``false``, ``0`` or
    ``no``; every other value (including absence) yields ``True``. Raises
    nothing.
    """
    raw = _get(env, name, "true").lower()
    return raw not in _FALSE_VALUES


def _get_bool_default_false(env: Mapping[str, str], name: str) -> bool:
    """Read a boolean setting that defaults to false.

    Returns ``True`` only for the case-insensitive values ``true``, ``1`` or
    ``yes``; every other value (including absence) yields ``False``. Raises
    nothing.
    """
    raw = _get(env, name, "false").lower()
    return raw in _TRUE_VALUES


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the service configuration."""

    db_path: str
    base_url: str
    default_ttl_days: int
    max_url_length: int
    code_length: int
    code_max_attempts: int
    rate_limit_max: int
    rate_limit_window_seconds: int
    rate_limit_enabled: bool
    trust_forwarded_for: bool
    stats_default_limit: int
    stats_max_limit: int
    dns_resolution_enabled: bool
    log_level: str

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        """Build a :class:`Settings` instance from the process environment.

        ``env`` defaults to :data:`os.environ`. Returns a fully populated,
        range-checked settings object. Raises nothing: invalid values fall back
        to the documented defaults.
        """
        source: Mapping[str, str] = os.environ if env is None else env

        db_path = _get(source, "LINKS_DB_PATH", "./links.db")
        base_url = _get(source, "LINKS_BASE_URL", "http://localhost:8000").rstrip("/")
        if len(base_url) > MAX_BASE_URL_LENGTH:
            logger.warning("LINKS_BASE_URL too long; using default")
            base_url = "http://localhost:8000"

        max_url_length = _get_int(
            source, "LINKS_MAX_URL_LENGTH", "2048", 1, ABSOLUTE_MAX_URL_LENGTH
        )
        stats_max_limit = _get_int(source, "LINKS_STATS_MAX_LIMIT", "500", 1, 10000)
        stats_default_limit = _get_int(
            source, "LINKS_STATS_DEFAULT_LIMIT", "50", 1, 10000
        )
        if stats_default_limit > stats_max_limit:
            stats_default_limit = stats_max_limit

        log_level = _get(source, "LINKS_LOG_LEVEL", "INFO").upper()
        if log_level not in _VALID_LOG_LEVELS:
            log_level = "INFO"

        return cls(
            db_path=db_path,
            base_url=base_url,
            default_ttl_days=_get_int(
                source, "LINKS_DEFAULT_TTL_DAYS", "30", 1, 36500
            ),
            max_url_length=max_url_length,
            code_length=_get_int(source, "LINKS_CODE_LENGTH", "7", 4, 32),
            code_max_attempts=_get_int(
                source, "LINKS_CODE_MAX_ATTEMPTS", "5", 1, 100
            ),
            rate_limit_max=_get_int(source, "LINKS_RATE_LIMIT_MAX", "10", 1, 1000000),
            rate_limit_window_seconds=_get_int(
                source, "LINKS_RATE_LIMIT_WINDOW_SECONDS", "60", 1, 86400
            ),
            rate_limit_enabled=_get_bool_default_true(
                source, "LINKS_RATE_LIMIT_ENABLED"
            ),
            trust_forwarded_for=_get_bool_default_false(
                source, "LINKS_TRUST_FORWARDED_FOR"
            ),
            stats_default_limit=stats_default_limit,
            stats_max_limit=stats_max_limit,
            dns_resolution_enabled=_get_bool_default_true(
                source, "LINKS_DNS_RESOLUTION_ENABLED"
            ),
            log_level=log_level,
        )
