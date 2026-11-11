"""Which URLs may be fetched: hosts, addresses and the parts of a URL. No I/O.

Re-exported by ``seminars.http``; import these names from there.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
from typing import Final
from urllib.parse import urlsplit

from seminars.httpbase import ForbiddenHostError, UnsupportedSchemeError
from seminars.textrules import strip_surrogates

_INTERNAL_SUFFIXES: Final = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
_NUMERIC_LABEL: Final = re.compile(r"[0-9]+|0x[0-9a-f]*")  # what makes a host an IPv4 address
_IDEOGRAPHIC_FULL_STOP: Final = chr(0x3002)  # a label separator to IDNA, like "."
_IPV4_IN_IPV6: Final = tuple(
    ipaddress.IPv6Network(network)
    for network in (
        "::ffff:0:0/96",  # IPv4-mapped
        "::ffff:0:0:0/96",  # IPv4-translated
        "64:ff9b::/96",  # NAT64
        "::/96",  # IPv4-compatible (retired)
    )
)
"""IPv6 ranges whose last 32 bits are an IPv4 address that the packet ends up at."""


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address that an IPv6 address merely wraps, if it is of such a kind."""
    if any(address in network for network in _IPV4_IN_IPV6):
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def _is_global(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for a globally routable unicast address.

    An IPv6 address that wraps an IPv4 address (IPv4-mapped, IPv4-translated,
    IPv4-compatible, NAT64) counts as that IPv4 address. Multicast and the
    retired site-local range are not global, although ``ipaddress`` does not
    list them as special.
    """
    if isinstance(address, ipaddress.IPv6Address):
        wrapped = _embedded_ipv4(address)
        if wrapped is not None:
            return _is_global(wrapped)
        if address.is_site_local:
            return False
    return address.is_global and not address.is_multicast


def is_public_address(address: str) -> bool:
    """True when ``address``, an IP address as text, is globally routable.

    This is how the answers of a resolver and the peer of a connection are
    judged. A zone id (``fe80::1%eth0``) is ignored, and an IPv6 address
    that wraps an IPv4 address (IPv4-mapped, NAT64, ...) counts as that IPv4
    address. Loopback, link-local, private, carrier-grade NAT, multicast,
    documentation and other reserved ranges are not public, and neither is
    text that is not an IP address.
    """
    try:
        return _is_global(ipaddress.ip_address(address.partition("%")[0]))
    except ValueError:
        return False


def canonical_host(host: str) -> str:
    """One spelling per host: lower case, ASCII dots and digits, no trailing dot.

    HTTP clients accept full-width characters and the ideographic full stop
    in a host and map them to ASCII, so ``127`` + U+3002 + ``0.0.1`` and
    ``Example.EDU.`` are other ways to write ``127.0.0.1`` and
    ``example.edu``. Hosts are compared, checked and rate limited in this
    form.
    """
    return (
        unicodedata.normalize("NFKC", host).replace(_IDEOGRAPHIC_FULL_STOP, ".").lower().rstrip(".")
    )


def is_public_host(host: str) -> bool:
    """False for localhost, internal names and non-global IP addresses, however they are written.

    Sources are public web sites. URLs can also come from scraped pages and
    redirects, so anything that addresses this machine or a private network
    is refused instead of fetched.

    A host whose last label is a number is an IPv4 address to the resolver,
    in decimal, octal or hexadecimal and with one to four parts:
    ``2130706433``, ``127.1``, ``0x7f.1`` and ``0177.0.0.1`` all mean
    127.0.0.1. Such a host is read the way the resolver reads it; when that
    fails it is not a name either, and it is refused. An IPv6 address that
    wraps an IPv4 address (IPv4-mapped, NAT64, ...) is judged by that IPv4
    address, and multicast addresses are not public.

    What this guarantees: a True answer means the host is not WRITTEN as a
    local or private address. It says nothing about where a name leads,
    because this function looks nothing up and does no I/O. A public name
    can resolve to 127.0.0.1 or to 169.254.169.254. Closing that is the job
    of the live client, which resolves every name before each request and
    checks the address of the peer before it reads an answer; see "Public
    hosts only" in ``seminars.http`` for those checks and for the risk that
    remains.
    """
    name = canonical_host(host)
    if not name or name == "localhost" or name.endswith(_INTERNAL_SUFFIXES):
        return False
    try:
        return _is_global(ipaddress.ip_address(name))
    except ValueError:
        pass
    if _NUMERIC_LABEL.fullmatch(name.rsplit(".", 1)[-1]) is None:
        return True
    try:
        return _is_global(ipaddress.IPv4Address(socket.inet_aton(name)))
    except (OSError, ValueError):
        return False


def split_url(url: str) -> tuple[str, str, str]:
    """Split a URL into ``(origin, host, path-with-query)``; public http(s) only.

    Raises ``UnsupportedSchemeError`` for anything that is not an absolute
    http(s) URL and ``ForbiddenHostError`` for a host that ``is_public_host``
    rejects. Like that function it judges the spelling of the host only and
    does no lookup: passing this check is necessary, not sufficient. The
    offline client needs no more, since it never opens a connection; the
    live client adds the lookup and the peer check described in
    ``seminars.http``.

    ``host`` is in canonical form (see ``canonical_host``), so every spelling
    of a host shares one request queue and one budget. A URL that contains
    a lone surrogate (see ``seminars.textrules``) cannot be sent, hashed or
    stored and is refused like any other string that is not a URL.
    """
    if strip_surrogates(url) != url:
        raise UnsupportedSchemeError(url, "a URL with a lone surrogate cannot be fetched")
    parts = urlsplit(url)
    host = canonical_host(parts.hostname or "")
    if parts.scheme not in ("http", "https") or not host:
        raise UnsupportedSchemeError(url, "only absolute http(s) URLs can be fetched")
    if not is_public_host(host):
        raise ForbiddenHostError(url, "refusing a local or private network address")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return f"{parts.scheme}://{parts.netloc.lower()}", host, path
