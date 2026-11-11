"""Tests for the connections of the live client: made to cleared addresses, by address.

Most tests run the real connection pool of httpcore over a fake socket
layer (``FakeSockets``), so they need no network and are never skipped. One
class talks to a server on the loopback interface through real sockets.
"""

from __future__ import annotations

import functools
import http.server
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest

from fakes import PUBLIC_ADDRESS, FakeClock, StubResolver
from seminars.config import HttpSettings
from seminars.http import (
    ForbiddenHostError,
    HttpRequestError,
    PoliteClient,
    RobotsUnavailableError,
)
from seminars.httpconnect import Guard, PinnedBackend, PinnedTransport, Refused

NAME = "dept.example.edu"
PAGE = f"https://{NAME}/events"
OTHER_ADDRESS = "93.184.216.35"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"
LOCAL_END = ("192.0.2.10", 50000)


def answer(status: int = 200, body: bytes = b"ok", headers: str = "") -> bytes:
    reason = {200: "OK", 404: "Not Found"}.get(status, "Status")
    head = f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\n{headers}\r\n"
    return head.encode("ascii") + body


class FakeStream(httpcore.NetworkStream):
    """One connection of ``FakeSockets``: answers every request from a script."""

    def __init__(self, sockets: FakeSockets, address: str, port: int) -> None:
        self._sockets = sockets
        self.address, self.port = address, port
        self.written = b""
        self.closed = False
        self.tls_name: str | None = None
        self._pending: list[bytes | Exception] = []

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self._sockets.read_timeouts.append(timeout)
        if not self._pending:
            return b""
        part = self._pending.pop(0)
        if isinstance(part, Exception):
            raise part
        return part

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.written += buffer
        if buffer.endswith(b"\r\n\r\n"):  # the end of a request without a body
            path = buffer.split(b" ", 2)[1].decode("ascii")
            self._sockets.requests.append((self.address, path))
            self._pending.extend(self._sockets.respond(path))

    def close(self) -> None:
        self.closed = True

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        self.tls_name = server_hostname
        return self

    def get_extra_info(self, info: str) -> Any:
        if info == "server_addr":
            return (self._sockets.peers.get(self.address, self.address), self.port)
        if info == "client_addr":
            return self._sockets.local_end
        return None  # no ssl_object: HTTP/1.1; not readable: the server did not hang up


class FakeSockets(httpcore.NetworkBackend):
    """A socket layer without sockets; ``connects`` lists what was connected to."""

    def __init__(self) -> None:
        self.connects: list[tuple[str, int]] = []
        self.streams: list[FakeStream] = []
        self.requests: list[tuple[str, str]] = []
        self.read_timeouts: list[float | None] = []
        self.unreachable: set[str] = set()
        self.peers: dict[str, str] = {}  # address asked for -> address the connection ends at
        self.local_end: object = LOCAL_END
        self.pages: dict[str, list[bytes | Exception]] = {}
        self.stream_class: type[FakeStream] = FakeStream
        self.connect_error: Exception | None = None

    def respond(self, path: str) -> list[bytes | Exception]:
        if path in self.pages:
            return list(self.pages[path])
        return [answer(404, b"")] if path == "/robots.txt" else [answer(200, b"ok")]

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        self.connects.append((host, port))
        if self.connect_error is not None:
            raise self.connect_error
        if host in self.unreachable:
            raise httpcore.ConnectError(f"cannot reach {host}")
        stream = self.stream_class(self, host, port)
        self.streams.append(stream)
        return stream


@pytest.fixture
def sockets(monkeypatch: pytest.MonkeyPatch) -> FakeSockets:
    """Replaces the socket backend that a ``PoliteClient`` without a transport would use."""
    fake = FakeSockets()
    monkeypatch.setattr(httpcore, "SyncBackend", lambda: fake)
    return fake


@pytest.fixture
def client(
    http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver, sockets: FakeSockets
) -> Iterator[PoliteClient]:
    with PoliteClient(
        http_settings, clock=fake_clock, sleep=fake_clock.sleep, resolver=resolver
    ) as polite:
        yield polite


