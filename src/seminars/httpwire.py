"""One request and its answer, to public peers only: the lower half of the live client.

``PoliteClient`` (``seminars.http``) decides when a request may be sent:
robots.txt, the pause between requests, budgets, retries and redirects.
``Wire`` sends it and reads the answer, and it is where the checks of
"Public hosts only" that need the network happen: the lookup before a
request, the connection to an address that lookup cleared
(``seminars.httpconnect``) and the address of the peer before a body is
read. The module docstring of ``seminars.http`` describes them.

Bodies and memory
-----------------

The HTTP library is never asked to decode a body: it would undo every
content coding a site lists, each in one go, and a few hundred bytes of
stacked gzip unpack to gigabytes that way. ``Wire`` reads the body as it
was sent and undoes the coding itself:

* every request says ``Accept-Encoding: gzip, deflate``, and an answer may
  use ONE of the two (``x-gzip`` is gzip, ``identity`` is nothing). Several
  codings, or any other, are refused before the body is touched
  (``UnsupportedContentEncodingError``);
* the decompressor is told with every call how many bytes are still
  allowed, so no call can produce more than that. The bound: the unpacked
  bytes of one body never add up to more than ``max_body_bytes + 1``,
  however little arrived, and no more than ``max_body_bytes`` bytes are
  taken from the connection; one byte more of either ends the reading
  with ``ResponseTooLargeError``. Reading a body thus takes about twice
  ``max_body_bytes`` of memory at most (the pieces and what they are
  joined to, or the output of one decompressor call and its working
  copy), whether it is packed or not;
* a packed body that is damaged, ends too early or goes on after its end
  is a failed attempt like a network error (``AttemptFailed``, which the
  caller retries and reports as ``HttpRequestError``).
"""

from __future__ import annotations

import socket
import time
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Final

import httpx

from seminars.config import HttpSettings
from seminars.httpbase import (
    Clock,
    ForbiddenHostError,
    OwnAddressCheck,
    Resolver,
    ResponseTooLargeError,
    UnsupportedContentEncodingError,
    UnsupportedContentTypeError,
    is_binary_media_type,
    media_type,
)
from seminars.httpconnect import Guard, PinnedTransport, Refused, ip_of
from seminars.httptext import decode_body
from seminars.httpurl import parse_networks


def system_resolver(host: str) -> list[str]:
    """The IP addresses the operating system resolves ``host`` to, each once.

    The default ``Resolver`` of the live client: one ``socket.getaddrinfo``
    call, which also reads /etc/hosts and accepts an IP address in place of
    a name. Raises ``OSError`` when the name does not resolve.
    """
    answers = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(answer[4][0]) for answer in answers))


def system_own_address(address: str) -> bool:
    """True when ``address`` is an address of this machine; nothing is sent to find out.

    The default ``OwnAddressCheck`` of the live client. A UDP socket is
    connected to the address, which only makes the kernel choose a route
    and a source address for it. For an address that sits on one of the
    machine's own interfaces the source is that very address. The public
    address of a host is globally routable like any other, so this is the
    only way to tell it from a site on the internet.
    """
    target = ip_of(address)
    if target is None:
        return False
    family = socket.AF_INET6 if target.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.connect((str(target), 9))
            source = probe.getsockname()[0]
    except OSError:  # no route, or no such address family here: not reachable at all
        return False
    return ip_of(source) == target


_SIZE_DIGITS: Final = 30
"""A declared body size is read this far; a number with more digits is too large anyway."""

ACCEPT_ENCODING: Final = "gzip, deflate"
"""What every request asks for; the only content codings ``Wire`` undoes."""

_GZIP: Final = 16 + zlib.MAX_WBITS
_ZLIB: Final = zlib.MAX_WBITS
_BARE_DEFLATE: Final = -zlib.MAX_WBITS
"""``wbits`` of the three stream formats: RFC 1952, RFC 1950 and RFC 1951."""

_CODINGS: Final = {"gzip": _GZIP, "x-gzip": _GZIP, "deflate": None}
"""Content coding -> stream format; ``None`` when the first bytes of the body decide."""


