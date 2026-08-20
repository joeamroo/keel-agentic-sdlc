"""Validation of user supplied target URLs.

A URL is only stored after it passes every check here, and the stored value is
exactly what is later served as a redirect ``Location``, so nothing that skipped
validation can be reached.

Policy:
  * only the ``http`` and ``https`` schemes are accepted, by allow-list;
  * embedded credentials are rejected;
  * the host is resolved and every resolved address must be a global unicast
    address. Loopback, private, link-local, multicast, reserved and unspecified
    addresses are rejected, with 169.254.169.254 (the cloud metadata endpoint)
    denied explicitly.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import List, Union
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("app.urls")

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Cloud metadata endpoints. 169.254.169.254 is already covered by the
# link-local rule but is denied by name because reaching it is how a URL
# handler becomes a credential leak.
METADATA_ADDRESSES = frozenset({"169.254.169.254", "fd00:ec2::254"})

MAX_HOSTNAME_LENGTH = 253

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


class InvalidURLError(ValueError):
    """Raised when a target URL may not be stored or served."""


def _normalise_ip(ip: IPAddress) -> IPAddress:
    """Unwrap IPv4-in-IPv6 representations.

    Returns the embedded IPv4 address for mapped and 6to4 addresses, otherwise
    the address unchanged. Raises nothing.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip.sixtofour is not None:
            return ip.sixtofour
    return ip


def is_forbidden_address(ip: IPAddress) -> bool:
    """Report whether an IP address must not be used as a redirect target.

    Returns True for loopback, private, link-local, multicast, reserved,
    unspecified, metadata and any other non globally routable address. Raises
    nothing.
    """
    candidate = _normalise_ip(ip)
    if str(candidate) in METADATA_ADDRESSES:
        return True
    if (
        candidate.is_loopback
        or candidate.is_private
        or candidate.is_link_local
        or candidate.is_multicast
        or candidate.is_reserved
        or candidate.is_unspecified
    ):
        return True
    return not candidate.is_global


def _resolve(hostname: str, port: int) -> List[str]:
    """Resolve a hostname to the list of textual IP addresses it points at.

    Returns a non-empty list of address strings. Raises InvalidURLError when
    resolution fails or yields nothing usable.
    """
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise InvalidURLError("The host of the target url could not be resolved.") from exc
    addresses: List[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        address = sockaddr[0]
        if isinstance(address, str) and address:
            addresses.append(address.split("%", 1)[0])
    if not addresses:
        raise InvalidURLError("The host of the target url could not be resolved.")
    return addresses


def validate_target_url(raw: str, max_length: int) -> str:
    """Validate and normalise a user supplied target URL.

    Returns the normalised absolute URL that must be stored and later served.
    Raises InvalidURLError when the URL is empty, too long, malformed, uses a
    scheme other than http/https, embeds credentials, or resolves to an address
    that is not globally routable.
    """
    candidate = raw.strip()
    if not candidate:
        raise InvalidURLError("A target url is required.")
    if len(candidate) > max_length:
        raise InvalidURLError(
            "The target url exceeds the maximum length of %d characters." % max_length
        )
    for character in candidate:
        if ord(character) < 0x20 or ord(character) == 0x7F:
            raise InvalidURLError("The target url contains control characters.")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise InvalidURLError("The target url could not be parsed.") from exc

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidURLError("Only http and https target urls are allowed.")
    if not parts.netloc:
        raise InvalidURLError("The target url must include a host.")
    if "@" in parts.netloc:
        raise InvalidURLError("Embedded credentials are not allowed in the target url.")

    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise InvalidURLError("The target url has an invalid host or port.") from exc

    if not hostname:
        raise InvalidURLError("The target url must include a host.")
    if port is not None and not 1 <= port <= 65535:
        raise InvalidURLError("The target url has an invalid port.")

    literal_ip = None
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        ascii_host = str(literal_ip)
        addresses = [ascii_host]
    else:
        host = hostname.rstrip(".")
        if not host:
            raise InvalidURLError("The target url must include a host.")
        try:
            ascii_host = host.encode("idna").decode("ascii").lower()
        except (UnicodeError, UnicodeDecodeError) as exc:
            raise InvalidURLError(
                "The host of the target url is not a valid domain name."
            ) from exc
        if len(ascii_host) > MAX_HOSTNAME_LENGTH:
            raise InvalidURLError("The host of the target url is too long.")
        default_port = 443 if scheme == "https" else 80
        addresses = _resolve(ascii_host, port if port is not None else default_port)

    for address in addresses:
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError as exc:
            raise InvalidURLError(
                "The host of the target url resolved to an unusable address."
            ) from exc
        if is_forbidden_address(resolved):
            raise InvalidURLError("The target url resolves to a blocked network address.")

    if isinstance(literal_ip, ipaddress.IPv6Address) or (literal_ip is None and ":" in ascii_host):
        host_part = "[%s]" % ascii_host
    else:
        host_part = ascii_host
    netloc = host_part if port is None else "%s:%d" % (host_part, port)

    normalised = urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))
    if len(normalised) > max_length:
        raise InvalidURLError(
            "The target url exceeds the maximum length of %d characters." % max_length
        )
    return normalised
