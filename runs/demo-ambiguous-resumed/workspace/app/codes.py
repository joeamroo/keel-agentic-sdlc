"""Cryptographically secure short code generation."""
from __future__ import annotations

import secrets
import string
from typing import Final, FrozenSet

CODE_ALPHABET: Final[str] = string.digits + string.ascii_uppercase + string.ascii_lowercase
CODE_ALPHABET_SET: Final[FrozenSet[str]] = frozenset(CODE_ALPHABET)
DEFAULT_CODE_LENGTH: Final[int] = 7
MAX_CODE_LENGTH: Final[int] = 64


def generate_code(length: int = DEFAULT_CODE_LENGTH) -> str:
    """Generate a base62 short code from a cryptographically secure random source.

    Args:
        length: Number of base62 characters to emit.

    Returns:
        A random, non sequential, non enumerable code of exactly ``length`` characters.

    Raises:
        ValueError: If ``length`` is outside ``[1, MAX_CODE_LENGTH]``.
    """
    if length < 1 or length > MAX_CODE_LENGTH:
        raise ValueError("code length out of range")
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def is_plausible_code(code: str, max_length: int = MAX_CODE_LENGTH) -> bool:
    """Report whether a path segment could be a short code at all.

    Args:
        code: The candidate path segment supplied by the caller.
        max_length: Longest accepted candidate length.

    Returns:
        ``True`` when the candidate is non empty, within the length bound and made
        solely of base62 characters; ``False`` otherwise, in which case the caller
        must answer 404 without querying the datastore.

    Raises:
        Nothing.
    """
    if not code or len(code) > max_length:
        return False
    return all(character in CODE_ALPHABET_SET for character in code)
