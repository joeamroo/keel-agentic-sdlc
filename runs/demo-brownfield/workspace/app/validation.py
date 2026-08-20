"""Validation of user-supplied target URLs.

Only ``http`` and ``https`` URLs are accepted (allow-list, not a block-list).
Embedded credentials are rejected, the host is resolved and every resolved
address must be a globally routable unicast address: loopback, private, link
local, multicast, reserved and unspecified addresses are refused, with
169.254.169.254 (the cloud metadata endpoint) denied explicitly.

The value returned by :func:`validate_target_url` is the value the caller must
store, so nothing that skipped validation can ever be served on a redirect.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections import OrderedDict
from typing import Dict, Tuple, Union
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_HOST_LENGTH = 253
DNS_CACHE_TTL_SECONDS = 60.0
DNS_CACHE_MAX_ENTRIES = 512

#: Cloud metadata endpoints denied by explicit literal as well as by range.
METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

_dns_cache: "OrderedDict[str, Tuple[float, Tuple[str, ...]]]" = OrderedDict()
_dns_lock = threading.Lock()


class UrlValidationError(ValueError):
    """Raised when a submitted target URL is not acceptable for storage."""


def _has_forbidden_characters(value: str) -> bool:
    """Report whether a URL string contains whitespace or control characters.

    Args:
        value: The candidate URL.

    Returns:
        ``True`` when the value contains any whitespace, C0 control character or
        DEL, which would allow header or request splitting downstream.

    Raises:
        Nothing.
    """
    for char in value:
        if char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F:
            return True
    return False


def _normalise_host(host: str) -> str:
    """Normalise a URL host to a lowercase ASCII form.

    Args:
        host: The host component taken from the parsed URL.

    Returns:
        The lowercase host, IDNA-encoded when it contains non-ASCII characters.

    Raises:
        UrlValidationError: If the host is empty or is not encodable as a
            domain name.
    """
    candidate = host.strip().rstrip(".").lower()
    if not candidate:
        raise UrlValidationError("The target URL must include a host.")
    if candidate.isascii():
        return candidate
    try:
        return candidate.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError) as exc:  # pragma: no cover - rare
        raise UrlValidationError(
            "The target URL host is not a valid domain name."
        ) from exc


def resolve_host(host: str) -> Tuple[str, ...]:
    """Resolve a host to the set of IP addresses it points at.

    Literal IP addresses are returned unchanged.  Successful DNS results are
    cached in-process for ``DNS_CACHE_TTL_SECONDS`` so a burst of creations does
    not hammer the resolver.

    Args:
        host: An ASCII host name or IP literal.

    Returns:
        A tuple of IP address strings (at least one entry).

    Raises:
        UrlValidationError: If the host cannot be resolved.
    """
    try:
        ipaddress.ip_address(host)
        return (host,)
    except ValueError:
        pass

    now = time.monotonic()
    with _dns_lock:
        cached = _dns_cache.get(host)
        if cached is not None and cached[0] > now:
            _dns_cache.move_to_end(host)
            return cached[1]

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise UrlValidationError(
            "The target URL host could not be resolved."
        ) from exc

    addresses = tuple(sorted({str(info[4][0]).split("%")[0] for info in infos}))
    if not addresses:
        raise UrlValidationError("The target URL host could not be resolved.")

    with _dns_lock:
        _dns_cache[host] = (now + DNS_CACHE_TTL_SECONDS, addresses)
        _dns_cache.move_to_end(host)
        while len(_dns_cache) > DNS_CACHE_MAX_ENTRIES:
            _dns_cache.popitem(last=False)
    return addresses


def clear_dns_cache() -> None:
    """Empty the in-process DNS result cache.

    Returns:
        None.

    Raises:
        Nothing.
    """
    with _dns_lock:
        _dns_cache.clear()


def _assert_address_allowed(address: IPAddress) -> None:
    """Reject any address that is not a globally routable unicast address.

    Args:
        address: The resolved IP address to inspect.

    Returns:
        None.

    Raises:
        UrlValidationError: If the address is the cloud metadata endpoint, or is
            loopback, private, link local, multicast, reserved, unspecified or
            otherwise not globally routable.
    """
    if address in METADATA_ADDRESSES:
        raise UrlValidationError(
            "The target URL resolves to a blocked internal address."
        )
    effective: IPAddress = address
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            effective = mapped
        elif address.sixtofour is not None:
            effective = address.sixtofour
    if effective in METADATA_ADDRESSES:
        raise UrlValidationError(
            "The target URL resolves to a blocked internal address."
        )
    if (
        effective.is_loopback
        or effective.is_private
        or effective.is_link_local
        or effective.is_multicast
        or effective.is_reserved
        or effective.is_unspecified
        or not effective.is_global
    ):
        raise UrlValidationError(
            "The target URL resolves to a blocked internal address."
        )


def validate_target_url(raw_url: str, max_length: int) -> str:
    """Validate and normalise a user supplied target URL for storage.

    Args:
        raw_url: The URL exactly as submitted by the client.
        max_length: Maximum accepted length of the URL.

    Returns:
        The normalised URL string that must be persisted and later served.

    Raises:
        UrlValidationError: If the URL is empty, too long, contains whitespace
            or control characters, uses a scheme other than http/https, embeds
            credentials, has no or an invalid host/port, cannot be resolved, or
            resolves to a non-public address.
    """
    candidate = raw_url.strip()
    if not candidate:
        raise UrlValidationError("The target URL must not be empty.")
    if len(candidate) > max_length:
        raise UrlValidationError(
            "The target URL is longer than the configured maximum."
        )
    if _has_forbidden_characters(candidate):
        raise UrlValidationError(
            "The target URL must not contain whitespace or control characters."
        )

    try:
        split = urlsplit(candidate)
    except ValueError as exc:
        raise UrlValidationError("The target URL could not be parsed.") from exc

    scheme = split.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlValidationError("Only http and https target URLs are allowed.")
    if not split.netloc:
        raise UrlValidationError("The target URL must include a host.")
    if "@" in split.netloc:
        raise UrlValidationError(
            "The target URL must not contain embedded credentials."
        )

    try:
        host = split.hostname
        port = split.port
    except ValueError as exc:
        raise UrlValidationError("The target URL has an invalid port.") from exc

    if not host:
        raise UrlValidationError("The target URL must include a host.")
    if len(host) > MAX_HOST_LENGTH:
        raise UrlValidationError("The target URL host is too long.")
    if port is not None and not 1 <= port <= 65535:
        raise UrlValidationError("The target URL has an invalid port.")

    ascii_host = _normalise_host(host)
    for address_text in resolve_host(ascii_host):
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError as exc:  # pragma: no cover - resolver contract
            raise UrlValidationError(
                "The target URL host could not be resolved."
            ) from exc
        _assert_address_allowed(address)

    netloc = "[" + ascii_host + "]" if ":" in ascii_host else ascii_host
    if port is not None:
        netloc = netloc + ":" + str(port)
    path = split.path or "/"
    normalised = urlunsplit((scheme, netloc, path, split.query, split.fragment))
    if len(normalised) > max_length:
        raise UrlValidationError(
            "The target URL is longer than the configured maximum."
        )
    return normalised


def dns_cache_size() -> int:
    """Return the number of cached DNS entries.

    Returns:
        The current number of hosts held in the in-process DNS cache.

    Raises:
        Nothing.
    """
    with _dns_lock:
        return len(_dns_cache)


_UNUSED: Dict[str, str] = {}
