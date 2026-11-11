"""HTTP building blocks without any I/O: the client protocol, errors and URL helpers.

Kept separate from ``seminars.http`` so that modules which only need the
types (for example ``seminars.fixtures``) do not import the live client.
Everything here is re-exported by ``seminars.http``; import it from there.
"""

from __future__ import annotations

import codecs
import ipaddress
import re
import socket
import unicodedata
from collections.abc import Callable, Collection, Mapping
from typing import Final, Protocol, runtime_checkable
from urllib.parse import urlsplit

from seminars.models import FetchedDocument
from seminars.textrules import ascii_safe

Clock = Callable[[], float]
"""Returns the current time as epoch seconds."""

Sleeper = Callable[[float], None]
"""Blocks for the given number of seconds."""

REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
RETRY_STATUSES: Final = frozenset({408, 425, 429, 500, 502, 503, 504})
BINARY_MEDIA_PREFIXES: Final = ("image/", "audio/", "video/", "font/")
_INTERNAL_SUFFIXES: Final = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
_NUMERIC_LABEL: Final = re.compile(r"[0-9]+|0x[0-9a-f]*")  # what makes a host an IPv4 address
_IDEOGRAPHIC_FULL_STOP: Final = chr(0x3002)  # a label separator to IDNA, like "."
BINARY_MEDIA_TYPES: Final = frozenset(
    {
        "application/gzip",
        "application/octet-stream",
        "application/pdf",
        "application/x-gzip",
        "application/zip",
    }
)


_SNIFF_BYTES: Final = 2048
_FALLBACK_ENCODING: Final = "cp1252"
_BOM_ENCODINGS: Final = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),  # before UTF-16 LE, whose mark is a prefix of this one
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_LABEL: Final = r"[A-Za-z0-9._:-]+"
_HEADER_CHARSET: Final = re.compile(rf"""charset\s*=\s*["']?({_LABEL})""", re.IGNORECASE)
_XML_ENCODING: Final = re.compile(
    rf"""^\s*<\?xml[^>]*?\bencoding\s*=\s*["']({_LABEL})""".encode("ascii")
)
_META_CHARSET: Final = re.compile(
    rf"""<meta\b[^>]*?\bcharset\s*=\s*["']?\s*({_LABEL})""".encode("ascii"), re.IGNORECASE
)
_LATIN1_CODECS: Final = frozenset({"iso8859-1", "ascii"})
_REFUSED_CODECS: Final = frozenset({"utf-7"})  # obsolete; lets ASCII text smuggle other text
_WIDE_CODEC_PREFIXES: Final = ("utf-16", "utf-32")


def media_type(content_type: str | None) -> str | None:
    """``"Text/HTML; charset=utf-8"`` -> ``"text/html"``; ``None`` stays ``None``."""
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower() or None


def is_binary_media_type(value: str | None) -> bool:
    """True for media types this project never downloads (images, archives, ...)."""
    return value is not None and (
        value.startswith(BINARY_MEDIA_PREFIXES) or value in BINARY_MEDIA_TYPES
    )


def _text_codec(label: str | None) -> str | None:
    """Python's name of the text encoding called ``label``; ``None`` when it is not usable.

    ISO-8859-1 and ASCII labels are read as windows-1252, as browsers do:
    pages labelled that way routinely contain windows-1252 punctuation.
    UTF-7 is refused, also as in browsers.
    """
    if not label:
        return None
    try:
        b"".decode(label)  # also rejects codecs that are not text encodings (base64, ...)
        codec = codecs.lookup(label).name
    except (LookupError, ValueError):
        return None
    if codec in _REFUSED_CODECS:
        return None
    return _FALLBACK_ENCODING if codec in _LATIN1_CODECS else codec


def _declared_encoding(head: bytes) -> str | None:
    """The encoding an XML declaration or an HTML ``<meta>`` tag in ``head`` names."""
    for pattern in (_XML_ENCODING, _META_CHARSET):
        match = pattern.search(head)
        if match is None:
            continue
        codec = _text_codec(match.group(1).decode("ascii"))
        # A declaration that can be read as ASCII is not in a 16 or 32 bit encoding.
        if codec is not None and not codec.startswith(_WIDE_CODEC_PREFIXES):
            return codec
    return None


