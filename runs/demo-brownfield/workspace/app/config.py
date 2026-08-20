"""Environment driven configuration for the URL shortener.

All values are read from environment variables with safe defaults.  Parsing is
fail-soft: a malformed value degrades to its default rather than preventing the
process from starting.  No secret is ever hard coded here; API keys are read
from the environment only and are never logged.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

logger = logging.getLogger("shortener.config")

#: Hard upper bound for any URL string accepted at the boundary.
ABSOLUTE_MAX_URL_LENGTH: int = 8192

#: Hard upper bound for a requested time to live (roughly ten years).
ABSOLUTE_MAX_TTL_SECONDS: int = 315_360_000

_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})

DEFAULT_DB_PATH = "./links.db"
DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_CODE_LENGTH = 7
DEFAULT_CODE_MAX_ATTEMPTS = 5
DEFAULT_TTL_SECONDS = 0
DEFAULT_MAX_URL_LENGTH = 2048
DEFAULT_RATE_LIMIT_ENABLED = True
DEFAULT_RATE_LIMIT_MAX = 60
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class Config:
    """Immutable snapshot of the process configuration.

    Attributes mirror the documented environment variables one for one.
    """

    db_path: str
    base_url: str
    code_length: int
    code_max_attempts: int
    default_ttl_seconds: int
    max_url_length: int
    rate_limit_enabled: bool
    rate_limit_max: int
    rate_limit_window_seconds: float
    api_key_quotas: Mapping[str, int]


def _env_str(name: str, default: str) -> str:
    """Read a string environment variable.

    Returns the stripped value, or ``default`` when unset or blank.  Raises
    nothing.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip()
    return value if value else default


def _env_int(name: str, default: int, minimum: int, maximum: Optional[int] = None) -> int:
    """Read an integer environment variable, clamped to a safe range.

    Returns the parsed integer clamped between ``minimum`` and ``maximum``, or
    ``default`` when the variable is unset or unparseable.  Raises nothing.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning("Ignoring unparseable integer for %s; using default", name)
        return default
    if value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def _env_float(name: str, default: float, minimum: float) -> float:
    """Read a float environment variable, clamped to a safe minimum.

    Returns the parsed float (never below ``minimum``), or ``default`` when the
    variable is unset or unparseable.  Raises nothing.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        logger.warning("Ignoring unparseable number for %s; using default", name)
        return default
    return value if value >= minimum else minimum


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean environment variable.

    Returns ``False`` for "false"/"0"/"no"/"off" and ``True`` for
    "true"/"1"/"yes"/"on" (case-insensitive); any other value yields
    ``default``.  Raises nothing.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _FALSE_VALUES:
        return False
    if value in _TRUE_VALUES:
        return True
    logger.warning("Ignoring unrecognised boolean for %s; using default", name)
    return default


def parse_api_keys(raw: Optional[str]) -> Mapping[str, int]:
    """Parse the SHORTENER_API_KEYS declaration into a key -> quota mapping.

    Accepts a comma separated list of ``NAME:QUOTA`` pairs.  An entry is kept
    only when it has exactly one ':' separator, a non-empty name after
    stripping and a quota that parses as an integer >= 1; every other entry is
    skipped.  On duplicate names the last declaration wins.

    Returns an immutable mapping (possibly empty).  Raises nothing: malformed
    input degrades to fewer recognised keys, never to a startup failure.
    """
    quotas: dict[str, int] = {}
    skipped = 0
    if not raw:
        return MappingProxyType({})
    for entry in raw.split(","):
        if not entry.strip():
            # A stray comma or trailing separator is not an error.
            continue
        if entry.count(":") != 1:
            skipped += 1
            continue
        name_part, quota_part = entry.split(":", 1)
        name = name_part.strip()
        quota_text = quota_part.strip()
        if not name:
            skipped += 1
            continue
        try:
            quota = int(quota_text)
        except ValueError:
            skipped += 1
            continue
        if quota < 1:
            skipped += 1
            continue
        quotas[name] = quota
    if skipped:
        # Never log the entry text itself: it may contain key material.
        logger.warning("Skipped %d malformed SHORTENER_API_KEYS entries", skipped)
    return MappingProxyType(dict(quotas))


def load_config() -> Config:
    """Build a Config snapshot from the current process environment.

    Returns a fully populated, immutable :class:`Config`.  Raises nothing:
    every individual value falls back to its documented default when missing or
    malformed.
    """
    base_url = _env_str("LINKS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    if not base_url:
        base_url = DEFAULT_BASE_URL
    return Config(
        db_path=_env_str("LINKS_DB_PATH", DEFAULT_DB_PATH),
        base_url=base_url,
        code_length=_env_int("LINKS_CODE_LENGTH", DEFAULT_CODE_LENGTH, minimum=1, maximum=64),
        code_max_attempts=_env_int(
            "LINKS_CODE_MAX_ATTEMPTS", DEFAULT_CODE_MAX_ATTEMPTS, minimum=1, maximum=50
        ),
        default_ttl_seconds=_env_int(
            "LINKS_DEFAULT_TTL_SECONDS",
            DEFAULT_TTL_SECONDS,
            minimum=0,
            maximum=ABSOLUTE_MAX_TTL_SECONDS,
        ),
        max_url_length=_env_int(
            "LINKS_MAX_URL_LENGTH",
            DEFAULT_MAX_URL_LENGTH,
            minimum=1,
            maximum=ABSOLUTE_MAX_URL_LENGTH,
        ),
        rate_limit_enabled=_env_bool("LINKS_RATE_LIMIT_ENABLED", DEFAULT_RATE_LIMIT_ENABLED),
        rate_limit_max=_env_int("LINKS_RATE_LIMIT_MAX", DEFAULT_RATE_LIMIT_MAX, minimum=1),
        rate_limit_window_seconds=_env_float(
            "LINKS_RATE_LIMIT_WINDOW_SECONDS",
            DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
            minimum=0.001,
        ),
        api_key_quotas=parse_api_keys(os.environ.get("SHORTENER_API_KEYS")),
    )