class TestPinnedConnections:
    def test_the_connection_is_made_to_the_address_the_lookup_cleared(
        self, client: PoliteClient, sockets: FakeSockets, resolver: StubResolver
    ) -> None:
        assert client.get(PAGE).text == "ok\n"
        assert sockets.connects == [(PUBLIC_ADDRESS, 443)]
        assert sockets.requests == [(PUBLIC_ADDRESS, "/robots.txt"), (PUBLIC_ADDRESS, "/events")]
        assert resolver.calls == [NAME, NAME]  # once per request, never again for connecting

    def test_tls_still_checks_the_certificate_against_the_name(
        self, client: PoliteClient, sockets: FakeSockets
    ) -> None:
        client.get(PAGE)
        assert [stream.tls_name for stream in sockets.streams] == [NAME]
        assert b"\r\nHost: dept.example.edu\r\n" in sockets.streams[0].written

    def test_plain_http_and_other_ports(self, client: PoliteClient, sockets: FakeSockets) -> None:
        client.get(f"http://{NAME}/events")
        client.get(f"http://{NAME}:8080/events")
        assert sockets.connects == [(PUBLIC_ADDRESS, 80), (PUBLIC_ADDRESS, 8080)]
        assert [stream.tls_name for stream in sockets.streams] == [None, None]

    def test_a_host_written_as_an_address(
        self, client: PoliteClient, sockets: FakeSockets, resolver: StubResolver
    ) -> None:
        resolver.answers[PUBLIC_V6] = [PUBLIC_V6]
        client.get(f"http://[{PUBLIC_V6.upper()}]:8080/status")
        assert sockets.connects == [(PUBLIC_V6, 8080)]
        assert resolver.calls == [PUBLIC_V6, PUBLIC_V6]

    def test_a_name_with_other_letters_is_looked_up_once_under_its_ascii_form(
        self, client: PoliteClient, sockets: FakeSockets, resolver: StubResolver
    ) -> None:
        """The name of the lookup and the name the pool connects for must be the same string."""
        client.get(f"https://B{chr(0xFC)}cher.Example./katalog")
        assert resolver.calls == ["xn--bcher-kva.example"] * 2
        assert sockets.connects == [(PUBLIC_ADDRESS, 443)]
        assert sockets.streams[0].tls_name == "xn--bcher-kva.example"

    def test_a_lookup_that_changes_its_mind_cannot_steer_a_connection_inside(
        self, client: PoliteClient, sockets: FakeSockets, resolver: StubResolver
    ) -> None:
        """DNS rebinding: public for the check, private for the connection. There is no second."""
        answers = [[PUBLIC_ADDRESS], ["10.0.0.5"], [OTHER_ADDRESS], ["127.0.0.1"], [PUBLIC_ADDRESS]]
        resolver.script(NAME, *answers)
        outcomes = []
        for number in range(5):
            try:
                client.get(f"{PAGE}?page={number}")
                outcomes.append("ok")
            except ForbiddenHostError:
                outcomes.append("refused")
        assert outcomes == ["refused", "ok", "refused", "ok", "ok"]
        assert {address for address, _ in sockets.connects} <= {PUBLIC_ADDRESS, OTHER_ADDRESS}
        assert {address for address, _ in sockets.requests} <= {PUBLIC_ADDRESS, OTHER_ADDRESS}

    def test_a_name_that_resolves_to_a_private_address_is_never_connected_to(
        self, client: PoliteClient, sockets: FakeSockets, resolver: StubResolver
    ) -> None:
        resolver.answers[NAME] = ["192.168.1.10"]
        with pytest.raises(ForbiddenHostError, match="resolves to a local or private network"):
            client.get(PAGE)
        assert sockets.connects == []

    def test_the_addresses_are_tried_in_order(
        self, client: PoliteClient, sockets: FakeSockets, resolver: StubResolver
    ) -> None:
        resolver.answers[NAME] = [PUBLIC_V6, OTHER_ADDRESS, PUBLIC_ADDRESS]
        sockets.unreachable = {PUBLIC_V6}
        assert client.get(PAGE).text == "ok\n"
        assert sockets.connects == [(PUBLIC_V6, 443), (OTHER_ADDRESS, 443)]
        assert sockets.requests[-1] == (OTHER_ADDRESS, "/events")

    def test_no_reachable_address_is_a_failed_attempt_like_any_network_error(
        self, client: PoliteClient, sockets: FakeSockets, fake_clock: FakeClock
    ) -> None:
        sockets.unreachable = {PUBLIC_ADDRESS}
        with pytest.raises(RobotsUnavailableError, match="unreachable") as caught:
            client.get(f"http://{NAME}/events")
        assert "ConnectError" in str(caught.value.__cause__)
        assert sockets.connects == [(PUBLIC_ADDRESS, 80)] * 4
        assert fake_clock.sleeps == [1.0, 2.0, 4.0]

    def test_a_connection_is_kept_and_used_again(
        self, client: PoliteClient, sockets: FakeSockets, resolver: StubResolver
    ) -> None:
        for number in range(3):
            client.get(f"{PAGE}?page={number}")
        assert sockets.connects == [(PUBLIC_ADDRESS, 443)]
        assert len(sockets.requests) == 4
        assert resolver.calls == [NAME] * 4  # the lookup is checked before every request

    def test_a_kept_connection_is_not_used_once_the_name_leads_elsewhere(
        self, client: PoliteClient, sockets: FakeSockets, resolver: StubResolver
    ) -> None:
        client.get(PAGE)
        resolver.answers[NAME] = ["10.0.0.5"]
        with pytest.raises(ForbiddenHostError):
            client.get(f"{PAGE}?again")
        assert len(sockets.requests) == 2


