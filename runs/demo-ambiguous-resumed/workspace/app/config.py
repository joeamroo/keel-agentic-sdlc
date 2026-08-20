"""Environment driven configuration with safe defaults.

Every knob is read from the environment variable names given in the design; an
unparsable or out of range value falls back to the documented default and logs a
warning rather than crashing the process.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Dict, Final, Mapping, Optional
from urllib.parse import urlsplit

LOGGER = logging.getLogger("links.config")

DEFAULTS: Final[Dict[str, str]] = {
    "LINKS_DB_PATH": "./links.db",
    "LINKS_BIND_HOST": "127.0.0.1",
    "LINKS_BIND_PORT": "8080",
    "LINKS_PUBLIC_BASE_URL": "https://short.example.com",
    "LINKS_DEFAULT_EXPIRY_DAYS": "30",
    "LINKS_MAX_EXPIRY_DAYS": "365",
    "LINKS_MAX_URL_LENGTH": "2048",
    "LINKS_CODE_LENGTH": "7",
    "LINKS_CODE_MAX_ATTEMPTS": "5",
    "LINKS_RATE_LIMIT_MAX": "20",
    "LINKS_RATE_LIMIT_WINDOW_SECONDS": "60",
    "LINKS_TRUST_PROXY_HEADER": "false",
    "LINKS_DNS_TIMEOUT_MS": "2000",
    "LINKS_DNS_CACHE_TTL_SECONDS": "0",
    "LINKS_ALLOW_PRIVATE_DESTINATIONS": "false",
    "LINKS_LOG_LEVEL": "info",
}

_LOG_LEVELS: Final[Dict[str, int]] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_UVICORN_LOG_LEVELS: Final[Dict[str, str]] = {
    "debug": "debug",
    "info": "info",
    "warn": "warning",
    "warning": "warning",
    "error": "error",
}

# Redirects are rate limited too, but far more generously than creation because a
# popular link is meant to be shared widely. Derived from LINKS_RATE_LIMIT_MAX so
# that no configuration name outside the design's list is introduced.
REDIRECT_LIMIT_MULTIPLIER: Final[int] = 20


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the service configuration."""

    db_path: str
    bind_host: str
    bind_port: int
    public_base_url: str
    default_expiry_days: int
    max_expiry_days: int
    max_url_length: int
    code_length: int
    code_max_attempts: int
    rate_limit_max: int
    rate_limit_window_seconds: int
    trust_proxy_header: bool
    dns_timeout_ms: int
    dns_cache_ttl_seconds: int
    allow_private_destinations: bool
    log_level: str

    @property
    def redirect_rate_limit_max(self) -> int:
        """Return the per-IP allowance for redirect requests inside one window.

        Returns:
            The creation allowance multiplied by :data:`REDIRECT_LIMIT_MULTIPLIER`.

        Raises:
            Nothing.
        """
        return max(self.rate_limit_max, self.rate_limit_max * REDIRECT_LIMIT_MULTIPLIER)

    @property
    def uvicorn_log_level(self) -> str:
        """Return the configured log level translated to uvicorn's vocabulary.

        Returns:
            One of ``debug``, ``info``, ``warning`` or ``error``.

        Raises:
            Nothing.
        """
        return _UVICORN_LOG_LEVELS.get(self.log_level, "info")


def _raw(env: Mapping[str, str], name: str) -> str:
    """Return the raw environment value for ``name`` or its documented default.

    Returns:
        The configured string, never ``None``.

    Raises:
        KeyError: If ``name`` is not a known configuration key.
    """
    value = env.get(name)
    if value is None or value.strip() == "":
        return DEFAULTS[name]
    return value


def _read_bool(env: Mapping[str, str], name: str) -> bool:
    """Parse a boolean configuration value.

    Returns:
        ``True`` only for ``1``, ``true``, ``yes`` or ``on`` (case insensitive).

    Raises:
        KeyError: If ``name`` is not a known configuration key.
    """
    return _raw(env, name).strip().lower() in {"1", "true", "yes", "on"}


