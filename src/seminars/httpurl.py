"""Which URLs may be fetched: hosts, addresses and the parts of a URL. No I/O.

Re-exported by ``seminars.http``; import these names from there.

URLs come from the configuration, from redirects and from scraped pages. A
URL is never passed on as it was written: ``parse_url`` takes it apart,
refuses what this project never fetches and builds the one canonical URL
that is sent. Whatever is checked (the host, the path against robots.txt)
is therefore exactly what goes out, and two spellings of one address share
one request queue, one budget and one robots.txt.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from seminars.httpbase import ForbiddenHostError, UnsupportedSchemeError
from seminars.textrules import strip_surrogates

MAX_URL_LENGTH: Final = 8192
"""Longer URLs are refused; servers do not take them either."""

_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
_INTERNAL_SUFFIXES: Final = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
_IPV4_PART: Final = re.compile(r"0x[0-9a-f]*|[0-9]+")  # a number the way inet_aton reads it
_LABEL: Final = re.compile(r"[a-z0-9_-]{1,63}")
_MAX_NAME_LENGTH: Final = 253
_UNICODE_NAME: Final = re.compile("[a-z0-9._\\-\u0080-\U0010ffff]+")
_CONTROL_CHARACTER: Final = re.compile(r"[\x00-\x1f\x7f]")
_ENCODED_DOT: Final = re.compile("%2e", re.IGNORECASE)
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


def _ipv4_number(part: str) -> int:
    """One part of an IPv4 address: decimal, octal (leading 0) or hexadecimal (leading 0x)."""
    if _IPV4_PART.fullmatch(part) is None:
        raise ValueError(f"{part!r} is not a number")
    if part.startswith("0x"):
        return int(part[2:] or "0", 16)
    return int(part, 8) if len(part) > 1 and part.startswith("0") else int(part)


def _ipv4(name: str) -> str:
    """Dotted-quad form of an IPv4 address written with one to four numbers.

    The last number fills all the bytes that are left, as ``inet_aton`` and
    browsers read it: ``127.1`` and ``2130706433`` are 127.0.0.1.
    """
    *leading, last = [_ipv4_number(part) for part in name.split(".")]
    if len(leading) > 3 or any(number > 255 for number in leading):
        raise ValueError(f"{name!r} is not an IPv4 address")
    if last >= 256 ** (4 - len(leading)):
        raise ValueError(f"{name!r} is not an IPv4 address")
    value = sum(number << (8 * (3 - index)) for index, number in enumerate(leading)) + last
    return str(ipaddress.IPv4Address(value))


def _a_labels(name: str) -> str:
    """ASCII form (IDNA A-labels) of a host name with non-ASCII characters.

    The HTTP library does the encoding, so that the name which is checked,
    looked up and rate limited is the very name a connection would be made
    to. It is imported here because few hosts need it and this module is
    also used where nothing is fetched.
    """
    import httpx

    if _UNICODE_NAME.fullmatch(name) is None:
        raise ValueError(f"{name!r} is not a host name")
    try:
        return httpx.URL(f"http://{name}/").raw_host.decode("ascii")
    except httpx.InvalidURL as error:
        raise ValueError(str(error)) from error


def _dns_name(name: str) -> str:
    """``name`` as a DNS name in ASCII; ``ValueError`` when it is not one."""
    ascii_name = name if name.isascii() else _a_labels(name)
    labels = ascii_name.split(".")
    if len(ascii_name) > _MAX_NAME_LENGTH or not all(_LABEL.fullmatch(label) for label in labels):
        raise ValueError(f"{name!r} is not a host name")
    return ascii_name


def canonical_host(host: str) -> str:
    """The one spelling of ``host`` that everything else uses; ``ValueError`` if it is no host.

    * Full-width characters and the ideographic full stop become ASCII, as
      HTTP clients map them; letters become lower case; one trailing dot is
      dropped. ``Example.EDU.`` is ``example.edu``.
    * A name with non-ASCII characters becomes its IDNA A-labels
      (``xn--...``), so the Unicode and the ASCII spelling are one host.
    * A host whose last label is a number is an IPv4 address to a resolver,
      in decimal, octal or hexadecimal and with one to four parts:
      ``2130706433``, ``127.1``, ``0x7f.1`` and ``0177.0.0.1`` all become
      ``127.0.0.1``. When it cannot be read that way it is no host at all.
    * An IPv6 address (without brackets) gets its shortest form. A zone id
      is not accepted.

    A name must consist of labels of 1 to 63 letters, digits, hyphens or
    underscores and be at most 253 characters long. Hosts are compared,
    checked, looked up, rate limited and sent in this form.
    """
    name = unicodedata.normalize("NFKC", host).replace(_IDEOGRAPHIC_FULL_STOP, ".").lower()
    name = name.removesuffix(".")
    try:
        if ":" in name:
            if "%" in name:
                raise ValueError("an address with a zone id")
            return str(ipaddress.IPv6Address(name))
        if _IPV4_PART.fullmatch(name.rsplit(".", 1)[-1]) is not None:
            return _ipv4(name)
        return _dns_name(name)
    except ValueError as error:
        raise ValueError(f"{host[:80]!a} is not a host name or an IP address") from error


def _is_public(name: str) -> bool:
    """``is_public_host`` for a host that is in canonical form already."""
    if name == "localhost" or name.endswith(_INTERNAL_SUFFIXES):
        return False
    try:
        return _is_global(ipaddress.ip_address(name))
    except ValueError:
        return True


def is_public_host(host: str) -> bool:
    """False for localhost, internal names and non-global IP addresses, however they are written.

    Sources are public web sites. URLs can also come from scraped pages and
    redirects, so anything that addresses this machine or a private network
    is refused instead of fetched.

    The host is judged in its canonical form (``canonical_host``): every
    way to write an IPv4 address counts as that address, an IPv6 address
    that wraps an IPv4 address (IPv4-mapped, NAT64, ...) is judged by that
    IPv4 address, and multicast addresses are not public. Text that is not
    a host at all is not a public host either.

    What this guarantees: a True answer means the host is not WRITTEN as a
    local or private address. It says nothing about where a name leads,
    because this function looks nothing up and does no I/O. A public name
    can resolve to 127.0.0.1 or to 169.254.169.254. Closing that is the job
    of the live client, which resolves every name before each request and
    checks the address of the peer before it reads an answer; see "Public
    hosts only" in ``seminars.http`` for those checks and for the risk that
    remains.
    """
    try:
        return _is_public(canonical_host(host))
    except ValueError:
        return False


def _without_dot_segments(path: str) -> str:
    """``path`` without its ``.`` and ``..`` segments (RFC 3986, section 5.2.4).

    ``%2e`` counts as a dot, as it does to browsers and to most servers.
    A server resolves these segments before it looks a path up, so the
    path with the segments still in it is not the one that robots.txt has
    to be asked about.
    """
    segments = path.split("/")[1:]  # the path of an http(s) URL starts with "/"
    kept: list[str] = []
    for position, segment in enumerate(segments, start=1):
        plain = _ENCODED_DOT.sub(".", segment)
        if plain not in (".", ".."):
            kept.append(segment)
            continue
        if plain == ".." and kept:
            kept.pop()
        if position == len(segments):
            kept.append("")  # "/a/b/.." is the directory "/a/"
    return "/" + "/".join(kept)


@dataclass(frozen=True)
class UrlParts:
    """A URL as the live client uses it; made by ``parse_url``."""

    url: str  # the canonical URL: what is sent, cached and looked up
    origin: str  # scheme://host, with the port unless it is the default one
    host: str  # in canonical form, IPv6 without brackets: names the queue, budget and lock
    path: str  # path and query as sent; this is what robots.txt is matched against


def parse_url(url: str) -> UrlParts:
    """Take ``url`` apart and build its canonical form; public http(s) only.

    Raises ``UnsupportedSchemeError`` for anything this project does not
    fetch: a URL that is not absolute http(s), one with a user name or
    password (nothing here needs one, and it would be sent as credentials),
    a port outside 1 to 65535, a host that is not a name or an IP address
    (``canonical_host``), control characters, a lone surrogate (see
    ``seminars.textrules``; such a string cannot be sent, hashed or
    stored), or more than ``MAX_URL_LENGTH`` characters. Raises
    ``ForbiddenHostError`` for a host that ``is_public_host`` rejects.

    Like that function it judges the spelling of the host only and does no
    lookup: passing this check is necessary, not sufficient. The offline
    client needs no more, since it never opens a connection; the live
    client adds the lookup and the peer check described in ``seminars.http``.

    The canonical URL has a lower-case scheme, the host in canonical form,
    the port only when it is not the default of the scheme, and no fragment.
    Dot segments are removed from the path (``/a/../b`` is ``/b``, also when
    a dot is written ``%2e``): robots.txt is matched against the path that
    the server will look up. An empty path is ``/``. Everything else in
    path and query stays as written.
    """
    if strip_surrogates(url) != url:
        raise UnsupportedSchemeError(url, "a URL with a lone surrogate cannot be fetched")
    if len(url) > MAX_URL_LENGTH:
        raise UnsupportedSchemeError(url[:200], f"invalid URL: longer than {MAX_URL_LENGTH}")
    if _CONTROL_CHARACTER.search(url) is not None:
        raise UnsupportedSchemeError(url, "invalid URL: control characters")
    try:
        parts = urlsplit(url)
        hostname, port = parts.hostname, parts.port
    except ValueError as error:  # unbalanced brackets, a port that is no number, ...
        raise UnsupportedSchemeError(url, "invalid URL") from error
    scheme = parts.scheme
    if scheme not in _DEFAULT_PORTS or not hostname:
        raise UnsupportedSchemeError(url, "only absolute http(s) URLs can be fetched")
    if "@" in parts.netloc:
        raise UnsupportedSchemeError(url, "a URL with a user name or password cannot be fetched")
    if port == 0:
        raise UnsupportedSchemeError(url, "invalid URL: port 0")
    try:
        host = canonical_host(hostname)
    except ValueError as error:
        raise UnsupportedSchemeError(
            url, "invalid URL: the host is neither a name nor an IP address"
        ) from error
    if not _is_public(host):
        raise ForbiddenHostError(url, "refusing a local or private network address")
    origin = f"{scheme}://[{host}]" if ":" in host else f"{scheme}://{host}"
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        origin = f"{origin}:{port}"
    path = _without_dot_segments(parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return UrlParts(url=origin + path, origin=origin, host=host, path=path)


def split_url(url: str) -> tuple[str, str, str]:
    """Split a URL into ``(origin, host, path-with-query)``; public http(s) only.

    The three values are those of ``parse_url``, which also says what is
    refused: ``origin`` and ``host`` are in canonical form, so every
    spelling of a host shares one request queue and one budget.
    """
    parts = parse_url(url)
    return parts.origin, parts.host, parts.path