def decode_body(content: bytes, content_type: str | None) -> str:
    """Decode a response body to text.

    The encoding is taken from, in this order: a byte order mark (which is
    consumed); the ``charset`` parameter of the Content-Type header; the
    ``encoding`` of an XML declaration or the charset of an HTML ``<meta>``
    tag within the first 2048 bytes. A body that states nothing is UTF-8 if
    it is valid UTF-8 and windows-1252 otherwise, which is what old
    hand-written pages use. Unknown labels are skipped, and bytes that are
    invalid in the chosen encoding become U+FFFD.
    """
    encoding = next((name for mark, name in _BOM_ENCODINGS if content.startswith(mark)), None)
    if encoding is None:
        header = _HEADER_CHARSET.search(content_type or "")
        encoding = _text_codec(header.group(1) if header is not None else None)
    if encoding is None:
        encoding = _declared_encoding(content[:_SNIFF_BYTES])
    if encoding is None:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            encoding = _FALLBACK_ENCODING
    try:
        return content.decode(encoding, errors="replace")
    except (LookupError, ValueError):  # exotic codecs that refuse the "replace" handler
        return content.decode("utf-8", errors="replace")


class HttpError(Exception):
    """Base class of every error raised by an ``HttpClient``."""

    def __init__(self, url: str, message: str) -> None:
        self.url = url
        super().__init__(f"{message}: {ascii_safe(url)}")


class UnsupportedSchemeError(HttpError, ValueError):
    """The URL is not an absolute http(s) URL."""


class ForbiddenHostError(HttpError, ValueError):
    """The URL points at this machine or a private network, never at a public site."""


class RobotsDisallowedError(HttpError):
    """robots.txt forbids fetching the URL."""


class RobotsUnavailableError(HttpError):
    """robots.txt could not be fetched, so its origin is off limits for the rest of the run."""


class CrawlDelayTooLongError(HttpError):
    """The host asks for a longer crawl delay than ``max_crawl_delay_seconds``."""


class HostBudgetExceededError(HttpError):
    """The per-host request budget of this run is used up."""


class HttpStatusError(HttpError):
    """The final response has a status outside 2xx that the caller did not allow."""

    def __init__(self, url: str, status: int) -> None:
        self.status = status
        super().__init__(url, f"HTTP {status}")


class HttpRequestError(HttpError):
    """Timeout or network failure that persisted through all retries."""


class TooManyRedirectsError(HttpError):
    """More than ``max_redirects`` redirects in a row."""


class UnsupportedContentTypeError(HttpError):
    """The response is an image or another binary type; its body was not read."""

    def __init__(self, url: str, content_type: str | None) -> None:
        self.content_type = content_type
        super().__init__(url, f"refusing binary content type {content_type}")


class ResponseTooLargeError(HttpError):
    """The body is larger than ``max_body_bytes``; reading it was given up."""

    def __init__(self, url: str, limit: int) -> None:
        self.limit = limit
        super().__init__(url, f"response body is larger than {limit} bytes")


@runtime_checkable
class HttpClient(Protocol):
    """What adapters use to fetch. Implemented by PoliteClient and OfflineHttpClient."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        allow_statuses: Collection[int] = (),
    ) -> FetchedDocument:
        """Fetch ``url`` and return its body as text in canonical form.

        ``headers`` adds request headers (User-Agent cannot be overridden).
        A final status outside 2xx raises ``HttpStatusError`` unless it is
        listed in ``allow_statuses``.
        """
        ...


def _is_global(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for a globally routable address; an IPv4-mapped IPv6 address counts as its IPv4."""
    mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    return (mapped or address).is_global


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
    fails it is not a name either, and it is refused. An IPv4-mapped IPv6
    address is judged by its IPv4 address. Names are not looked up, so a
    public name that resolves to a private address is not caught here.
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

    ``host`` is in canonical form (see ``canonical_host``), so every spelling
    of a host shares one request queue and one budget.
    """
    parts = urlsplit(url)
    host = canonical_host(parts.hostname or "")
    if parts.scheme not in ("http", "https") or not host:
        raise UnsupportedSchemeError(url, "only absolute http(s) URLs can be fetched")
    if not is_public_host(host):
        raise ForbiddenHostError(url, "refusing a local or private network address")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return f"{parts.scheme}://{parts.netloc.lower()}", host, path
