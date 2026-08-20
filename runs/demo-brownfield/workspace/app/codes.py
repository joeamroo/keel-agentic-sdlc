"""Short code generation.

Codes are drawn from a cryptographically secure random source; they are never
derived from a database row id or any other sequence.
"""

from __future__ import annotations

import secrets
import string

ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits


def generate_code(length: int) -> str:
    """Generate a base62 short code.

    ``length`` is the number of characters to produce. Returns a string of
    exactly ``length`` characters drawn uniformly from ``[A-Za-z0-9]`` using
    :mod:`secrets`. Raises :class:`ValueError` when ``length`` is not positive.
    """
    if length <= 0:
        raise ValueError("code length must be positive")
    return "".join(secrets.choice(ALPHABET) for _ in range(length))
