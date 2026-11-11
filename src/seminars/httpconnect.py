"""Connections of the live client: opened by the client itself, to vetted addresses only.

An HTTP library that is given a host name looks the name up and connects to
whatever comes back. That is a second lookup after the one that was
checked, and the two can disagree (DNS rebinding). The live client does not
leave the connection to the library:

* ``Guard`` holds the rules. It looks a host up, refuses the host when any
  address of the answer is not a public one (``is_public_address`` with the
  forbidden networks of the settings) or belongs to this machine, and
  judges both ends of a connection.
* ``PinnedBackend`` is the network backend of the connection pool. It opens
  every connection to an address the guard has just cleared, by address and
  not by name, so there is no second lookup, and it hands a connection on
  only after the guard has seen its two ends. Nothing has been sent at that
  point. The name is still what TLS verifies the certificate against: the
  pool takes it from the URL.
* ``PinnedTransport`` is the transport ``Wire`` gives to httpx. It is the
  plain connection pool of httpcore with that backend; proxies and unix
  sockets do not exist in it.

Every connection also carries the time limit of the request that uses it
(``Guard.until``): connecting, the TLS handshake and each read and write
get no more time than is left of it. This is what bounds the time until
the headers of an answer have arrived, which httpx gives no chance to
check from outside.

Whoever replaces the transport (the mock transports of the tests) replaces
all of this; the lookup before a request and the peer check of ``Wire``
still apply to it.
"""

from __future__ import annotations

import contextlib
import ipaddress
import ssl
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from typing import Any, Final

import httpcore
import httpx

from seminars.httpbase import Clock, OwnAddressCheck, Resolver
from seminars.httpurl import Network, is_public_address

_MAX_CONNECTIONS: Final = 100
_MAX_KEEPALIVE_CONNECTIONS: Final = 20
_KEEPALIVE_SECONDS: Final = 5.0
"""Pool limits; the same numbers httpx uses for its own transport."""

PRIVATE_PLACE: Final = "a local or private network address"
THIS_MACHINE: Final = "this machine"
"""The two places nothing is fetched from, as they appear in error messages."""

_HTTPCORE_ERRORS: Final = (
    httpcore.TimeoutException,
    httpcore.NetworkError,
    httpcore.ProtocolError,
    httpcore.ProxyError,
    httpcore.UnsupportedProtocol,
)

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def ip_of(address: object) -> IpAddress | None:
    """``address`` as an IP address without its zone id; ``None`` when it is not one."""
    if not isinstance(address, str):
        return None
    try:
        return ipaddress.ip_address(address.partition("%")[0])
    except ValueError:
        return None


def _end(address: object) -> IpAddress | None:
    """The IP address of one end of a connection, as ``getpeername`` and ``getsockname`` give it."""
    return ip_of(address[0]) if isinstance(address, tuple) and address else None


