"""Validation of user supplied target URLs.

The validator is an allow-list: only ``http`` and ``https`` survive, embedded
credentials are refused, and the host is resolved so that loopback, private,
link-local, multicast, reserved and unspecified destinations - including the
cloud metadata address 169.254.169.254 - can never be stored and therefore can
never be served by the redirect route.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from typing import Dict, List, Tuple, Union
from urllib.parse import urlsplit

LOGGER = logging.getLogger("app.urls")

MAX_URL_LENGTH = 2048
MAX_HOSTNAME_LENGTH = 253
ALLOWED_SCHEMES = frozenset({"http", "https"})

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)

_EXPLICIT_DENY_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)

_RESOLUTION_CACHE_TTL_SECONDS = 30.0
_RESOLUTION_CACHE_MAX_ENTRIES = 1024
_cache_lock = threading.Lock()
_resolution_cache: Dict[str, Tuple[float, bool]] = {}


class UrlValidationError(ValueError):
    """Raised when a submitted URL is not acceptable as a redirect target."""


def _normalise_address(address: IPAddress) -> IPAddress:
    """Unwrap IPv4-mapped IPv6 addresses.

    Returns the embedded IPv4 address when present, otherwise the input.
    Raises nothing.
    """
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return mapped
    return address


def is_disallowed_address(address: IPAddress) -> bool:
    """Report whether an IP address must never be fetched or redirected to.

    Returns ``True`` for loopback, private, link-local, multicast, reserved,
    unspecified, IPv6 site-local and explicitly denied metadata addresses.
    Raises nothing.
    """
    candidate = _normalise_address(address)
    if candidate in _EXPLICIT_DENY_ADDRESSES or address in _EXPLICIT_DENY_ADDRESSES:
        return True
    if getattr(candidate, "is_site_local", False):
        return True
    return bool(
        candidate.is_loopback
        or candidate.is_private
        or candidate.is_link_local
        or candidate.is_multicast
        or candidate.is_reserved
        or candidate.is_unspecified
    )


def _resolve_host_addresses(host: str) -> List[IPAddress]:
    """Resolve a hostname to every address it maps to.

    Returns a non-empty list of IP addresses.
    Raises :class:`UrlValidationError` when the host cannot be resolved.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise UrlValidationError("The URL host could not be resolved.") from exc
    addresses: List[IPAddress] = []
    for info in infos:
        sockaddr = info[4]
        raw = str(sockaddr[0]).split("%", 1)[0]
        try:
            addresses.append(ipaddress.ip_address(raw))
        except ValueError:
            continue
    if not addresses:
        raise UrlValidationError("The URL host could not be resolved.")
    return addresses


def _cached_host_verdict(host: str) -> bool:
    """Return whether every resolved address for ``host`` is publicly routable.

    Results are cached for a short TTL so a burst of creations does not repeat
    the same DNS lookup.  Returns ``True`` when the host is acceptable.
    Raises :class:`UrlValidationError` when the host cannot be resolved.
    """
    now = time.monotonic()
    with _cache_lock:
        cached = _resolution_cache.get(host)
        if cached is not None and cached[0] > now:
            return cached[1]
    addresses = _resolve_host_addresses(host)
    safe = not any(is_disallowed_address(address) for address in addresses)
    with _cache_lock:
        if len(_resolution_cache) >= _RESOLUTION_CACHE_MAX_ENTRIES:
            _resolution_cache.clear()
        _resolution_cache[host] = (now + _RESOLUTION_CACHE_TTL_SECONDS, safe)
    return safe


def validate_target_url(raw_url: str, *, max_length: int = MAX_URL_LENGTH) -> str:
    """Validate a user supplied redirect target.

    Returns the exact string that must be stored and later served (the input
    with surrounding whitespace removed).
    Raises :class:`UrlValidationError` with a safe, caller-facing message when
    the URL is empty, too long, contains control characters, uses a scheme
    other than http/https, embeds credentials, has no host, has an invalid
    port, or resolves to a non public address.
    """
    if not isinstance(raw_url, str):
        raise UrlValidationError("The url field must be a string.")
    candidate = raw_url.strip()
    if not candidate:
        raise UrlValidationError("The url field must not be empty.")
    if len(candidate) > max_length:
        raise UrlValidationError(
            "The url field must be at most %d characters." % max_length
        )
    for character in candidate:
        if ord(character) < 0x20 or ord(character) == 0x7F or character.isspace():
            raise UrlValidationError("The url must not contain whitespace or control characters.")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise UrlValidationError("The url could not be parsed.") from exc

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlValidationError("Only http and https URLs are accepted.")
    if not parts.netloc:
        raise UrlValidationError("The url must include a host.")
    if "@" in parts.netloc:
        raise UrlValidationError("The url must not contain embedded credentials.")

    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise UrlValidationError("The url contains an invalid port.") from exc
    if not host:
        raise UrlValidationError("The url must include a host.")
    if port is not None and not (1 <= port <= 65535):
        raise UrlValidationError("The url contains an invalid port.")

    hostname = host.lower().rstrip(".")
    if not hostname or len(hostname) > MAX_HOSTNAME_LENGTH:
        raise UrlValidationError("The url host is not acceptable.")
    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith(".localhost"):
        raise UrlValidationError("The url host is not allowed.")

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        if is_disallowed_address(literal):
            raise UrlValidationError("The url host is not allowed.")
        return candidate

    if not _cached_host_verdict(hostname):
        raise UrlValidationError("The url host is not allowed.")
    return candidate
