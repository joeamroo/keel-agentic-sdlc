"""Target URL validation and normalisation.

Only ``http`` and ``https`` URLs are accepted (allowlist, not a blocklist).
Embedded credentials are rejected, hosts are resolved and every resulting
address must be a public unicast address; the cloud metadata address
169.254.169.254 is denied explicitly.  The normalised value returned here is
the value that is stored and later served as the redirect Location.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional, Union
from urllib.parse import quote, urlsplit, urlunsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
METADATA_ADDRESSES = frozenset({"169.254.169.254", "fd00:ec2::254"})
MAX_HOST_LENGTH = 255

_SAFE_PATH = "/%:@!$&'()*+,;=~-._"
_SAFE_QUERY = "/?%:@!$&'()*+,;=~-._"

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


class UrlValidationError(ValueError):
    """Raised when a supplied target URL is malformed or unsafe."""


def _parse_ip_literal(host: str) -> Optional[IPAddress]:
    """Parse a host as an IP literal.

    Returns the parsed address, or ``None`` when the host is not an IP literal.
    Raises nothing.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _assert_ip_allowed(address: IPAddress) -> None:
    """Reject addresses that must never be fetched or redirected to.

    Returns ``None`` when the address is a public unicast address.  Raises
    :class:`UrlValidationError` for loopback, private, link-local, multicast,
    reserved, unspecified or metadata addresses.
    """
    candidate: IPAddress = address
    if isinstance(candidate, ipaddress.IPv6Address):
        mapped = candidate.ipv4_mapped
        if mapped is not None:
            candidate = mapped
    if str(candidate) in METADATA_ADDRESSES:
        raise UrlValidationError("target_url host resolves to a blocked address")
    if (
        candidate.is_loopback
        or candidate.is_private
        or candidate.is_link_local
        or candidate.is_multicast
        or candidate.is_reserved
        or candidate.is_unspecified
        or not candidate.is_global
    ):
        raise UrlValidationError("target_url host resolves to a non-public address")


def _normalise_host(hostname: str) -> str:
    """Normalise a hostname to lowercase ASCII (IDNA encoded when needed).

    Returns the ASCII hostname.  Raises :class:`UrlValidationError` when the
    host is empty, too long or cannot be IDNA encoded.
    """
    host = hostname.strip()
    if not host:
        raise UrlValidationError("target_url must include a host")
    if len(host) > MAX_HOST_LENGTH:
        raise UrlValidationError("target_url host is too long")
    if host.isascii():
        return host.lower()
    try:
        return host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError) as exc:
        raise UrlValidationError("target_url host is not a valid domain name") from exc


def _assert_host_allowed(host: str) -> None:
    """Resolve a host and reject it when any address is not public.

    Returns ``None`` when every resolved address is a public unicast address.
    Raises :class:`UrlValidationError` when the host cannot be resolved or maps
    to a blocked address.
    """
    literal = _parse_ip_literal(host)
    if literal is not None:
        _assert_ip_allowed(literal)
        return
    lowered = host.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise UrlValidationError("target_url host resolves to a non-public address")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise UrlValidationError("target_url host could not be resolved") from exc
    if not infos:
        raise UrlValidationError("target_url host could not be resolved")
    for info in infos:
        sockaddr = info[4]
        raw_address = str(sockaddr[0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise UrlValidationError("target_url host could not be resolved") from exc
        _assert_ip_allowed(address)


def validate_target_url(raw_url: str, max_length: int) -> str:
    """Validate a user supplied target URL and return its normalised form.

    The returned ASCII URL is the value that must be stored and later served as
    the redirect destination.  Raises :class:`UrlValidationError` when the URL
    is empty, too long, contains control characters or credentials, uses a
    scheme other than http/https, or resolves to a non-public address.
    """
    candidate = raw_url.strip()
    if not candidate:
        raise UrlValidationError("target_url must not be empty")
    if len(candidate) > max_length:
        raise UrlValidationError(f"target_url must be at most {max_length} characters")
    for char in candidate:
        if ord(char) < 0x20 or ord(char) == 0x7F:
            raise UrlValidationError("target_url must not contain control characters")
    try:
        split = urlsplit(candidate)
    except ValueError as exc:
        raise UrlValidationError("target_url is not a valid URL") from exc

    scheme = split.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlValidationError("target_url scheme must be http or https")
    if "@" in split.netloc or split.username or split.password:
        raise UrlValidationError("target_url must not contain embedded credentials")

    hostname = split.hostname
    if not hostname:
        raise UrlValidationError("target_url must include a host")
    try:
        port = split.port
    except ValueError as exc:
        raise UrlValidationError("target_url has an invalid port") from exc
    if port is not None and not (1 <= port <= 65535):
        raise UrlValidationError("target_url has an invalid port")

    host_ascii = _normalise_host(hostname)
    _assert_host_allowed(host_ascii)

    netloc_host = f"[{host_ascii}]" if ":" in host_ascii else host_ascii
    netloc = netloc_host if port is None else f"{netloc_host}:{port}"

    path = quote(split.path, safe=_SAFE_PATH)
    query = quote(split.query, safe=_SAFE_QUERY)
    fragment = quote(split.fragment, safe=_SAFE_QUERY)
    normalised = urlunsplit((scheme, netloc, path, query, fragment))

    if not normalised.isascii():
        raise UrlValidationError("target_url must be representable in ASCII")
    if len(normalised) > max_length:
        raise UrlValidationError(f"target_url must be at most {max_length} characters")
    return normalised
