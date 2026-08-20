"""Timezone-aware timestamp helpers.

Every timestamp handled by the service is timezone aware and stored as RFC 3339
UTC text, so expiry comparisons cannot go wrong across a deployment boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def utc_now() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime.

    Returns:
        The current time with ``tzinfo`` set to UTC.

    Raises:
        Nothing.
    """
    return datetime.now(timezone.utc)


def to_rfc3339(moment: datetime) -> str:
    """Render a timezone-aware datetime as RFC 3339 UTC text.

    Args:
        moment: A timezone-aware datetime.

    Returns:
        The instant in UTC, e.g. ``2024-01-01T00:00:00.000000Z``.

    Raises:
        ValueError: If ``moment`` is naive (no tzinfo).
    """
    if moment.tzinfo is None:
        raise ValueError("Refusing to serialise a naive datetime.")
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_rfc3339(value: str) -> datetime:
    """Parse RFC 3339 text into a timezone-aware UTC datetime.

    Args:
        value: Timestamp text, with or without a trailing ``Z``.

    Returns:
        The parsed instant, normalised to UTC.

    Raises:
        ValueError: If the text is not a parsable timestamp.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_expired(expires_at: Optional[str], now: datetime) -> bool:
    """Decide whether a stored expiry timestamp has passed.

    Args:
        expires_at: Stored RFC 3339 text, or ``None`` for a link that never expires.
        now: Timezone-aware reference instant.

    Returns:
        ``True`` when the link is expired (or its stored timestamp is unreadable,
        which is treated as expired so a corrupt row fails closed), ``False``
        otherwise.

    Raises:
        ValueError: If ``now`` is naive.
    """
    if now.tzinfo is None:
        raise ValueError("Refusing to compare against a naive datetime.")
    if expires_at is None:
        return False
    try:
        deadline = parse_rfc3339(expires_at)
    except ValueError:
        return True
    return deadline <= now
