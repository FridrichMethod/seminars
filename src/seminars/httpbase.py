"""HTTP building blocks without any I/O: the client protocol, its errors and shared types.

Kept separate from ``seminars.http`` so that modules which only need the
types (for example ``seminars.fixtures``) do not import the live client.
The URL rules live in ``seminars.httpurl`` and body decoding in
``seminars.httptext``. Everything in the three modules is re-exported by
``seminars.http``; import it from there.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Final, Protocol, runtime_checkable

from seminars.models import FetchedDocument
from seminars.textrules import ascii_safe

Clock = Callable[[], float]
"""Returns the current time as epoch seconds."""

Sleeper = Callable[[float], None]
"""Blocks for the given number of seconds."""

Resolver = Callable[[str], Sequence[str]]
"""Looks a host name up and returns its IP addresses as text.

Raises ``OSError`` when the name has no address (``socket.gaierror`` is one).
The live client asks it before every request; tests pass a stub, so they
never touch DNS.
"""

REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
RETRY_STATUSES: Final = frozenset({408, 425, 429, 500, 502, 503, 504})
BINARY_MEDIA_PREFIXES: Final = ("image/", "audio/", "video/", "font/")
BINARY_MEDIA_TYPES: Final = frozenset(
    {
        "application/gzip",
        "application/octet-stream",
        "application/pdf",
        "application/x-gzip",
        "application/zip",
    }
)


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
    """The URL leads to this machine or a private network, never to a public site.

    Raised for a host that is written as a local or private address, for a
    name that resolves to one, and for an answer that came from one.
    """


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
        They are meant for the origin of ``url``: when a redirect leads to
        another origin, only ``Accept`` and ``Accept-Language`` are sent
        there, so a credential passed here stays with the site it is for.
        Cookies are never kept between requests; a ``Cookie`` header given
        here is sent like any other header. A final status outside 2xx
        raises ``HttpStatusError`` unless it is listed in ``allow_statuses``.
        """
        ...
