"""Timezone-aware timestamp helpers.

Every timestamp handled by the service is timezone aware and serialised as
RFC3339 UTC with a trailing ``Z``; naive datetimes are rejected at the boundary
of these helpers so expiry logic cannot silently drift across deployments.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Returns:
        A ``datetime`` whose ``tzinfo`` is ``timezone.utc``.

    Raises:
        Nothing.
    """
    return datetime.now(timezone.utc)


def to_rfc3339(value: datetime) -> str:
    """Serialise a timezone-aware datetime as RFC3339 UTC.

    Args:
        value: A timezone-aware datetime.

    Returns:
        A string such as ``2024-01-01T00:00:00.000Z``.

    Raises:
        ValueError: If ``value`` is naive (has no tzinfo/utcoffset).
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a timezone-aware datetime is required")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def parse_rfc3339(value: str) -> datetime:
    """Parse an RFC3339 UTC timestamp produced by :func:`to_rfc3339`.

    Args:
        value: The stored timestamp string.

    Returns:
        A timezone-aware datetime normalised to UTC.

    Raises:
        ValueError: If the string cannot be parsed as a timestamp.
    """
    text = value.strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