class TestConnectionsThatMustNotBeUsed:
    """The second look: where a connection really ends, before anything is sent over it."""

    @pytest.mark.parametrize(
        ("ends_at", "place"),
        [
            ("10.0.0.5", "a local or private network address"),
            ("127.0.0.1", "a local or private network address"),
            ("169.254.169.254", "a local or private network address"),
            ("168.63.129.16", "a local or private network address"),
            (LOCAL_END[0], "a local or private network address"),
        ],
    )
    def test_a_connection_that_ends_somewhere_else_is_closed_unused(
        self, client: PoliteClient, sockets: FakeSockets, ends_at: str, place: str
    ) -> None:
        sockets.peers[PUBLIC_ADDRESS] = ends_at
        with pytest.raises(ForbiddenHostError, match=f"refusing a connection to {place}"):
            client.get(PAGE)
        (stream,) = sockets.streams
        assert (stream.closed, stream.written, stream.tls_name) == (True, b"", None)
        assert sockets.requests == []
        with pytest.raises(ForbiddenHostError, match="earlier in this run"):
            client.get(PAGE)
        assert len(sockets.connects) == 1

    def test_a_connection_from_an_address_to_itself_is_this_machine(
        self, client: PoliteClient, sockets: FakeSockets
    ) -> None:
        sockets.local_end = (PUBLIC_ADDRESS, 51000)
        with pytest.raises(ForbiddenHostError, match="refusing a connection to this machine"):
            client.get(PAGE)
        assert sockets.streams[0].written == b""
        assert sockets.requests == []

    def test_a_peer_that_is_an_address_of_this_machine(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        resolver: StubResolver,
        sockets: FakeSockets,
    ) -> None:
        asked: list[str] = []

        def own(address: str) -> bool:
            asked.append(address)
            return len(asked) > 1  # "not mine" at the lookup, "mine" at the connection

        with (
            PoliteClient(
                http_settings,
                clock=fake_clock,
                sleep=fake_clock.sleep,
                resolver=resolver,
                own_address=own,
            ) as polite,
            pytest.raises(ForbiddenHostError, match="a connection to this machine"),
        ):
            polite.get(PAGE)
        assert sockets.requests == []

    @pytest.mark.parametrize("peer", [None, (), "unix", (None, 443), ("not an address", 443)])
    def test_a_connection_whose_peer_cannot_be_told_is_not_used(
        self, client: PoliteClient, sockets: FakeSockets, peer: object
    ) -> None:
        class Blind(FakeStream):
            def get_extra_info(self, info: str) -> Any:
                return peer if info == "server_addr" else LOCAL_END

        sockets.stream_class = Blind
        with pytest.raises(ForbiddenHostError, match="refusing a connection to"):
            client.get(PAGE)
        assert sockets.streams[0].closed
        assert sockets.requests == []

    def test_a_connection_that_dies_before_it_can_be_examined_is_a_failed_attempt(
        self, client: PoliteClient, sockets: FakeSockets
    ) -> None:
        class Dead(FakeStream):
            def get_extra_info(self, info: str) -> Any:
                raise OSError(107, "Transport endpoint is not connected")

        sockets.stream_class = Dead
        with pytest.raises(RobotsUnavailableError, match="unreachable") as caught:
            client.get(PAGE)
        assert "ConnectError" in str(caught.value.__cause__)
        assert all(stream.closed for stream in sockets.streams)
        assert len(sockets.connects) == 4