class AttemptFailed(Exception):
    """One attempt got no answer for a reason that is worth another try."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class TookTooLong(Exception):
    """The answer was not complete when the time for it was over; reading it was given up."""


class _Inflater:
    """Undoes one content coding piece by piece, never producing more than it is asked for.

    ``gzip`` is one gzip member. ``deflate`` is a zlib stream, as the
    standard says, or the bare deflate stream that some servers send in
    its place; the first two bytes of the body tell which. Damaged data
    raises ``AttemptFailed``.
    """

    def __init__(self, coding: str) -> None:
        wbits = _CODINGS[coding]
        self._engine = None if wbits is None else zlib.decompressobj(wbits)
        self._head = b""

    def feed(self, data: bytes, room: int) -> bytes:
        """What ``data`` unpacks to, cut off after ``room`` bytes.

        No call of the decompressor may produce more than is left of
        ``room``. A caller that gets ``room`` bytes has reached its limit
        and stops; input that was still waiting then is never unpacked.
        """
        if self._engine is None:
            self._head += data
            if len(self._head) < 2:
                return b""
            data, self._head = self._head, b""
            is_zlib = (data[0] & 0x0F) == 8 and ((data[0] << 8) | data[1]) % 31 == 0
            self._engine = zlib.decompressobj(_ZLIB if is_zlib else _BARE_DEFLATE)
        parts: list[bytes] = []
        while data and room > 0:
            try:
                part = self._engine.decompress(data, room)
            except zlib.error as error:
                raise AttemptFailed("damaged compressed body") from error
            if self._engine.unused_data:
                raise AttemptFailed("data after the end of the compressed body")
            parts.append(part)
            room -= len(part)
            data = self._engine.unconsumed_tail
        return b"".join(parts)

    def finish(self) -> None:
        """Raise ``AttemptFailed`` unless the packed stream came to its end."""
        if self._engine is None or not self._engine.eof:
            raise AttemptFailed("compressed body ends too early")


def _raw_pieces(response: httpx.Response) -> Iterator[bytes]:
    """The body as it was sent, piece by piece, with its content coding still on.

    An answer that a mock transport built from ready bytes was read by the
    HTTP library when it was made; the bytes as sent are still in its
    stream and are taken from there.
    """
    if not response.is_stream_consumed:
        return response.iter_raw()
    assert isinstance(response.stream, httpx.SyncByteStream)
    return iter(response.stream)


@dataclass(frozen=True)
class Exchange:
    """One HTTP response as received."""

    status: int
    headers: httpx.Headers
    text: str


class Wire:
    """Looks hosts up, sends single requests and reads their answers.

    It knows nothing about politeness: every call is one lookup or one
    request. Without a ``transport`` the requests go through
    ``PinnedTransport``, which connects to the addresses the lookup cleared
    and to nothing else; a transport that is passed in (tests) is used as
    it is. Proxy settings of the environment are ignored; see "Public
    hosts only" in ``seminars.http``. No cookie is ever stored or sent
    back: the jar of the client accepts none, so no answer leaves a trace
    that changes a later request.
    """

    def __init__(
        self,
        settings: HttpSettings,
        *,
        clock: Clock = time.time,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver | None = None,
        own_address: OwnAddressCheck | None = None,
    ) -> None:
        self._settings = settings
        self._guard = Guard(
            resolver=system_resolver if resolver is None else resolver,
            own_address=system_own_address if own_address is None else own_address,
            forbidden=parse_networks(settings.forbidden_networks),
            clock=clock,
        )
        self._client = httpx.Client(
            headers={"User-Agent": settings.user_agent, "Accept-Encoding": ACCEPT_ENCODING},
            timeout=settings.timeout_seconds,
            follow_redirects=False,
            transport=PinnedTransport(self._guard) if transport is None else transport,
            trust_env=False,  # no proxies: the peer of a connection has to be the site
            cookies=CookieJar(DefaultCookiePolicy(allowed_domains=[])),  # accepts nothing
        )
        self._clock = clock

    def close(self) -> None:
        """Release the underlying connections."""
        self._client.close()

    def resolves(self, host: str, url: str) -> bool:
        """Look ``host`` up, the host of ``url`` in canonical form; False when it has no address.

        Raises ``ForbiddenHostError`` when any address it resolves to is not
        public (``is_public_address`` with the forbidden networks of the
        settings) or belongs to this machine. An IP address in place of a
        name goes the same way: the resolver hands it back and it is judged
        like any answer.

        The canonical host (``parse_url``) is the name a connection for
        ``url`` is made to: ASCII, IDNA-encoded, IPv6 without brackets. The
        addresses are kept for ``exchange``: when that has to open a
        connection, it is made to one of them and the name is not looked
        up a second time.
        """
        try:
            return bool(self._guard.lookup(host))
        except Refused as error:
            raise ForbiddenHostError(url, error.reason) from error

    def exchange(self, url: str, headers: Mapping[str, str], *, give_up_at: float) -> Exchange:
        """One request and its answer; ``url`` is in canonical form (``parse_url``).

        Raises ``AttemptFailed`` when no answer came for a reason that is
        worth another try (a network error), ``TookTooLong`` when the
        answer was not complete at ``give_up_at`` (a time of the injected
        clock), and an ``HttpError`` when the answer must not or cannot be
        read.

        The client's own transport gives no socket operation more time than
        is left until ``give_up_at``, so the limit holds from the connection
        to the last byte. With any other transport it is checked when the
        headers are there and after every piece of the body.
        """
        try:
            with (
                self._guard.until(give_up_at),
                self._client.stream("GET", url, headers=dict(headers)) as response,
            ):
                self._check_time()
                self._check_peer(url, response)
                content_type = media_type(response.headers.get("content-type"))
                if is_binary_media_type(content_type):
                    raise UnsupportedContentTypeError(url, content_type)
                body = self._read_body(url, response, self._content_coding(url, response))
                text = decode_body(body, response.headers.get("content-type"))
                return Exchange(response.status_code, response.headers, text)
        except httpx.RequestError as error:
            if self._clock() >= give_up_at:  # the error is how running out of time showed
                raise TookTooLong from error
            raise AttemptFailed(type(error).__name__) from error
        except Refused as error:  # the connection was turned down before anything was sent
            raise ForbiddenHostError(url, error.reason) from error
        finally:
            self._guard.forget()

    def _check_time(self) -> None:
        if self._guard.expired():
            raise TookTooLong

    def _check_peer(self, url: str, response: httpx.Response) -> None:
        """Refuse an answer that did not come from a public address; the body is still unread.

        The address is the one of the connection itself, so it holds
        whatever the lookup before the request said. A connection whose two
        ends have the same address, or whose peer is an address of this
        machine, goes to a service on this machine, however public that
        address is. Without a network connection (mock transports) there
        is nothing to check; with one whose peer cannot be told, the answer
        is refused.

        For the client's own transport this is the second look at a
        connection that ``PinnedBackend`` examined when it opened it.
        """
        stream = response.extensions.get("network_stream")
        if stream is None:
            return
        try:
            place = self._guard.connection_place(stream)
        except OSError as error:  # the connection is gone already
            raise AttemptFailed("connection lost") from error
        if place is not None:
            raise ForbiddenHostError(url, f"refusing an answer from {place}")

    @staticmethod
    def _content_coding(url: str, response: httpx.Response) -> str | None:
        """The one content coding that has to be undone; ``None`` when the body is not packed.

        Raises ``UnsupportedContentEncodingError`` for an answer with
        several codings or with one that is not gzip or deflate. Its body
        has not been touched then.
        """
        values = response.headers.get_list("content-encoding", split_commas=True)
        codings = [value.strip().lower() for value in values]
        codings = [coding for coding in codings if coding not in ("", "identity")]
        if not codings:
            return None
        if len(codings) > 1 or codings[0] not in _CODINGS:
            raise UnsupportedContentEncodingError(url, ", ".join(codings))
        return codings[0]

    def _read_body(self, url: str, response: httpx.Response, coding: str | None) -> bytes:
        """Read the body in pieces and undo ``coding``; stop when it is too large or too late.

        Too large is more than ``max_body_bytes``, as sent or unpacked. The
        module docstring states how much memory this can take.
        """
        limit = self._settings.max_body_bytes
        declared = response.headers.get("content-length", "")
        if declared.isascii() and declared.isdigit() and int(declared[:_SIZE_DIGITS]) > limit:
            raise ResponseTooLargeError(url, limit)
        inflater = None if coding is None else _Inflater(coding)
        chunks: list[bytes] = []
        received = size = 0
        for piece in _raw_pieces(response):
            received += len(piece)
            if received > limit:
                raise ResponseTooLargeError(url, limit)
            chunk = piece if inflater is None else inflater.feed(piece, limit + 1 - size)
            size += len(chunk)
            if size > limit:
                raise ResponseTooLargeError(url, limit)
            self._check_time()
            chunks.append(chunk)
        if inflater is not None and received:
            inflater.finish()
        return b"".join(chunks)
