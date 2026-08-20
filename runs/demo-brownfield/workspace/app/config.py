"""Environment driven configuration for the link shortener service.

Every setting is read from the process environment with a safe default.  Bad
values never abort start-up: they are logged (with credential material
redacted) and the documented default is used instead.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional, Tuple

LOGGER = logging.getLogger("app.config")

DEFAULT_DB_PATH = "./links.db"
DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_CODE_LENGTH = 7
MIN_CODE_LENGTH = 4
MAX_CODE_LENGTH = 16
DEFAULT_CODE_MAX_ATTEMPTS = 5
DEFAULT_MAX_TTL_SECONDS = 31536000
DEFAULT_RATE_LIMIT_ENABLED = True
DEFAULT_RATE_LIMIT_MAX = 10
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60
DEFAULT_RATE_LIMIT_REDIRECT_MULTIPLIER = 100
DEFAULT_LOG_LEVEL = "INFO"
MAX_API_KEY_NAME_LENGTH = 256

_FALSE_VALUES = frozenset({"false", "0", "no", "off"})
_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_VALID_LOG_LEVELS = frozenset(
    {"CRITICAL", "ERROR", "WARNING", "WARN", "INFO", "DEBUG", "NOTSET"}
)


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the service configuration.

    ``api_key_entries`` holds ``(name, quota)`` pairs parsed from
    ``SHORTENER_API_KEYS``.  It is excluded from ``repr`` so that credential
    material can never reach a log record or a traceback frame summary.
    """

    db_path: str
    base_url: str
    code_length: int
    code_max_attempts: int
    max_ttl_seconds: int
    rate_limit_enabled: bool
    rate_limit_max: int
    rate_limit_window_seconds: int
    rate_limit_redirect_multiplier: int
    log_level: str
    api_key_entries: Tuple[Tuple[str, int], ...] = field(default=(), repr=False)