class TestTheSecondLook:
    """The peer is examined again when an answer arrives, over the client's own connections too."""

    def test_an_answer_over_a_kept_connection_is_judged_by_where_it_comes_from(
        self, client: PoliteClient, sockets: FakeSockets
    ) -> None:
        client.get(PAGE)
        sockets.peers[PUBLIC_ADDRESS] = "10.0.0.5"  # the connection now reports another peer
        with pytest.raises(ForbiddenHostError, match="refusing an answer from a local or private"):
            client.get(f"{PAGE}?again")
        assert len(sockets.connects) == 1
        assert len(sockets.requests) == 3

    def test_the_connection_pool_hands_the_connection_of_every_answer_to_the_check(
        self, client: PoliteClient, sockets: FakeSockets, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Should a later httpcore stop doing so, this fails instead of the check going quiet."""
        seen: list[object] = []
        guard = client._wire._guard
        examine = guard.connection_place

        def recording(stream: object) -> str | None:
            seen.append(stream)
            return examine(stream)

        monkeypatch.setattr(guard, "connection_place", recording)
        client.get(PAGE)
        assert len(seen) == 3  # the new connection, the robots.txt answer, the page
        assert all(hasattr(stream, "get_extra_info") for stream in seen)

    def test_a_connection_that_cannot_say_where_it_leads_is_not_trusted(self) -> None:
        guard = Guard(resolver=StubResolver(), own_address=lambda address: False, forbidden=())
        assert guard.connection_place(object()) == "a local or private network address"


class TestNetworkErrors:
    @pytest.mark.parametrize(
        ("error", "name"),
        [
            (httpcore.ReadError("reset"), "ReadError"),
            (httpcore.ReadTimeout("slow"), "ReadTimeout"),
            (httpcore.RemoteProtocolError("garbage"), "RemoteProtocolError"),
        ],
    )
    def test_they_reach_the_client_under_the_names_of_httpx(
        self, client: PoliteClient, sockets: FakeSockets, error: Exception, name: str
    ) -> None:
        sockets.pages["/events"] = [error]
        with pytest.raises(HttpRequestError, match=rf"4 attempt\(s\) \({name}\)"):
            client.get(PAGE)

    def test_an_error_while_the_body_arrives(
        self, client: PoliteClient, sockets: FakeSockets
    ) -> None:
        head = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\npartial"
        sockets.pages["/events"] = [head, httpcore.ReadError("reset")]
        with pytest.raises(HttpRequestError, match="ReadError"):
            client.get(PAGE)

    def test_an_answer_that_is_not_http_is_a_protocol_error(
        self, client: PoliteClient, sockets: FakeSockets
    ) -> None:
        sockets.pages["/events"] = [b"SSH-2.0-OpenSSH_9.6\r\n\r\n"]
        with pytest.raises(HttpRequestError, match="RemoteProtocolError"):
            client.get(PAGE)

    @pytest.mark.parametrize("name", ["Odd", "URL", "codes"])
    def test_an_error_httpx_has_no_name_for_is_a_transport_error(self, name: str) -> None:
        """Also when httpx has something else under that name."""
        failing = FakeSockets()
        failing.connect_error = type(name, (httpcore.NetworkError,), {})("odd")
        guard = Guard(resolver=StubResolver(), own_address=lambda address: False, forbidden=())
        transport = PinnedTransport(guard, inner=failing)
        with httpx.Client(transport=transport) as plain, pytest.raises(httpx.TransportError):
            plain.get(PAGE)


class TestBackend:
    @staticmethod
    def guard(resolver: StubResolver) -> Guard:
        return Guard(resolver=resolver, own_address=lambda address: False, forbidden=())

    def test_a_connection_without_a_lookup_before_it_is_looked_up_and_judged_here(self) -> None:
        resolver = StubResolver({"intranet.example.edu": ["10.0.0.5"]})
        sockets = FakeSockets()
        backend = PinnedBackend(self.guard(resolver), sockets)
        stream = backend.connect_tcp(NAME, 443)
        assert stream.get_extra_info("server_addr") == (PUBLIC_ADDRESS, 443)
        assert sockets.connects == [(PUBLIC_ADDRESS, 443)]
        with pytest.raises(Refused, match="resolves to a local or private network address"):
            backend.connect_tcp("intranet.example.edu", 443)
        assert len(sockets.connects) == 1
        assert resolver.calls == [NAME, "intranet.example.edu"]

    def test_the_lookup_of_one_host_is_not_used_for_another(self) -> None:
        resolver = StubResolver({"other.example.edu": [OTHER_ADDRESS]})
        sockets = FakeSockets()
        guard = self.guard(resolver)
        backend = PinnedBackend(guard, sockets)
        guard.lookup(NAME)
        backend.connect_tcp("other.example.edu", 443)
        backend.connect_tcp(NAME, 443)
        guard.forget()
        backend.connect_tcp(NAME, 443)
        assert [address for address, _ in sockets.connects] == [
            OTHER_ADDRESS,
            PUBLIC_ADDRESS,
            PUBLIC_ADDRESS,
        ]
        assert resolver.calls == [NAME, "other.example.edu", NAME, NAME]

    def test_the_lookup_of_the_request_in_progress_is_used(self) -> None:
        resolver = StubResolver()
        resolver.script(NAME, [OTHER_ADDRESS], ["10.0.0.5"])
        sockets = FakeSockets()
        guard = self.guard(resolver)
        backend = PinnedBackend(guard, sockets)
        assert guard.lookup(NAME) == [OTHER_ADDRESS]
        backend.connect_tcp(NAME, 443)
        backend.connect_tcp(NAME, 443)
        assert sockets.connects == [(OTHER_ADDRESS, 443)] * 2
        assert resolver.calls == [NAME]

    def test_a_host_without_an_address_cannot_be_connected_to(self) -> None:
        resolver = StubResolver({NAME: OSError("no such name")})
        backend = PinnedBackend(self.guard(resolver), FakeSockets())
        with pytest.raises(httpcore.ConnectError, match="has no address"):
            backend.connect_tcp(NAME, 443)

    def test_unix_sockets_do_not_exist(self) -> None:
        backend = PinnedBackend(self.guard(StubResolver()), FakeSockets())
        with pytest.raises(httpcore.ConnectError, match="unix sockets are not used"):
            backend.connect_unix_socket("/run/docker.sock")

    def test_each_thread_keeps_its_own_lookup(self) -> None:
        resolver = StubResolver({"b.example.edu": [OTHER_ADDRESS]})
        guard = self.guard(resolver)
        guard.lookup(NAME)
        seen: list[list[str]] = []
        thread = threading.Thread(target=lambda: seen.append(guard.lookup("b.example.edu")))
        thread.start()
        thread.join(timeout=5)
        assert seen == [[OTHER_ADDRESS]]
        assert guard.addresses_to_connect(NAME) == [PUBLIC_ADDRESS]
        assert resolver.calls == [NAME, "b.example.edu"]

    def test_the_default_socket_layer_is_the_one_of_httpcore(self) -> None:
        backend = PinnedBackend(self.guard(StubResolver()))
        assert isinstance(backend._inner, httpcore.SyncBackend)
        backend.sleep(0.0)


class _Loopback(Guard):
    """A guard that lets connections to this machine through, for the tests below only."""

    def _place(self, address: object) -> str | None:
        return None

    def connection_place(self, stream: object) -> str | None:
        return None


class TestRealSockets:
    """The transport over real sockets, against a server on the loopback interface."""

    @pytest.fixture
    def server(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, list[str]]]:
        served: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                served.append(f"{self.headers['Host']}{self.path}")
                body = b"x" * 70_000 if self.path == "/large" else b"hello"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        try:
            # A plain TCP server: http.server.HTTPServer would ask for the name of its host.
            listening = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        except OSError as error:  # pragma: no cover - a sandbox without loopback sockets
            pytest.skip(f"cannot listen on the loopback interface: {error}")
        listening.daemon_threads = True
        port = listening.server_address[1]

        def literal_only(host: str, to: int, *args: object) -> list[tuple[Any, ...]]:
            assert host == "127.0.0.1", "only the address may reach the socket layer, not a name"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, to))]

        monkeypatch.setattr(socket, "getaddrinfo", literal_only)
        serve = functools.partial(listening.serve_forever, poll_interval=0.01)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield port, served
        finally:
            listening.shutdown()
            listening.server_close()
            thread.join(timeout=5)

    def test_requests_go_to_the_cleared_address_and_carry_the_name(
        self, server: tuple[int, list[str]]
    ) -> None:
        port, served = server
        resolver = StubResolver({NAME: ["127.0.0.1"]})
        guard = _Loopback(resolver=resolver, own_address=lambda address: False, forbidden=())
        with httpx.Client(transport=PinnedTransport(guard)) as plain:
            for path in ("/a", "/large", "/b"):
                guard.lookup(NAME)
                response = plain.get(f"http://{NAME}:{port}{path}")
                guard.forget()
                assert response.status_code == 200
                assert len(response.content) == (70_000 if path == "/large" else 5)
                peer = response.extensions["network_stream"].get_extra_info("server_addr")
                assert peer == ("127.0.0.1", port)
        assert served == [f"{NAME}:{port}/a", f"{NAME}:{port}/large", f"{NAME}:{port}/b"]
        assert resolver.calls == [NAME] * 3

    def test_a_connection_that_ends_on_this_machine_is_closed_before_a_request_is_sent(
        self, server: tuple[int, list[str]]
    ) -> None:
        port, served = server

        class LookupOnly(Guard):
            def lookup(self, host: str) -> list[str]:
                return ["127.0.0.1"]  # a lookup that was fooled; the connection check is real

        guard = LookupOnly(resolver=StubResolver(), own_address=lambda address: False, forbidden=())
        with (
            httpx.Client(transport=PinnedTransport(guard)) as plain,
            pytest.raises(Refused, match="refusing a connection to a local or private network"),
        ):
            plain.get(f"http://{NAME}:{port}/internal")
        assert served == []

    def test_a_port_nobody_listens_on_is_a_connect_error(
        self, server: tuple[int, list[str]]
    ) -> None:
        port, _ = server
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            closed_port = spare.getsockname()[1]
        guard = _Loopback(
            resolver=StubResolver({NAME: ["127.0.0.1"]}),
            own_address=lambda address: False,
            forbidden=(),
        )
        assert closed_port != port
        with (
            httpx.Client(transport=PinnedTransport(guard)) as plain,
            pytest.raises(httpx.ConnectError),
        ):
            plain.get(f"http://{NAME}:{closed_port}/")


@pytest.fixture(scope="module")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Key and self-signed certificate for NAME, made with the openssl command."""
    openssl = shutil.which("openssl")
    if openssl is None:  # pragma: no cover - depends on the machine
        pytest.skip("the openssl command is not installed")
    directory = tmp_path_factory.mktemp("tls")
    key, made_certificate = directory / "key.pem", directory / "certificate.pem"
    command = [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2"]
    command += ["-keyout", str(key), "-out", str(made_certificate), "-subj", f"/CN={NAME}"]
    command += ["-addext", f"subjectAltName=DNS:{NAME}"]
    made = subprocess.run(command, capture_output=True, text=True, check=False)
    if made.returncode != 0:  # pragma: no cover - depends on the openssl version
        pytest.skip(f"openssl cannot make a certificate: {made.stderr[-200:]}")
    return key, made_certificate


class TestRealTls:
    """TLS over the loopback interface: the connection goes to an address, the certificate
    is still checked against the name."""

    @pytest.fixture
    def server(
        self, certificate: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[tuple[int, list[str], list[str | None]]]:
        served: list[str] = []
        names: list[str | None] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                served.append(f"{self.headers['Host']}{self.path}")
                self.send_response(200)
                self.send_header("Content-Length", "6")
                self.end_headers()
                self.wfile.write(b"secure")

            def log_message(self, format: str, *args: object) -> None:
                pass

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True

            def handle_error(self, request: object, client_address: object) -> None:
                pass  # a client that rejects the certificate hangs up during the handshake

        def remember(sock: ssl.SSLObject, name: str | None, context: ssl.SSLContext) -> None:
            names.append(name)

        key, own_certificate = certificate
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(own_certificate, key)
        context.sni_callback = remember
        try:
            listening = Server(("127.0.0.1", 0), Handler)
        except OSError as error:  # pragma: no cover - a sandbox without loopback sockets
            pytest.skip(f"cannot listen on the loopback interface: {error}")
        listening.socket = context.wrap_socket(
            listening.socket, server_side=True, do_handshake_on_connect=False
        )

        def literal_only(host: str, to: int, *args: object) -> list[tuple[Any, ...]]:
            assert host == "127.0.0.1", "only the address may reach the socket layer, not a name"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, to))]

        monkeypatch.setattr(socket, "getaddrinfo", literal_only)
        serve = functools.partial(listening.serve_forever, poll_interval=0.01)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield listening.server_address[1], served, names
        finally:
            listening.shutdown()
            listening.server_close()
            thread.join(timeout=5)

    @staticmethod
    def guard(*names: str) -> Guard:
        resolver = StubResolver({name: ["127.0.0.1"] for name in names})
        return _Loopback(resolver=resolver, own_address=lambda address: False, forbidden=())

    def test_the_certificate_is_checked_against_the_name_the_connection_is_for(
        self,
        server: tuple[int, list[str], list[str | None]],
        certificate: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        port, served, names = server
        trusting = ssl.create_default_context(cafile=str(certificate[1]))
        monkeypatch.setattr(httpx, "create_ssl_context", lambda **options: trusting)
        other = "www.example.edu"
        with httpx.Client(transport=PinnedTransport(self.guard(NAME, other))) as plain:
            response = plain.get(f"https://{NAME}:{port}/x")
            assert (response.status_code, response.text) == (200, "secure")
            peer = response.extensions["network_stream"].get_extra_info("server_addr")
            assert peer == ("127.0.0.1", port)
            with pytest.raises(httpx.ConnectError, match=r"(?i)certificate|hostname"):
                plain.get(f"https://{other}:{port}/x")  # the same address, another name
        assert served == [f"{NAME}:{port}/x"]
        assert names == [NAME, other]

    def test_a_certificate_nobody_vouches_for_is_refused(
        self, server: tuple[int, list[str], list[str | None]]
    ) -> None:
        """Connecting by address does not switch the checks of TLS off."""
        port, served, names = server
        with (
            httpx.Client(transport=PinnedTransport(self.guard(NAME))) as plain,
            pytest.raises(httpx.ConnectError, match=r"(?i)certificate"),
        ):
            plain.get(f"https://{NAME}:{port}/x")
        assert served == []
        assert names == [NAME]
