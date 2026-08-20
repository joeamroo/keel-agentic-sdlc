"""Timezone aware timestamp helpers.

All timestamps handled by the service are UTC and timezone aware; naive values
are never produced and never accepted as authoritative.
"""

from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    """Return the current time.

    Returns a timezone aware :class:`datetime` in UTC.  Raises nothing.
    """
    return datetime.now(timezone.utc)


def to_rfc3339(value: datetime) -> str:
    """Render a datetime as an RFC3339 UTC string.

    Naive input is interpreted as UTC.  Returns a string ending in 'Z'.
    Raises nothing.
    """
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_rfc3339(value: str) -> datetime:
    """Parse an RFC3339 UTC timestamp produced by :func:`to_rfc3339`.

    Returns a timezone aware UTC :class:`datetime`.
    Raises :class:`ValueError` when the text is not a parsable timestamp.
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
