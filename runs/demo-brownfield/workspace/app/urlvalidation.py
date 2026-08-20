"""Validation of user supplied target URLs.

The validated string is exactly what is stored, and the stored string is exactly
what is later served in a ``Location`` header, so nothing that skipped validation
can ever be reached through the service.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import FrozenSet, List, Set
from urllib.parse import urlsplit

MAX_URL_LENGTH: int = 2048
ALLOWED_SCHEMES: FrozenSet[str] = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}

# Cloud metadata endpoints: reaching these is how a URL handler leaks credentials.
METADATA_ADDRESSES: FrozenSet[str] = frozenset({"169.254.169.254", "fd00:ec2::254"})


class UrlValidationError(ValueError):
    """Raised when a target URL is not safe to store or serve."""


def _has_forbidden_characters(raw: str) -> bool:
    """Report whether a URL contains control characters or whitespace.

    Args:
        raw: Candidate URL text.

    Returns:
        ``True`` when the text contains whitespace or C0/C1 control characters.

    Raises:
        Nothing.
    """
    for char in raw:
        code_point = ord(char)
        if char.isspace() or code_point < 0x20 or code_point == 0x7F:
            return True
    return False


def is_blocked_address(address: str) -> bool:
    """Decide whether a resolved IP address must not be reachable via a short link.

    Args:
        address: Textual IPv4 or IPv6 address.

    Returns:
        ``True`` when the address is loopback, private, link-local, multicast,
        reserved, unspecified, a known metadata endpoint, or unparsable.

    Raises:
        Nothing.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    if str(parsed) in METADATA_ADDRESSES:
        return True
    if parsed.is_loopback or parsed.is_private or parsed.is_link_local:
        return True
    if parsed.is_multicast or parsed.is_reserved or parsed.is_unspecified:
        return True
    if getattr(parsed, "is_site_local", False):
        return True
    return False


def _resolve_host(host: str, port: int) -> List[str]:
    """Resolve a hostname (or IP literal) to every address it maps to.

    Args:
        host: Hostname or IP literal taken from the URL.
        port: Port used for the lookup.

    Returns:
        A list of textual addresses, never empty.

    Raises:
        UrlValidationError: If the host cannot be resolved.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise UrlValidationError("Target host could not be resolved.") from exc
    addresses: Set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if sockaddr and isinstance(sockaddr[0], str):
            addresses.add(sockaddr[0])
    if not addresses:
        raise UrlValidationError("Target host could not be resolved.")
    return sorted(addresses)


def validate_target_url(raw: str) -> str:
    """Validate a user supplied target URL and return the value to store.

    The URL must use http or https (allow-list, so ``javascript:``, ``data:``,
    ``file:`` and every other scheme are refused), must not embed credentials, and
    must resolve exclusively to publicly routable addresses.

    Args:
        raw: The candidate URL exactly as supplied by the client.

    Returns:
        The URL string to persist, byte-identical to the accepted input.

    Raises:
        UrlValidationError: If the URL is malformed, too long, uses a disallowed
            scheme, embeds credentials, or resolves to a blocked address.
    """
    if not raw:
        raise UrlValidationError("URL must not be empty.")
    if len(raw) > MAX_URL_LENGTH:
        raise UrlValidationError("URL exceeds the maximum length of 2048 characters.")
    if _has_forbidden_characters(raw):
        raise UrlValidationError("URL must not contain whitespace or control characters.")

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise UrlValidationError("URL could not be parsed.") from exc

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlValidationError("URL scheme must be http or https.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UrlValidationError("URL must not contain embedded credentials.")

    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise UrlValidationError("URL contains an invalid port.") from exc
    if not hostname:
        raise UrlValidationError("URL must contain a host.")
    if port is not None and not (1 <= port <= 65535):
        raise UrlValidationError("URL contains an invalid port.")

    lookup_port = port if port is not None else DEFAULT_PORTS[scheme]
    for address in _resolve_host(hostname, lookup_port):
        if is_blocked_address(address):
            raise UrlValidationError("URL resolves to a network address that is not allowed.")
    return raw
