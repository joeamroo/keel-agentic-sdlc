"""Destination URL validation.

Only ``http`` and ``https`` URLs are accepted (allow-list, not a block-list of
bad schemes). Hosts are resolved and every resulting address is checked against
the denied ranges: loopback, private, link-local (including the cloud metadata
address 169.254.169.254), unique-local, CGNAT, multicast, unspecified and
other reserved space. No HTTP request is ever made to a user supplied URL.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import List, Optional, Tuple, Union
from urllib.parse import urlsplit

from .errors import ApiError

logger = logging.getLogger("links.validation")

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_HOST_LENGTH = 253

BLOCKED_MESSAGE = (
    "The destination host is not a permitted public address."
)

#: Explicitly denied singleton addresses. 169.254.169.254 is the cloud
#: metadata endpoint; reaching it turns a URL fetcher into a credential leak.
CLOUD_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)

BLOCKED_NETWORKS: Tuple[IPNetwork, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    ipaddress.ip_network("100::/64"),
    ipaddress.ip_network("2001:db8::/32"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)


def _parse_int_token(token: str) -> Optional[int]:
    """Parse one dotted-quad component allowing decimal, octal and hex forms.

    ``token`` is a single component such as ``0x7f``, ``0177`` or ``127``.
    Returns the integer value, or ``None`` when the token is not a valid
    numeric component. Raises nothing.
    """
    if not token:
        return None
    lowered = token.lower()
    try:
        if lowered.startswith("0x"):
            digits = lowered[2:]
            if not digits or any(c not in "0123456789abcdef" for c in digits):
                return None
            value = int(digits, 16)
        elif len(lowered) > 1 and lowered.startswith("0"):
            digits = lowered[1:]
            if any(c not in "01234567" for c in digits):
                return None
            value = int(digits, 8)
        else:
            if not lowered.isdigit():
                return None
            value = int(lowered, 10)
    except ValueError:
        return None
    if value < 0 or value > 0xFFFFFFFF:
        return None
    return value


def parse_relaxed_ipv4(host: str) -> Optional[ipaddress.IPv4Address]:
    """Decode non canonical IPv4 literals (decimal, octal, hex, short forms).

    ``host`` is the host component of a URL. Returns the decoded
    :class:`ipaddress.IPv4Address` when the host is any ``inet_aton`` style
    literal such as ``2130706433``, ``0x7f.0.0.1`` or ``127.1``; returns
    ``None`` when the host is not such a literal. Raises nothing.
    """
    candidate = host.rstrip(".")
    if not candidate:
        return None
    parts = candidate.split(".")
    if len(parts) > 4:
        return None
    values: List[int] = []
    for part in parts:
        parsed = _parse_int_token(part)
        if parsed is None:
            return None
        values.append(parsed)
    for value in values[:-1]:
        if value > 0xFF:
            return None
    remaining_bytes = 4 - (len(values) - 1)
    last = values[-1]
    if last > (256 ** remaining_bytes) - 1:
        return None
    total = 0
    for index, value in enumerate(values[:-1]):
        total |= value << (8 * (3 - index))
    total |= last
    try:
        return ipaddress.IPv4Address(total)
    except (ipaddress.AddressValueError, ValueError):
        return None


def parse_host_as_ip(host: str) -> Optional[IPAddress]:
    """Interpret a URL host as an IP literal if possible.

    ``host`` is the (already bracket stripped) host component. Returns the
    parsed address for canonical IPv4/IPv6 literals and for relaxed IPv4
    literals, or ``None`` when the host is a name. Raises nothing.
    """
    cleaned = host.strip().strip("[]")
    if not cleaned:
        return None
    if "%" in cleaned:
        cleaned = cleaned.split("%", 1)[0]
    try:
        return ipaddress.ip_address(cleaned)
    except ValueError:
        pass
    return parse_relaxed_ipv4(cleaned)


def _embedded_addresses(ip: IPAddress) -> List[IPAddress]:
    """Extract IPv4 addresses embedded inside an IPv6 address.

    ``ip`` is any parsed address. Returns a list of embedded IPv4 addresses for
    IPv4-mapped, IPv4-compatible, 6to4 and Teredo forms; an empty list when
    nothing is embedded. Raises nothing.
    """
    if not isinstance(ip, ipaddress.IPv6Address):
        return []
    embedded: List[IPAddress] = []
    if ip.ipv4_mapped is not None:
        embedded.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        embedded.append(ip.sixtofour)
    teredo = ip.teredo
    if teredo is not None:
        embedded.extend([teredo[0], teredo[1]])
    packed = ip.packed
    if packed[:12] == b"\x00" * 12 and int(ip) > 1:
        embedded.append(ipaddress.IPv4Address(packed[12:]))
    return embedded


def _single_address_blocked(ip: IPAddress) -> bool:
    """Test one address (without unwrapping) against the denied ranges.

    ``ip`` is a parsed address. Returns ``True`` when the address is denied.
    Raises nothing.
    """
    if ip in CLOUD_METADATA_ADDRESSES:
        return True
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    ):
        return True
    if not ip.is_global:
        return True
    for network in BLOCKED_NETWORKS:
        if ip.version == network.version and ip in network:
            return True
    return False


def is_blocked_address(ip: IPAddress) -> bool:
    """Test an address, including any embedded IPv4 form, against the denylist.

    ``ip`` is a parsed address. Returns ``True`` when the address itself or any
    address embedded within it is loopback, private, link-local, unique-local,
    CGNAT, multicast, unspecified or otherwise reserved. Raises nothing.
    """
    if _single_address_blocked(ip):
        return True
    for embedded in _embedded_addresses(ip):
        if _single_address_blocked(embedded):
            return True
    return False


def resolve_host(host: str) -> List[str]:
    """Resolve a hostname to textual addresses using the stdlib resolver.

    ``host`` is a DNS name. Returns the list of address strings returned by
    :func:`socket.getaddrinfo` for ``AF_UNSPEC``; returns an empty list when
    resolution fails so callers can fail closed. Raises nothing.
    """
    try:
        infos = socket.getaddrinfo(
            host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
    except (socket.gaierror, UnicodeError, OSError, ValueError):
        return []
    addresses: List[str] = []
    for info in infos:
        address: Optional[str] = None
        if isinstance(info, str):
            address = info
        else:
            try:
                sockaddr = info[4]
            except (IndexError, TypeError, KeyError):
                sockaddr = None
            if isinstance(sockaddr, (tuple, list)) and sockaddr:
                candidate = sockaddr[0]
                if isinstance(candidate, bytes):
                    candidate = candidate.decode("ascii", "ignore")
                if isinstance(candidate, str):
                    address = candidate
            elif isinstance(sockaddr, str):
                address = sockaddr
        if not address:
            continue
        addresses.append(address.split("%", 1)[0])
    return addresses


def _has_forbidden_characters(url: str) -> bool:
    """Detect control characters or whitespace inside a URL.

    ``url`` is the raw client string. Returns ``True`` when the URL contains
    whitespace, C0 controls or DEL, which would allow header injection when the
    value is later emitted in a ``Location`` header. Raises nothing.
    """
    for char in url:
        if char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F:
            return True
    return False


def validate_destination_url(url: str, *, dns_enabled: bool) -> str:
    """Validate a caller supplied destination URL before it is stored.

    ``url`` is the raw string from the request body and ``dns_enabled`` selects
    whether hostnames are resolved and every resulting address denylist checked.
    Returns the URL unchanged (the exact value that will be stored and later
    served). Raises :class:`app.errors.ApiError` with code ``unsupported_scheme``
    for any scheme other than http/https, ``invalid_url`` for a structurally
    unusable URL or embedded credentials, and ``blocked_destination`` when the
    host is or resolves to a non public address (including unresolvable hosts,
    which fail closed).
    """
    if _has_forbidden_characters(url):
        raise ApiError(400, "invalid_url", "The url contains invalid characters.")

    try:
        parts = urlsplit(url)
    except ValueError:
        raise ApiError(400, "invalid_url", "The url could not be parsed.")

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ApiError(
            400,
            "unsupported_scheme",
            "Only http and https URLs are supported.",
        )

    if parts.username is not None or parts.password is not None:
        raise ApiError(
            400,
            "invalid_url",
            "Embedded credentials are not allowed in the url.",
        )

    try:
        port = parts.port
    except ValueError:
        raise ApiError(400, "invalid_url", "The url contains an invalid port.")
    if port is not None and (port < 1 or port > 65535):
        raise ApiError(400, "invalid_url", "The url contains an invalid port.")

    host = parts.hostname
    if not host:
        raise ApiError(400, "invalid_url", "The url must include a host.")
    if len(host) > MAX_HOST_LENGTH:
        raise ApiError(400, "invalid_url", "The url host is too long.")

    literal = parse_host_as_ip(host)
    if literal is not None:
        if is_blocked_address(literal):
            raise ApiError(400, "blocked_destination", BLOCKED_MESSAGE)
        return url

    if not dns_enabled:
        return url

    addresses = resolve_host(host)
    if not addresses:
        raise ApiError(
            400,
            "blocked_destination",
            "The destination host could not be resolved.",
        )
    for text in addresses:
        try:
            resolved = ipaddress.ip_address(text)
        except ValueError:
            # Fail closed: an address we cannot parse cannot be cleared.
            raise ApiError(400, "blocked_destination", BLOCKED_MESSAGE)
        if is_blocked_address(resolved):
            raise ApiError(400, "blocked_destination", BLOCKED_MESSAGE)
    return url
