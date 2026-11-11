"""One request and its answer, to public peers only: the lower half of the live client.

``PoliteClient`` (``seminars.http``) decides when a request may be sent:
robots.txt, the pause between requests, budgets, retries and redirects.
``Wire`` sends it and reads the answer, and it is where the two checks of
"Public hosts only" that need the network happen: the lookup before a
request and the address of the peer before a body is read. The module
docstring of ``seminars.http`` describes them.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx

from seminars.config import HttpSettings
from seminars.httpbase import (
    ForbiddenHostError,
    OwnAddressCheck,
    Resolver,
    ResponseTooLargeError,
    UnsupportedContentTypeError,
    UnsupportedSchemeError,
    is_binary_media_type,
    media_type,
)
from seminars.httptext import decode_body
from seminars.httpurl import is_public_address, parse_networks


def system_resolver(host: str) -> list[str]:
    """The IP addresses the operating system resolves ``host`` to, each once.

    The default ``Resolver`` of the live client: one ``socket.getaddrinfo``
    call, which also reads /etc/hosts and accepts an IP address in place of
    a name. Raises ``OSError`` when the name does not resolve.
    """
    answers = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(answer[4][0]) for answer in answers))


def _ip(address: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``address`` as an IP address without its zone id; ``None`` when it is not one."""
    if not isinstance(address, str):
        return None
    try:
        return ipaddress.ip_address(address.partition("%")[0])
    except ValueError:
        return None


def system_own_address(address: str) -> bool:
    """True when ``address`` is an address of this machine; nothing is sent to find out.

    The default ``OwnAddressCheck`` of the live client. A UDP socket is
    connected to the address, which only makes the kernel choose a route
    and a source address for it. For an address that sits on one of the
    machine's own interfaces the source is that very address. The public
    address of a host is globally routable like any other, so this is the
    only way to tell it from a site on the internet.
    """
    target = _ip(address)
    if target is None:
        return False
    family = socket.AF_INET6 if target.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.connect((str(target), 9))
            source = probe.getsockname()[0]
    except OSError:  # no route, or no such address family here: not reachable at all
        return False
    return _ip(source) == target


def _end(address: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The IP address of one end of a connection, as ``getpeername`` and ``getsockname`` give it."""
    return _ip(address[0]) if isinstance(address, tuple) and address else None


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
        own_address: OwnAddressCheck | None = None,
    ) -> None:
        self._settings = settings
        self._resolver = system_resolver if resolver is None else resolver
        self._own_address = system_own_address if own_address is None else own_address
        self._forbidden = parse_networks(settings.forbidden_networks)
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
        public (``is_public_address`` with the forbidden networks of the
        settings) or belongs to this machine. The name is taken from httpx,
        so it is exactly the one the connection will be made to
        (IDNA-encoded, IPv6 without brackets); a URL that httpx cannot parse
        is rejected here. An IP address in place of a name goes the same
        way: the resolver hands it back and it is judged like any answer.
        """
        try:
            name = httpx.URL(url).raw_host.decode("ascii")
        except httpx.InvalidURL as error:
            raise UnsupportedSchemeError(url, "invalid URL") from error
        try:
            addresses = list(self._resolver(name))
        except (OSError, UnicodeError):  # no such name, or not a name the resolver accepts
            return False
        for address in addresses:
            if not is_public_address(address, self._forbidden):
                raise ForbiddenHostError(
                    url, "refusing a host name that resolves to a local or private network address"
                )
            if self._own_address(address):
                raise ForbiddenHostError(
                    url, "refusing a host name that resolves to an address of this machine"
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

    def _check_peer(self, url: str, response: httpx.Response) -> None:
        """Refuse an answer that did not come from a public address; the body is still unread.

        The address is the one of the connection itself, so it holds
        whatever the lookup before the request said. A connection whose two
        ends have the same address, or whose peer is an address of this
        machine, goes to a service on this machine, however public that
        address is. Without a network connection (mock transports) there
        is nothing to check; with one whose peer cannot be told, the answer
        is refused.
        """
        stream = response.extensions.get("network_stream")
        if stream is None:
            return
        try:
            peer = _end(stream.get_extra_info("server_addr"))
            local = _end(stream.get_extra_info("client_addr"))
        except OSError as error:  # the connection is gone already
            raise AttemptFailed("connection lost") from error
        if peer is None or not is_public_address(str(peer), self._forbidden):
            raise ForbiddenHostError(
                url, "refusing an answer from a local or private network address"
            )
        if peer == local or self._own_address(str(peer)):
            raise ForbiddenHostError(url, "refusing an answer from this machine")

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