class Refused(Exception):
    """A host or a connection is turned down because of where it leads."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class Guard:
    """Where the live client may connect to, and until when; shared by ``Wire`` and its transport.

    Safe to use from several threads: what one request hands from its
    lookup to its connection, and the time it has, are kept per thread.
    """

    def __init__(
        self,
        *,
        resolver: Resolver,
        own_address: OwnAddressCheck,
        forbidden: Sequence[Network],
        clock: Clock = time.time,
    ) -> None:
        self._resolver = resolver
        self._own_address = own_address
        self._forbidden = tuple(forbidden)
        self._clock = clock
        self._request = threading.local()

    @contextlib.contextmanager
    def until(self, give_up_at: float) -> Iterator[None]:
        """Within this block, the request of this thread has time until ``give_up_at``."""
        self._request.give_up_at = give_up_at
        try:
            yield
        finally:
            self._request.give_up_at = None

    def expired(self) -> bool:
        """Whether the time of the request this thread is making is over."""
        give_up_at = getattr(self._request, "give_up_at", None)
        return give_up_at is not None and self._clock() >= give_up_at

    def time_left(self, timeout: float | None, late: type[Exception]) -> float | None:
        """The timeout for one socket operation: ``timeout``, but no more than what is left.

        Raises ``late`` when nothing is left. Outside ``until`` the
        timeout is passed through.
        """
        give_up_at = getattr(self._request, "give_up_at", None)
        if give_up_at is None:
            return timeout
        left = give_up_at - self._clock()
        if left <= 0:
            raise late("the time allowed for one request and its answer is over")
        return left if timeout is None else min(timeout, left)

    def lookup(self, host: str) -> list[str]:
        """The addresses of ``host``, every one of them cleared; empty when it has none.

        Raises ``Refused`` when any address is not public or is an address
        of this machine. An IP address in place of a name goes the same
        way: the resolver hands it back and it is judged like any answer.
        The answer is kept for the connection this thread opens next
        (``addresses_to_connect``), until ``forget`` is called.
        """
        try:
            addresses = list(self._resolver(host))
        except (OSError, UnicodeError):  # no such name, or not a name the resolver accepts
            return []
        for address in addresses:
            place = self._place(ip_of(address))
            if place is not None:
                raise Refused(f"refusing a host name that resolves to {_an(place)}")
        self._request.cleared = (host, addresses)
        return addresses

    def forget(self) -> None:
        """Drop the lookup this thread has kept for its next connection."""
        self._request.cleared = None

    def addresses_to_connect(self, host: str) -> list[str]:
        """The cleared addresses of ``host`` for a connection that is about to be opened.

        They are those of the lookup this thread made for the request in
        progress. Should a connection be asked for without one, the host is
        looked up and judged here; an unchecked address is never returned.
        """
        cleared = getattr(self._request, "cleared", None)
        if cleared is not None and cleared[0] == host:
            return cleared[1]
        return self.lookup(host)

    def connection_place(self, stream: object) -> str | None:
        """Where a connection must not lead although it does; ``None`` when it may be used.

        ``stream`` is the network stream of httpcore (anything with its
        ``get_extra_info``). The peer must be a public address, must not be
        an address of this machine, and the two ends of the connection must
        differ: a connection from an address to itself stays on this
        machine, however public that address is. A peer that cannot be told
        is refused. May raise ``OSError`` when the connection is gone.
        """
        get_extra_info = getattr(stream, "get_extra_info", None)
        if get_extra_info is None:
            return PRIVATE_PLACE
        peer, local = _end(get_extra_info("server_addr")), _end(get_extra_info("client_addr"))
        place = self._place(peer)
        return THIS_MACHINE if place is None and peer == local else place

    def _place(self, address: IpAddress | None) -> str | None:
        """Why nothing is fetched from ``address``; ``None`` for a public address elsewhere."""
        if address is None or not is_public_address(str(address), self._forbidden):
            return PRIVATE_PLACE
        return THIS_MACHINE if self._own_address(str(address)) else None


def _an(place: str) -> str:
    """``place`` as it reads after "resolves to"."""
    return "an address of this machine" if place == THIS_MACHINE else place


class _TimedStream(httpcore.NetworkStream):
    """A connection whose every operation ends when the time of its request does."""

    def __init__(self, stream: httpcore.NetworkStream, guard: Guard) -> None:
        self._stream = stream
        self._guard = guard

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._stream.read(max_bytes, self._guard.time_left(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(buffer, self._guard.time_left(timeout, httpcore.WriteTimeout))

    def close(self) -> None:
        self._stream.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        left = self._guard.time_left(timeout, httpcore.ConnectTimeout)
        return _TimedStream(self._stream.start_tls(ssl_context, server_hostname, left), self._guard)

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


class PinnedBackend(httpcore.NetworkBackend):
    """Opens the connections of the pool: to cleared addresses, and never to this machine.

    ``inner`` makes the actual connections (default: the socket backend of
    httpcore). It is always given an IP address, never a name.
    """

    def __init__(self, guard: Guard, inner: httpcore.NetworkBackend | None = None) -> None:
        self._guard = guard
        self._inner = httpcore.SyncBackend() if inner is None else inner

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        """A connection to one of the cleared addresses of ``host``, tried in order.

        Raises ``Refused`` when the host or the connection that came about
        must not be used; the connection is closed first and nothing was
        sent over it. A host without an address, and addresses that cannot
        be reached, are connection errors like in any backend.
        """
        failure: Exception = httpcore.ConnectError(f"{host} has no address")
        for address in self._guard.addresses_to_connect(host):
            try:
                stream = self._inner.connect_tcp(
                    address,
                    port,
                    timeout=self._guard.time_left(timeout, httpcore.ConnectTimeout),
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                failure = error
                continue
            try:
                place = self._guard.connection_place(stream)
            except OSError as error:
                stream.close()
                failure = httpcore.ConnectError(f"connection lost: {error}")
                continue
            if place is not None:
                stream.close()
                raise Refused(f"refusing a connection to {place}")
            return _TimedStream(stream, self._guard)
        raise failure

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        raise httpcore.ConnectError("unix sockets are not used")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


@contextlib.contextmanager
def _as_httpx_errors() -> Iterator[None]:
    """Turn the errors of httpcore into the errors of httpx with the same name."""
    try:
        yield
    except _HTTPCORE_ERRORS as error:
        kind = getattr(httpx, type(error).__name__, httpx.TransportError)
        if not (isinstance(kind, type) and issubclass(kind, httpx.TransportError)):
            kind = httpx.TransportError
        raise kind(str(error)) from error


class _Body(httpx.SyncByteStream):
    """The body of an answer of the pool, as httpx wants it."""

    def __init__(self, parts: Iterable[bytes]) -> None:
        self._parts = parts

    def __iter__(self) -> Iterator[bytes]:
        with _as_httpx_errors():
            yield from self._parts

    def close(self) -> None:
        close = getattr(self._parts, "close", None)
        if close is not None:
            close()


class PinnedTransport(httpx.BaseTransport):
    """The transport of the live client: a connection pool that connects through ``PinnedBackend``.

    HTTP/1.1 over TCP and TLS with the certificate checks of httpx (the
    certificate store of certifi; nothing is taken from the environment).
    ``inner`` replaces the socket backend in tests.
    """

    def __init__(self, guard: Guard, *, inner: httpcore.NetworkBackend | None = None) -> None:
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=_MAX_CONNECTIONS,
            max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=_KEEPALIVE_SECONDS,
            network_backend=PinnedBackend(guard, inner),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.SyncByteStream)
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _as_httpx_errors():
            answer = self._pool.handle_request(core_request)
        assert isinstance(answer.stream, Iterable)
        return httpx.Response(
            status_code=answer.status,
            headers=answer.headers,
            stream=_Body(answer.stream),
            extensions=answer.extensions,
        )

    def close(self) -> None:
        self._pool.close()
