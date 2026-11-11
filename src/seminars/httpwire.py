"""One request and its answer, to public peers only: the lower half of the live client.

``PoliteClient`` (``seminars.http``) decides when a request may be sent:
robots.txt, the pause between requests, budgets, retries and redirects.
``Wire`` sends it and reads the answer, and it is where the two checks of
"Public hosts only" that need the network happen: the lookup before a
request and the address of the peer before a body is read. The module
docstring of ``seminars.http`` describes them.
"""

from __future__ import annotations

import socket
from collections.abc import Mapping
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx

from seminars.config import HttpSettings
from seminars.httpbase import (
    ForbiddenHostError,
    Resolver,
    ResponseTooLargeError,
    UnsupportedContentTypeError,
    UnsupportedSchemeError,
    is_binary_media_type,
    media_type,
)
from seminars.httptext import decode_body
from seminars.httpurl import is_public_address


def system_resolver(host: str) -> list[str]:
    """The IP addresses the operating system resolves ``host`` to, each once.

    The default ``Resolver`` of the live client: one ``socket.getaddrinfo``
    call, which also reads /etc/hosts and accepts an IP address in place of
    a name. Raises ``OSError`` when the name does not resolve.
    """
    answers = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(answer[4][0]) for answer in answers))


class AttemptFailed(Exception):
    """One attempt got no answer for a reason that is worth another try."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Exchange:
    """One HTTP response as received."""

    status: int
    headers: httpx.Headers
    text: str


class Wire:
    """Looks hosts up, sends single requests and reads their answers.

    It knows nothing about politeness: every call is one lookup or one
    request. Proxy settings of the environment are ignored; see "Public
    hosts only" in ``seminars.http``. No cookie is ever stored or sent
    back: the jar of the client accepts none, so no answer leaves a trace
    that changes a later request.
    """

    def __init__(
        self,
        settings: HttpSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._settings = settings
        self._resolver = system_resolver if resolver is None else resolver
        self._client = httpx.Client(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.timeout_seconds,
            follow_redirects=False,
            transport=transport,
            trust_env=False,  # no proxies: the peer check needs the real peer
            cookies=CookieJar(DefaultCookiePolicy(allowed_domains=[])),  # accepts nothing
        )

    def close(self) -> None:
        """Release the underlying connections."""
        self._client.close()

    def resolves(self, url: str) -> bool:
        """Look up the host of ``url``; False when it has no address.

        Raises ``ForbiddenHostError`` when any address it resolves to is not
        public. The name is taken from httpx, so it is exactly the one the
        connection will be made to (IDNA-encoded, IPv6 without brackets); a
        URL that httpx cannot parse is rejected here.
        """
        try:
            name = httpx.URL(url).raw_host.decode("ascii")
        except httpx.InvalidURL as error:
            raise UnsupportedSchemeError(url, "invalid URL") from error
        try:
            addresses = list(self._resolver(name))
        except (OSError, UnicodeError):  # no such name, or not a name the resolver accepts
            return False
        if not all(is_public_address(address) for address in addresses):
            raise ForbiddenHostError(
                url, "refusing a host name that resolves to a local or private network address"
            )
        return bool(addresses)

    def exchange(self, url: str, headers: Mapping[str, str]) -> Exchange:
        """One request and its answer; ``resolves`` has made sure that httpx accepts ``url``.

        Raises ``AttemptFailed`` when no answer came for a reason that is
        worth another try (a network error), and an ``HttpError`` when the
        answer must not or cannot be read.
        """
        try:
            with self._client.stream("GET", url, headers=dict(headers)) as response:
                self._check_peer(url, response)
                content_type = media_type(response.headers.get("content-type"))
                if is_binary_media_type(content_type):
                    raise UnsupportedContentTypeError(url, content_type)
                body = self._read_body(url, response)
                text = decode_body(body, response.headers.get("content-type"))
                return Exchange(response.status_code, response.headers, text)
        except httpx.RequestError as error:
            raise AttemptFailed(type(error).__name__) from error

    @staticmethod
    def _check_peer(url: str, response: httpx.Response) -> None:
        """Refuse an answer that did not come from a public address; the body is still unread.

        The address is the one of the connection itself, so it holds
        whatever the lookup before the request said. Without a network
        connection (mock transports) there is nothing to check; with one
        whose peer cannot be told, the answer is refused.
        """
        stream = response.extensions.get("network_stream")
        if stream is None:
            return
        try:
            peer = stream.get_extra_info("server_addr")
        except OSError as error:  # the connection is gone already
            raise AttemptFailed("connection lost") from error
        address = peer[0] if isinstance(peer, tuple) and peer else None
        if not isinstance(address, str) or not is_public_address(address):
            raise ForbiddenHostError(
                url, "refusing an answer from a local or private network address"
            )

    def _read_body(self, url: str, response: httpx.Response) -> bytes:
        """Read the body in pieces and stop as soon as it is larger than ``max_body_bytes``."""
        limit = self._settings.max_body_bytes
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise ResponseTooLargeError(url, limit)
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > limit:
                raise ResponseTooLargeError(url, limit)
            chunks.append(chunk)
        return b"".join(chunks)
