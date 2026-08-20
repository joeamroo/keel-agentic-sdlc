"""Timezone aware timestamp helpers shared by the service.

All timestamps handled by the service are aware UTC datetimes; the stored string
form is fixed width so lexicographic comparison equals chronological comparison.
"""
from __future__ import annotations

from datetime import datetime, timezone

ISO_Z_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> datetime:
    """Return the current instant as an aware UTC datetime truncated to whole seconds.

    Returns:
        An aware :class:`datetime` in UTC with ``microsecond`` set to 0 so that the
        rendered ISO-8601 form is fixed width.

    Raises:
        Nothing.
    """
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso_z(moment: datetime) -> str:
    """Render an aware datetime as fixed width ISO-8601 UTC with a ``Z`` suffix.

    Args:
        moment: An aware datetime in any timezone.

    Returns:
        A string such as ``2024-01-31T10:20:30Z``.

    Raises:
        ValueError: If ``moment`` is naive (no timezone information).
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("naive datetimes are not supported")
    return moment.astimezone(timezone.utc).strftime(ISO_Z_FORMAT)
