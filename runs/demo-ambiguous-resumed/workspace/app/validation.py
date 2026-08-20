"""Destination URL parsing, normalization and SSRF validation.

Validation is limited to parsing and name resolution: the service never opens a
socket to a user supplied destination host or port, at creation or at redirect
time.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Lock
from typing import Dict, Final, FrozenSet, List, Optional, Tuple, Union
from urllib.parse import quote, urlsplit, urlunsplit

import idna

from .config import Settings

LOGGER = logging.getLogger("links.validation")

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

ALLOWED_SCHEMES: Final[FrozenSet[str]] = frozenset({"http", "https"})
DEFAULT_PORTS: Final[Dict[str, int]] = {"http": 80, "https": 443}

_PATH_SAFE: Final[str] = "/%:@!$&'()*+,;=~-._"
_QUERY_SAFE: Final[str] = "/%:@!$&'()*+,;=?~-._"
_FRAGMENT_SAFE: Final[str] = "/%:@!$&'()*+,;=?~-._"

_ASCII_HOST_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)

_DENIED_IPV4_NETWORKS: Final[Tuple[ipaddress.IPv4Network, ...]] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.31.196.0/24",
        "192.52.193.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
    )
)

_DENIED_IPV6_NETWORKS: Final[Tuple[ipaddress.IPv6Network, ...]] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/32",
        "2001:2::/48",
        "2001:db8::/32",
        "2002::/16",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)

# Cloud metadata endpoints are denied explicitly: reaching one is how a URL
# fetcher turns into a credential leak.
_EXPLICIT_DENIED_ADDRESSES: Final[FrozenSet[IPAddress]] = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("169.254.170.2"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


class DestinationError(Exception):
    """Raised when a destination URL is malformed, disallowed or not routable."""

    def __init__(self, code: str, message: str) -> None:
        """Create a destination validation error.

        Args:
            code: Machine readable error code such as ``scheme_not_allowed``.
            message: Static message; it never echoes the submitted URL or a resolved IP.

        Returns:
            None.

        Raises:
            Nothing.
        """
        super().__init__(message)
        self.code = code
        self.message = message


def is_denied_address(address: IPAddress) -> bool:
    """Report whether an IP address is in a range the service refuses to reach.

    Loopback, private, carrier grade NAT, link local (including the cloud metadata
    addresses), unique local, multicast, reserved and unspecified ranges are all
    denied, as are IPv6 forms that embed such an IPv4 address.

    Args:
        address: The address to test.

    Returns:
        ``True`` when the address must not be used as a destination.

    Raises:
        Nothing.
    """
    if address in _EXPLICIT_DENIED_ADDRESSES:
        return True
    if isinstance(address, ipaddress.IPv6Address):
        embedded: List[ipaddress.IPv4Address] = []
        if address.ipv4_mapped is not None:
            embedded.append(address.ipv4_mapped)
        if address.sixtofour is not None:
            embedded.append(address.sixtofour)
        teredo = address.teredo
        if teredo is not None:
            embedded.extend([teredo[0], teredo[1]])
        for candidate in embedded:
            if is_denied_address(candidate):
                return True
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return True
    if not address.is_global:
        return True
    networks = _DENIED_IPV4_NETWORKS if address.version == 4 else _DENIED_IPV6_NETWORKS
    return any(address in network for network in networks)


def _encode_host(host: str) -> str:
    """Normalise a hostname to a lowercase ASCII (punycode A-label) form.

    Args:
        host: The host component as parsed from the URL.

    Returns:
        The IDNA A-label form of the host, lowercased and without a trailing dot.

    Raises:
        DestinationError: If the host is not a usable domain name.
    """
    candidate = host.strip()
    while candidate.endswith("."):
        candidate = candidate[:-1]
    if not candidate:
        raise DestinationError("invalid_url", "The destination URL must include a host.")
    try:
        return idna.encode(candidate, uts46=True, transitional=False).decode("ascii").lower()
    except (idna.IDNAError, UnicodeError, ValueError):
        if _ASCII_HOST_RE.match(candidate):
            return candidate.lower()
        raise DestinationError("invalid_url", "The destination host is not a valid domain name.")


def normalize_destination(raw_url: str, settings: Settings) -> Tuple[str, str]:
    """Parse and normalise a caller supplied destination URL.

    The returned URL is what gets stored and later served, so nothing that skipped
    this function can ever be reached through a short link.

    Args:
        raw_url: The raw string supplied by the caller.
        settings: Active configuration (supplies the maximum URL length).

    Returns:
        A tuple ``(normalized_url, host)`` where ``host`` is the ASCII host to
        resolve (an IP literal is returned in canonical form).

    Raises:
        DestinationError: If the URL is too long, malformed, carries credentials or
            uses a scheme other than http/https.
    """
    if len(raw_url) > settings.max_url_length:
        raise DestinationError("url_too_long", "The destination URL is longer than the allowed maximum.")
    for character in raw_url:
        if character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F:
            raise DestinationError("invalid_url", "The destination URL contains whitespace or control characters.")

    try:
        parts = urlsplit(raw_url)
    except ValueError:
        raise DestinationError("invalid_url", "The destination URL could not be parsed.")

    scheme = parts.scheme.lower()
    if not scheme:
        raise DestinationError("invalid_url", "The destination URL must be absolute and include a scheme.")
    if scheme not in ALLOWED_SCHEMES:
        raise DestinationError("scheme_not_allowed", "Only http and https destinations are allowed.")

    netloc = parts.netloc
    if not netloc:
        raise DestinationError("invalid_url", "The destination URL must include a host.")
    if "@" in netloc:
        raise DestinationError("credentials_in_url", "Embedded credentials are not allowed in a destination URL.")

    try:
        port = parts.port
    except ValueError:
        raise DestinationError("invalid_url", "The destination URL has an invalid port.")
    if port is not None and not 1 <= port <= 65535:
        raise DestinationError("invalid_url", "The destination URL has an invalid port.")

    host_raw = parts.hostname
    if not host_raw:
        raise DestinationError("invalid_url", "The destination URL must include a host.")

    literal: Optional[IPAddress]
    try:
        literal = ipaddress.ip_address(host_raw)
    except ValueError:
        literal = None

    if netloc.startswith("[") and (literal is None or literal.version != 6):
        raise DestinationError("invalid_url", "The destination URL has an invalid IPv6 host.")

    if literal is not None:
        host = str(literal)
        netloc_host = "[{0}]".format(host) if literal.version == 6 else host
    else:
        host = _encode_host(host_raw)
        netloc_host = host

    if port is not None and port != DEFAULT_PORTS[scheme]:
        normalized_netloc = "{0}:{1}".format(netloc_host, port)
    else:
        normalized_netloc = netloc_host

    path = quote(parts.path, safe=_PATH_SAFE)
    query = quote(parts.query, safe=_QUERY_SAFE)
    fragment = quote(parts.fragment, safe=_FRAGMENT_SAFE)
    normalized = urlunsplit((scheme, normalized_netloc, path, query, fragment))

    try:
        normalized.encode("ascii")
    except UnicodeEncodeError:
        raise DestinationError("invalid_url", "The destination URL could not be normalized.")

    if len(normalized) > settings.max_url_length:
        raise DestinationError("url_too_long", "The destination URL is longer than the allowed maximum.")
    return normalized, host


def host_of(url: str) -> Optional[str]:
    """Extract the host from an already normalised URL.

    Args:
        url: A stored, normalised absolute URL.

    Returns:
        The lowercase host, or ``None`` when the URL has no host.

    Raises:
        Nothing.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    return parsed.hostname


