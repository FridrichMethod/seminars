"""HTTP building blocks without any I/O: the client protocol, its errors and shared types.

Kept separate from ``seminars.http`` so that modules which only need the
types (for example ``seminars.fixtures``) do not import the live client.
The URL rules live in ``seminars.httpurl`` and body decoding in
``seminars.httptext``. What adapters and their tests need of the three
modules is re-exported by ``seminars.http``; import it from there.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Final, Protocol, runtime_checkable

from seminars.models import FetchedDocument
from seminars.textrules import ascii_printable

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

OwnAddressCheck = Callable[[str], bool]
"""Says whether an IP address, given as text, is one of the machine the client runs on.

The live client asks it about every address a host name resolves to and
about the peer of every connection; tests pass a stub.
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


_MEDIA_TYPE: Final = re.compile(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+")
"""A type and a subtype, each a token of the characters media types are made of."""


def media_type(content_type: str | None) -> str | None:
    """``"Text/HTML; charset=utf-8"`` -> ``"text/html"``; ``None`` stays ``None``.

    The header is whatever a site sent, and its value goes into documents,
    fixture manifests and messages. What stands before the first ``;`` is
    therefore only passed on when it is a plain ``type/subtype`` pair of
    tokens. Anything else (control characters, spaces, several values)
    gives ``None``, like a missing header.
    """
    if not content_type:
        return None
    value = content_type.split(";", 1)[0].strip().lower()
    return value if _MEDIA_TYPE.fullmatch(value) else None


def is_binary_media_type(value: str | None) -> bool:
    """True for media types this project never downloads (images, archives, ...)."""
    return value is not None and (
        value.startswith(BINARY_MEDIA_PREFIXES) or value in BINARY_MEDIA_TYPES
    )


class HttpError(Exception):
    """Base class of every error raised by an ``HttpClient``.

    The message is printable ASCII on one line, whatever the URL and the
    answer of a site contained (``seminars.textrules.ascii_printable``), so
    it can be logged and printed as it is. ``url`` is kept as it was given.
    """

    def __init__(self, url: str, message: str) -> None:
        self.url = url
        super().__init__(ascii_printable(f"{message}: {url}"))


class UnsupportedSchemeError(HttpError, ValueError):
    """The text is not a URL this project fetches.

    It is not an absolute http(s) URL, or it is one that is refused as
    such: with a user name or password, with a port or a host that cannot
    be, with control characters, or too long. ``seminars.httpurl.parse_url``
    has the list.
    """


class ForbiddenHostError(HttpError, ValueError):
    """The URL leads to this machine or a private network, never to a public site.

    Raised for a host that is written as a local or private address, for a
    name that resolves to one, and for an answer that came from one. An
    address of the machine the client runs on and the addresses of
    ``http.forbidden_networks`` count as such, although they may be
    globally routable.
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
    """No usable answer: a timeout or network failure that persisted through all retries.

    Also raised, without retries, for an answer that was not complete
    within ``max_exchange_seconds``.
    """


class DeadlineExceededError(HttpRequestError):
    """One ``get`` used up ``max_get_seconds`` and was given up.

    The time went into waiting for the host (crawl delay, backoff, other
    processes) or into slow answers along the way. Nothing is held against
    the host: the next ``get`` starts with its full time.
    """


class TooManyRedirectsError(HttpError):
    """More than ``max_redirects`` redirects in a row."""


class UnsupportedContentTypeError(HttpError):
    """The response is an image or another binary type; its body was not read."""

    def __init__(self, url: str, content_type: str | None) -> None:
        self.content_type = content_type
        super().__init__(url, f"refusing binary content type {content_type}")


class UnsupportedContentEncodingError(HttpError):
    """The body is packed in a way the client does not undo; it was not read.

    Every request asks for ``gzip`` or ``deflate``, and one layer of either
    is all that is unpacked. An answer that lists several content codings,
    or another one, is refused.
    """

    def __init__(self, url: str, content_encoding: str) -> None:
        super().__init__(url, f"refusing content encoding {content_encoding[:60]}")


class ResponseTooLargeError(HttpError):
    """The body is larger than ``max_body_bytes``; reading it was given up.

    The limit holds for the body as it was sent and for the body after
    ``gzip`` or ``deflate`` was undone.
    """

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

        ``headers`` adds request headers (User-Agent and Accept-Encoding
        cannot be overridden). They are meant for the origin of ``url``:
        when a redirect leads to another origin, only ``Accept`` and
        ``Accept-Language`` are sent there, so a credential passed here
        stays with the site it is for.
        Cookies are never kept between requests; a ``Cookie`` header given
        here is sent like any other header. A final status outside 2xx
        raises ``HttpStatusError`` unless it is listed in ``allow_statuses``.
        """
        ...
