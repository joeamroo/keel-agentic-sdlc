"""Environment driven configuration for the URL shortener.

All settings are read exactly once into an immutable :class:`Settings` snapshot
at application construction. Parsing always fails safe: an unusable value is
logged and degraded to the documented default rather than raising.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

logger = logging.getLogger("app.config")

_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"})

# Hard ceilings applied on top of the documented defaults so that a hostile or
# fat-fingered environment cannot push the process into pathological ranges.
_MAX_URL_LENGTH_CEILING = 8192
_MAX_API_KEY_NAME_LENGTH = 128
_MAX_API_KEY_QUOTA = 1_000_000


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the process configuration.

    Attributes mirror the environment variables documented in the README. The
    instance is frozen so that no request handler can mutate configuration.
    """

    db_path: str
    base_url: str
    code_length: int
    code_max_attempts: int
    max_url_length: int
    default_ttl_seconds: int
    max_ttl_seconds: int
    rate_limit_enabled: bool
    rate_limit_max: int
    rate_limit_window_seconds: int
    max_tracked_keys: int
    log_level: str
    api_key_quotas: Mapping[str, int]


def _read_str(env: Mapping[str, str], name: str, default: str) -> str:
    """Read a string environment variable.

    Returns the trimmed value, or ``default`` when the variable is unset or
    blank. Raises nothing.
    """
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip()
    return value if value else default


def _read_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Read an integer environment variable with range enforcement.

    Returns the parsed value when it is an integer inside ``[minimum, maximum]``,
    otherwise logs a warning and returns ``default``. Raises nothing.
    """
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning("%s is not an integer; using default %d", name, default)
        return default
    if value < minimum or value > maximum:
        logger.warning(
            "%s=%d is outside the accepted range %d..%d; using default %d",
            name,
            value,
            minimum,
            maximum,
            default,
        )
        return default
    return value


def _read_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    """Read a boolean environment variable.

    Returns True when the trimmed, lower-cased value is one of
    ``true``/``1``/``yes``/``on``; returns ``default`` when unset or blank and
    False otherwise. Raises nothing.
    """
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUE_VALUES


def parse_api_key_quotas(raw: Optional[str]) -> Mapping[str, int]:
    """Parse ``SHORTENER_API_KEYS`` into an immutable name -> quota mapping.

    Accepts a comma separated list of ``name:quota`` pairs with arbitrary
    surrounding whitespace. Returns an empty mapping when the value is unset,
    empty or wholly unparsable. Malformed entries are counted and reported
    without ever logging the offending value, because entries may be
    credentials. Raises nothing.
    """
    quotas: "dict[str, int]" = {}
    skipped = 0
    if raw:
        for chunk in raw.split(","):
            item = chunk.strip()
            if not item:
                continue
            name, separator, quota_raw = item.partition(":")
            name = name.strip()
            quota_raw = quota_raw.strip()
            if not separator or not name or not quota_raw:
                skipped += 1
                continue
            if len(name) > _MAX_API_KEY_NAME_LENGTH:
                skipped += 1
                continue
            try:
                quota = int(quota_raw)
            except ValueError:
                skipped += 1
                continue
            if quota < 1 or quota > _MAX_API_KEY_QUOTA:
                skipped += 1
                continue
            quotas[name] = quota
    if skipped:
        logger.warning(
            "Ignored %d malformed entr%s in SHORTENER_API_KEYS",
            skipped,
            "y" if skipped == 1 else "ies",
        )
    return MappingProxyType(dict(quotas))


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Build the frozen settings snapshot from the environment.

    ``env`` defaults to ``os.environ``. Returns a fully populated
    :class:`Settings`. Raises nothing: every unusable value degrades to its
    documented default with a warning.
    """
    source: Mapping[str, str] = env if env is not None else os.environ

    db_path = _read_str(source, "LINKS_DB_PATH", "./data/links.db")
    base_url = _read_str(source, "LINKS_BASE_URL", "http://localhost:8000").rstrip("/")
    if not base_url:
        base_url = "http://localhost:8000"

    log_level_raw = _read_str(source, "LINKS_LOG_LEVEL", "INFO").upper()
    if log_level_raw not in _VALID_LOG_LEVELS:
        logger.warning("LINKS_LOG_LEVEL is not a known level; using INFO")
        log_level_raw = "INFO"

    max_ttl_seconds = _read_int(source, "LINKS_MAX_TTL_SECONDS", 31_536_000, 1, 1_000_000_000)
    default_ttl_seconds = _read_int(source, "LINKS_DEFAULT_TTL_SECONDS", 0, 0, 1_000_000_000)

    return Settings(
        db_path=db_path,
        base_url=base_url,
        code_length=_read_int(source, "LINKS_CODE_LENGTH", 7, 4, 16),
        code_max_attempts=_read_int(source, "LINKS_CODE_MAX_ATTEMPTS", 5, 1, 100),
        max_url_length=_read_int(
            source, "LINKS_MAX_URL_LENGTH", 2048, 1, _MAX_URL_LENGTH_CEILING
        ),
        default_ttl_seconds=default_ttl_seconds,
        max_ttl_seconds=max_ttl_seconds,
        rate_limit_enabled=_read_bool(source, "LINKS_RATE_LIMIT_ENABLED", True),
        rate_limit_max=_read_int(source, "LINKS_RATE_LIMIT_MAX", 10, 1, 1_000_000),
        rate_limit_window_seconds=_read_int(
            source, "LINKS_RATE_LIMIT_WINDOW_SECONDS", 60, 1, 3600
        ),
        max_tracked_keys=_read_int(source, "LINKS_MAX_TRACKED_KEYS", 10_000, 1, 1_000_000),
        log_level=log_level_raw,
        api_key_quotas=parse_api_key_quotas(source.get("SHORTENER_API_KEYS")),
    )
