"""Per-key POST quotas parsed from SHORTENER_API_KEYS.

Key material is a secret. It is hashed with a per-boot salt on the way in and the
plaintext is never retained as a dict key, attribute, log field or exception
argument, never written to SQLite and never placed in a response body.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import Dict, Mapping, Optional

LOGGER = logging.getLogger("shortener.apikeys")

#: 32 random bytes, generated once per process, never logged or serialised.
BOOT_SALT: bytes = secrets.token_bytes(32)

#: Longest header value considered as a possible key; anything longer is unknown.
MAX_KEY_LENGTH: int = 512


def salted_digest(material: bytes) -> bytes:
    """Hash arbitrary identity material with the per-boot salt.

    Args:
        material: Raw bytes (an API key or a client address) to hash.

    Returns:
        A 32-byte digest that is stable within this process only.

    Raises:
        Nothing.
    """
    return hashlib.blake2b(material, key=BOOT_SALT, digest_size=32).digest()


@dataclass(frozen=True)
class ApiKeyEntry:
    """A recognised API key, represented only by its salted digest and quota.

    Attributes:
        digest: Salted hash of the configured key.
        quota: Allowed POST /api/links requests per window; always >= 1.
    """

    digest: bytes
    quota: int


def parse_api_key_quotas(raw: Optional[str]) -> Dict[bytes, ApiKeyEntry]:
    """Parse the SHORTENER_API_KEYS value into a digest-keyed quota table.

    The value is a comma-separated list of ``key:quota`` pairs. Whitespace around
    keys, quotas and separators is stripped. Entries without a colon, with a blank
    key, or with a non-integer / zero / negative quota are dropped. When a key
    appears more than once the last occurrence wins.

    Args:
        raw: The raw environment value, possibly ``None`` or empty.

    Returns:
        A mapping of salted digest to :class:`ApiKeyEntry`. Empty when nothing is
        configured, in which case no key is ever recognised.

    Raises:
        Nothing: malformed entries are ignored so the service always starts.
    """
    table: Dict[bytes, ApiKeyEntry] = {}
    dropped = 0
    for segment in (raw or "").split(","):
        item = segment.strip()
        if not item:
            continue
        key_part, separator, quota_part = item.partition(":")
        if not separator:
            dropped += 1
            continue
        key = key_part.strip()
        quota_text = quota_part.strip()
        if not key or not quota_text or len(key) > MAX_KEY_LENGTH:
            dropped += 1
            continue
        try:
            quota = int(quota_text, 10)
        except ValueError:
            dropped += 1
            continue
        if quota < 1:
            dropped += 1
            continue
        digest = salted_digest(key.encode("utf-8"))
        table[digest] = ApiKeyEntry(digest=digest, quota=quota)
    if dropped:
        LOGGER.warning("Ignored %d malformed SHORTENER_API_KEYS entries.", dropped)
    LOGGER.info("Loaded %d API key quota entries.", len(table))
    return table


def lookup_api_key(
    table: Mapping[bytes, ApiKeyEntry], presented: Optional[str]
) -> Optional[ApiKeyEntry]:
    """Look up the quota for a presented API key.

    Recognition is one constant-work hash plus a hash-table lookup, confirmed with
    :func:`hmac.compare_digest`, so there is no early-exit comparison loop over the
    configured key set to time. Matching is byte-exact and case-sensitive; a header
    that is absent, empty or whitespace-only counts as no key at all.

    Args:
        table: The digest-keyed quota table built at startup.
        presented: The raw ``X-API-Key`` header value, if any.

    Returns:
        The matching :class:`ApiKeyEntry`, or ``None`` when the caller presented no
        key or an unrecognised one.

    Raises:
        Nothing.
    """
    if not table or presented is None:
        return None
    candidate = presented.strip()
    if not candidate or len(candidate) > MAX_KEY_LENGTH:
        return None
    digest = salted_digest(candidate.encode("utf-8"))
    entry = table.get(digest)
    if entry is None:
        return None
    if not hmac.compare_digest(digest, entry.digest):
        return None
    return entry
