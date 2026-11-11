"""HTTP building blocks without any I/O: the client protocol, errors and URL helpers.

Kept separate from ``seminars.http`` so that modules which only need the
types (for example ``seminars.fixtures``) do not import the live client.
Everything here is re-exported by ``seminars.http``; import it from there.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Collection, Mapping
from typing import Final, Protocol, runtime_checkable
from urllib.parse import urlsplit

from seminars.models import FetchedDocument

Clock = Callable[[], float]
"""Returns the current time as epoch seconds."""

Sleeper = Callable[[float], None]
"""Blocks for the given number of seconds."""

REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
RETRY_STATUSES: Final = frozenset({408, 425, 429, 500, 502, 503, 504})
BINARY_MEDIA_PREFIXES: Final = ("image/", "audio/", "video/", "font/")
_INTERNAL_SUFFIXES: Final = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
BINARY_MEDIA_TYPES: Final = frozenset(
    {
        "application/gzip",
        "application/octet-stream",
        "application/pdf",
        "application/x-gzip",
        "application/zip",
    }
)


def ascii_safe(text: str) -> str:
    """Escape non-ASCII characters so that a message is safe for logs and reports."""
    return text.encode("ascii", "backslashreplace").decode("ascii")


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
    """robots.txt could not be fetched, so the host is treated as off limits."""


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
        """Fetch ``url`` and return its body as text.

        ``headers`` adds request headers (User-Agent cannot be overridden).
        A final status outside 2xx raises ``HttpStatusError`` unless it is
        listed in ``allow_statuses``.
        """
        ...


def is_public_host(host: str) -> bool:
    """False for localhost, internal names and non-global IP address literals.

    Sources are public web sites. URLs can also come from scraped pages and
    redirects, so anything that addresses this machine or a private network
    is refused instead of fetched.
    """
    name = host.lower().rstrip(".")
    if name == "localhost" or name.endswith(_INTERNAL_SUFFIXES):
        return False
    try:
        return ipaddress.ip_address(name).is_global
    except ValueError:
        return True


def split_url(url: str) -> tuple[str, str, str]:
    """Split a URL into ``(origin, host, path-with-query)``; public http(s) only."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise UnsupportedSchemeError(url, "only absolute http(s) URLs can be fetched")
    if not is_public_host(host):
        raise ForbiddenHostError(url, "refusing a local or private network address")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return f"{parts.scheme}://{parts.netloc.lower()}", host, path