def _read_env(name: str, default: str) -> str:
    """Read one environment variable.

    Returns the raw string value, or ``default`` when the variable is unset.
    Raises nothing.
    """
    value = os.environ.get(name)
    if value is None:
        return default
    return value


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: Optional[int] = None,
) -> int:
    """Read an integer environment variable, failing soft.

    Returns the parsed value when it is a base-10 integer inside
    ``[minimum, maximum]``; otherwise logs a warning and returns ``default``.
    Raises nothing.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    text = raw.strip()
    try:
        value = int(text, 10)
    except ValueError:
        LOGGER.warning("%s is not an integer; using default %d", name, default)
        return default
    if value < minimum or (maximum is not None and value > maximum):
        LOGGER.warning(
            "%s value %d is out of range; using default %d", name, value, default
        )
        return default
    return value


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean environment variable, failing soft.

    Returns ``False`` for 'false', '0', 'no' or 'off' (case-insensitive),
    ``True`` for 'true', '1', 'yes' or 'on'; anything else logs a warning and
    returns ``default``.  Raises nothing.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    text = raw.strip().lower()
    if text in _FALSE_VALUES:
        return False
    if text in _TRUE_VALUES:
        return True
    LOGGER.warning("%s is not a recognised boolean; using default %s", name, default)
    return default


def parse_api_key_entries(raw: str) -> Tuple[Tuple[str, int], ...]:
    """Parse the ``SHORTENER_API_KEYS`` value into ``(name, quota)`` pairs.

    Accepts a comma separated list of ``name:quota`` entries.  Surrounding
    whitespace around the entry, the name and the quota is ignored and empty
    entries are skipped.  An entry without exactly one ':' separator, with an
    empty or over-long name, or whose quota is not a base-10 integer >= 1 is
    logged by position only (never by value) and discarded.

    Returns a tuple of valid pairs in declaration order; a later duplicate name
    wins when the tuple is later collapsed into a mapping.  Raises nothing.
    """
    entries: list[Tuple[str, int]] = []
    if not raw or not raw.strip():
        return ()
    for index, chunk in enumerate(raw.split(",")):
        item = chunk.strip()
        if not item:
            continue
        if item.count(":") != 1:
            LOGGER.warning(
                "SHORTENER_API_KEYS entry #%d skipped: expected exactly one ':' separator",
                index,
            )
            continue
        name_part, _, quota_part = item.partition(":")
        name = name_part.strip()
        quota_text = quota_part.strip()
        if not name:
            LOGGER.warning(
                "SHORTENER_API_KEYS entry #%d skipped: empty key name", index
            )
            continue
        if len(name) > MAX_API_KEY_NAME_LENGTH:
            LOGGER.warning(
                "SHORTENER_API_KEYS entry #%d skipped: key name longer than %d characters",
                index,
                MAX_API_KEY_NAME_LENGTH,
            )
            continue
        if not quota_text.isdigit():
            LOGGER.warning(
                "SHORTENER_API_KEYS entry #%d skipped: quota is not a base-10 integer",
                index,
            )
            continue
        quota = int(quota_text, 10)
        if quota < 1:
            LOGGER.warning(
                "SHORTENER_API_KEYS entry #%d skipped: quota must be >= 1", index
            )
            continue
        entries.append((name, quota))
    return tuple(entries)


def _parse_log_level(raw: str) -> str:
    """Normalise a log level name.

    Returns the upper-cased level name when recognised, otherwise logs a
    warning and returns the default level.  Raises nothing.
    """
    text = raw.strip().upper()
    if text in _VALID_LOG_LEVELS:
        return "WARNING" if text == "WARN" else text
    LOGGER.warning("LOG_LEVEL is not a recognised level; using %s", DEFAULT_LOG_LEVEL)
    return DEFAULT_LOG_LEVEL


def load_settings() -> Settings:
    """Build a :class:`Settings` snapshot from the current environment.

    Returns the populated settings object.  Invalid values are logged and
    replaced by their documented defaults, so this never raises.
    """
    base_url = _read_env("LINKS_BASE_URL", DEFAULT_BASE_URL).strip()
    if not base_url:
        base_url = DEFAULT_BASE_URL
    base_url = base_url.rstrip("/")

    return Settings(
        db_path=_read_env("LINKS_DB_PATH", DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH,
        base_url=base_url,
        code_length=_env_int(
            "LINKS_CODE_LENGTH",
            DEFAULT_CODE_LENGTH,
            minimum=MIN_CODE_LENGTH,
            maximum=MAX_CODE_LENGTH,
        ),
        code_max_attempts=_env_int(
            "LINKS_CODE_MAX_ATTEMPTS", DEFAULT_CODE_MAX_ATTEMPTS, minimum=1, maximum=100
        ),
        max_ttl_seconds=_env_int(
            "LINKS_MAX_TTL_SECONDS", DEFAULT_MAX_TTL_SECONDS, minimum=1
        ),
        rate_limit_enabled=_env_bool(
            "LINKS_RATE_LIMIT_ENABLED", DEFAULT_RATE_LIMIT_ENABLED
        ),
        rate_limit_max=_env_int(
            "LINKS_RATE_LIMIT_MAX", DEFAULT_RATE_LIMIT_MAX, minimum=1
        ),
        rate_limit_window_seconds=_env_int(
            "LINKS_RATE_LIMIT_WINDOW_SECONDS",
            DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
            minimum=1,
        ),
        rate_limit_redirect_multiplier=_env_int(
            "LINKS_RATE_LIMIT_REDIRECT_MULTIPLIER",
            DEFAULT_RATE_LIMIT_REDIRECT_MULTIPLIER,
            minimum=1,
        ),
        log_level=_parse_log_level(_read_env("LOG_LEVEL", DEFAULT_LOG_LEVEL)),
        api_key_entries=parse_api_key_entries(_read_env("SHORTENER_API_KEYS", "")),
    )


def configure_logging(level_name: str) -> None:
    """Configure the root logger level and a stream handler if none exists.

    Returns ``None``.  Existing handlers (for example a test capture handler)
    are left untouched so log capture keeps working.  Raises nothing.
    """
    level = getattr(logging, level_name.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        root.addHandler(handler)