def _read_int(env: Mapping[str, str], name: str, minimum: int, maximum: int) -> int:
    """Parse an integer configuration value, clamping to the documented default on error.

    Returns:
        The parsed integer when it is within ``[minimum, maximum]``, otherwise the
        documented default for ``name``.

    Raises:
        KeyError: If ``name`` is not a known configuration key.
    """
    raw = _raw(env, name)
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        LOGGER.warning("invalid integer for %s; using default", name)
        return int(DEFAULTS[name])
    if value < minimum or value > maximum:
        LOGGER.warning("out of range value for %s; using default", name)
        return int(DEFAULTS[name])
    return value


def _read_public_base_url(env: Mapping[str, str]) -> str:
    """Read and normalise the public base URL used to build ``short_url`` values.

    Returns:
        An origin without a trailing slash. Non http(s) values fall back to the default.

    Raises:
        Nothing.
    """
    raw = _raw(env, "LINKS_PUBLIC_BASE_URL").strip()
    candidate = raw.rstrip("/")
    parts = urlsplit(candidate)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        LOGGER.warning("LINKS_PUBLIC_BASE_URL is not an http(s) origin; using default")
        return DEFAULTS["LINKS_PUBLIC_BASE_URL"].rstrip("/")
    if parts.scheme != "https":
        LOGGER.warning("LINKS_PUBLIC_BASE_URL is not https; the public API should be served over TLS")
    return candidate


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Build a :class:`Settings` snapshot from the process environment.

    Args:
        env: Mapping to read from; defaults to :data:`os.environ`.

    Returns:
        A frozen :class:`Settings` instance with safe defaults applied.

    Raises:
        Nothing; invalid values degrade to the documented defaults.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    log_level = _raw(source, "LINKS_LOG_LEVEL").strip().lower()
    if log_level not in _LOG_LEVELS:
        log_level = DEFAULTS["LINKS_LOG_LEVEL"]

    max_expiry_days = _read_int(source, "LINKS_MAX_EXPIRY_DAYS", 1, 36500)
    default_expiry_days = _read_int(source, "LINKS_DEFAULT_EXPIRY_DAYS", 1, 36500)
    if default_expiry_days > max_expiry_days:
        LOGGER.warning("LINKS_DEFAULT_EXPIRY_DAYS exceeds LINKS_MAX_EXPIRY_DAYS; clamping")
        default_expiry_days = max_expiry_days

    return Settings(
        db_path=_raw(source, "LINKS_DB_PATH").strip(),
        bind_host=_raw(source, "LINKS_BIND_HOST").strip(),
        bind_port=_read_int(source, "LINKS_BIND_PORT", 1, 65535),
        public_base_url=_read_public_base_url(source),
        default_expiry_days=default_expiry_days,
        max_expiry_days=max_expiry_days,
        max_url_length=_read_int(source, "LINKS_MAX_URL_LENGTH", 16, 65536),
        code_length=_read_int(source, "LINKS_CODE_LENGTH", 4, 32),
        code_max_attempts=_read_int(source, "LINKS_CODE_MAX_ATTEMPTS", 1, 50),
        rate_limit_max=_read_int(source, "LINKS_RATE_LIMIT_MAX", 1, 1000000),
        rate_limit_window_seconds=_read_int(source, "LINKS_RATE_LIMIT_WINDOW_SECONDS", 1, 86400),
        trust_proxy_header=_read_bool(source, "LINKS_TRUST_PROXY_HEADER"),
        dns_timeout_ms=_read_int(source, "LINKS_DNS_TIMEOUT_MS", 10, 60000),
        dns_cache_ttl_seconds=_read_int(source, "LINKS_DNS_CACHE_TTL_SECONDS", 0, 86400),
        allow_private_destinations=_read_bool(source, "LINKS_ALLOW_PRIVATE_DESTINATIONS"),
        log_level=log_level,
    )


def configure_logging(settings: Settings) -> None:
    """Configure the root logger level from the settings snapshot.

    Args:
        settings: The active configuration.

    Returns:
        None.

    Raises:
        Nothing.
    """
    level = _LOG_LEVELS.get(settings.log_level, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("links").setLevel(level)