class DestinationValidator:
    """Resolves destination hosts and rejects any that map to non routable space."""

    def __init__(self, settings: Settings) -> None:
        """Create a validator bound to the active settings.

        Args:
            settings: Active configuration (DNS timeout, cache TTL, escape hatch).

        Returns:
            None.

        Raises:
            Nothing.
        """
        self._settings = settings
        self._cache: Dict[str, Tuple[float, Tuple[str, ...]]] = {}
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dns")

    def close(self) -> None:
        """Shut down the resolver thread pool.

        Returns:
            None.

        Raises:
            Nothing.
        """
        self._executor.shutdown(wait=False)

    def assert_routable(self, host: str) -> None:
        """Reject a host that is, or resolves to, a non routable address.

        Args:
            host: ASCII host (domain name or IP literal) taken from a normalised URL.

        Returns:
            None when every resolved address is publicly routable.

        Raises:
            DestinationError: With code ``destination_not_routable`` when any
                resolved address is denied, or ``destination_unresolvable`` when the
                host cannot be resolved within the configured timeout.
        """
        if self._settings.allow_private_destinations:
            LOGGER.debug("destination range checks disabled by configuration")
            return
        if not host:
            raise DestinationError("invalid_url", "The destination URL must include a host.")

        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None

        if literal is not None:
            if is_denied_address(literal):
                raise DestinationError(
                    "destination_not_routable",
                    "The destination host is not a publicly routable address.",
                )
            return

        addresses = self.resolve(host)
        for text in addresses:
            try:
                resolved = ipaddress.ip_address(text)
            except ValueError:
                raise DestinationError(
                    "destination_not_routable",
                    "The destination host is not a publicly routable address.",
                )
            if is_denied_address(resolved):
                raise DestinationError(
                    "destination_not_routable",
                    "The destination host is not a publicly routable address.",
                )

    def resolve(self, host: str) -> Tuple[str, ...]:
        """Resolve A and AAAA records for a host with a bounded timeout.

        Args:
            host: ASCII domain name.

        Returns:
            A non empty tuple of textual IP addresses.

        Raises:
            DestinationError: With code ``destination_unresolvable`` on NXDOMAIN,
                resolver error, empty answer or timeout.
        """
        ttl = self._settings.dns_cache_ttl_seconds
        now = time.monotonic()
        if ttl > 0:
            with self._lock:
                cached = self._cache.get(host)
                if cached is not None and cached[0] > now:
                    return cached[1]

        future = self._executor.submit(
            socket.getaddrinfo, host, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
        try:
            infos = future.result(timeout=self._settings.dns_timeout_ms / 1000.0)
        except FutureTimeoutError:
            future.cancel()
            raise DestinationError("destination_unresolvable", "The destination host could not be resolved.")
        except (socket.gaierror, UnicodeError, OSError, ValueError):
            raise DestinationError("destination_unresolvable", "The destination host could not be resolved.")

        addresses = tuple(
            sorted({str(info[4][0]).split("%")[0] for info in infos if info and info[4]})
        )
        if not addresses:
            raise DestinationError("destination_unresolvable", "The destination host could not be resolved.")

        if ttl > 0:
            with self._lock:
                if len(self._cache) > 10000:
                    self._cache.clear()
                self._cache[host] = (now + ttl, addresses)
        return addresses
