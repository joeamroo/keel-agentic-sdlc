"""Environment-driven configuration for the URL shortener service.

All settings come from environment variables with safe defaults. No secret is
stored in this module: API key material is read separately (and never retained in
plaintext) by :mod:`app.apikeys`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Mapping, Optional

LOGGER = logging.getLogger("shortener.config")

DEFAULT_DB_PATH: str = "./links.db"
DEFAULT_BASE_URL: str = "http://localhost:8000"
DEFAULT_RATE_LIMIT_ENABLED: bool = True
DEFAULT_RATE_LIMIT_MAX: int = 10
DEFAULT_RATE_LIMIT_WINDOW_SECONDS: int = 60
DEFAULT_TRUST_FORWARDED_FOR: bool = False

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of service configuration.

    Attributes:
        db_path: Filesystem path of the SQLite database file.
        base_url: Origin used to build absolute short URLs, without a trailing slash.
        rate_limit_enabled: Master switch for all rate limiting.
        rate_limit_max: Per-window allowance of the per-IP creation bucket.
        rate_limit_window_seconds: Fixed-window length shared by every bucket.
        trust_forwarded_for: Whether the leftmost X-Forwarded-For entry identifies
            the client for the per-IP bucket.
    """

    db_path: str
    base_url: str
    rate_limit_enabled: bool
    rate_limit_max: int
    rate_limit_window_seconds: int
    trust_forwarded_for: bool


def _env_str(env: Mapping[str, str], name: str, default: str) -> str:
    """Read a non-empty string environment variable.

    Args:
        env: Mapping to read from.
        name: Variable name.
        default: Value used when the variable is missing or blank.

    Returns:
        The stripped configured value, or ``default``.

    Raises:
        Nothing.
    """
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip()
    return value if value else default


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    """Read a boolean environment variable.

    Args:
        env: Mapping to read from.
        name: Variable name.
        default: Value used when the variable is missing, blank or unrecognised.

    Returns:
        The parsed boolean, or ``default`` when the value is not recognised.

    Raises:
        Nothing.
    """
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    LOGGER.warning("Unrecognised boolean for %s; using default %s", name, default)
    return default


def _env_int(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    """Read a bounded integer environment variable.

    Args:
        env: Mapping to read from.
        name: Variable name.
        default: Value used when the variable is missing, blank, non-numeric or
            below ``minimum``.
        minimum: Smallest acceptable value.

    Returns:
        The parsed integer, or ``default``.

    Raises:
        Nothing.
    """
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip(), 10)
    except ValueError:
        LOGGER.warning("Non-integer value for %s; using default %d", name, default)
        return default
    if value < minimum:
        LOGGER.warning("Value for %s below minimum %d; using default %d", name, minimum, default)
        return default
    return value


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Build a :class:`Settings` instance from the environment.

    Args:
        env: Mapping to read from; defaults to ``os.environ``.

    Returns:
        A fully populated, immutable :class:`Settings`.

    Raises:
        Nothing: malformed values fall back to documented defaults.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    base_url = _env_str(source, "LINKS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    if not base_url:
        base_url = DEFAULT_BASE_URL
    return Settings(
        db_path=_env_str(source, "LINKS_DB_PATH", DEFAULT_DB_PATH),
        base_url=base_url,
        rate_limit_enabled=_env_bool(source, "LINKS_RATE_LIMIT_ENABLED", DEFAULT_RATE_LIMIT_ENABLED),
        rate_limit_max=_env_int(source, "LINKS_RATE_LIMIT_MAX", DEFAULT_RATE_LIMIT_MAX, 1),
        rate_limit_window_seconds=_env_int(
            source, "LINKS_RATE_LIMIT_WINDOW_SECONDS", DEFAULT_RATE_LIMIT_WINDOW_SECONDS, 1
        ),
        trust_forwarded_for=_env_bool(source, "LINKS_TRUST_FORWARDED_FOR", DEFAULT_TRUST_FORWARDED_FOR),
    )
