"""Short-code generation and shape validation."""

from __future__ import annotations

import re
import secrets
import string

ALPHABET = string.digits + string.ascii_uppercase + string.ascii_lowercase
CODE_PATTERN = re.compile(r"\A[0-9A-Za-z]{1,64}\Z")


def generate_code(length: int) -> str:
    """Generate a random base62 short code.

    Args:
        length: Number of characters to generate; must be >= 1.

    Returns:
        A cryptographically random base62 string of ``length`` characters.

    Raises:
        ValueError: If ``length`` is smaller than 1.
    """
    if length < 1:
        raise ValueError("code length must be at least 1")
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def is_valid_code(code: str) -> bool:
    """Report whether a path segment has the shape of a short code.

    Args:
        code: The candidate code taken from the request path.

    Returns:
        ``True`` when the value is 1-64 base62 characters, else ``False``.

    Raises:
        Nothing.
    """
    return bool(CODE_PATTERN.match(code))
