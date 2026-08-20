"""Small shared helpers: short code generation, time formatting, truncation."""

from __future__ import annotations

import re
import secrets
import string
from datetime import datetime, timezone
from typing import Optional

BASE62_ALPHABET = string.digits + string.ascii_uppercase + string.ascii_lowercase

# Codes accepted on the read paths. Generated codes are a strict subset.
CODE_PATTERN = re.compile(r"^[0-9A-Za-z]{1,64}$")


def generate_code(length: int) -> str:
    """Generate a cryptographically random base62 short code.

    Returns a string of exactly ``length`` characters. Raises ValueError when
    ``length`` is not positive.
    """
    if length <= 0:
        raise ValueError("code length must be positive")
    return "".join(secrets.choice(BASE62_ALPHABET) for _ in range(length))


def utc_now() -> datetime:
    """Return the current time as a timezone aware UTC datetime.

    Raises nothing.
    """
    return datetime.now(timezone.utc)


def to_rfc3339(value: datetime) -> str:
    """Format a datetime as a fixed width RFC3339 UTC string.

    Naive datetimes are treated as UTC. The fixed width (microsecond precision,
    trailing ``Z``) makes lexicographic comparison equivalent to chronological
    comparison, which is what the expiry check relies on. Returns the formatted
    string. Raises nothing.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def truncate(value: Optional[str], limit: int) -> Optional[str]:
    """Truncate an optional string to ``limit`` characters.

    Returns None when ``value`` is None, otherwise the (possibly shortened)
    string. Raises nothing.
    """
    if value is None:
        return None
    return value[:limit]
