"""Tests for "public hosts only" beyond the spelling of a host: lookups and the peer address.

No test here resolves a name for real: the clients get a ``StubResolver``,
and the peer of a connection is a stand-in for the ``network_stream``
extension that httpx attaches to real responses.
"""

from __future__ import annotations

import functools
import http.server
import socket
import socketserver
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from factories import make_document
from fakes import PUBLIC_ADDRESS, FakeClock, StubResolver
from seminars.config import HttpSettings
from seminars.fixtures import FixtureStore
from seminars.http import (
    ForbiddenHostError,
    HostBudgetExceededError,
    HttpRequestError,
    OfflineHttpClient,
    PoliteClient,
    RobotsUnavailableError,
    UnsupportedSchemeError,
    is_public_address,
    system_resolver,
)
from seminars.httpcache import HttpCache

NAME = "dept.example.edu"
HOST = f"https://{NAME}"
ROBOTS = f"{HOST}/robots.txt"
PAGE = f"{HOST}/events"
INTRANET = "intranet.example.edu"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"

PRIVATE_ANSWERS = [
    ["127.0.0.1"],
    ["10.1.2.3"],
    ["172.16.0.9"],
    ["192.168.1.10"],
    ["169.254.169.254"],  # cloud metadata service
    ["100.64.0.1"],  # carrier-grade NAT
    ["0.0.0.0"],
    ["::1"],
    ["fc00::1"],
    ["fd00:ec2::254"],
    ["fe80::1%eth0"],
    ["::ffff:192.168.0.1"],
]
"""Loopback, private, link-local and unique local answers, IPv4 and IPv6."""


@pytest.fixture
def client(
    http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
) -> PoliteClient:
    return PoliteClient(http_settings, clock=fake_clock, sleep=fake_clock.sleep, resolver=resolver)


class TestIsPublicAddress:
    @pytest.mark.parametrize(
        "address",
        [PUBLIC_ADDRESS, PUBLIC_V6, "8.8.8.8", "::ffff:8.8.8.8", "2606:4700:4700::1111%eth0"],
    )
    def test_global_addresses(self, address: str) -> None:
        assert is_public_address(address)

    @pytest.mark.parametrize("address", [answer[0] for answer in PRIVATE_ANSWERS])
    def test_local_and_private_addresses(self, address: str) -> None:
        assert not is_public_address(address)

    @pytest.mark.parametrize(
        "address",
        [
            "192.0.2.1",  # documentation
            "198.18.0.1",  # benchmarking
            "240.0.0.1",  # reserved
            "255.255.255.255",  # broadcast
            "224.0.0.1",  # multicast
            "ff02::1",  # multicast
            "::",  # unspecified
            "2001:db8::1",  # documentation
            "fec0::1",  # site-local (retired)
            "2002:7f00:1::",  # 6to4
        ],
    )
    def test_reserved_addresses(self, address: str) -> None:
        assert not is_public_address(address)

    @pytest.mark.parametrize(
        ("address", "public"),
        [
            ("::ffff:127.0.0.1", False),  # IPv4-mapped
            ("::ffff:8.8.8.8", True),
            ("::ffff:0:10.0.0.5", False),  # IPv4-translated
            ("::ffff:0:8.8.8.8", True),
            ("64:ff9b::7f00:1", False),  # NAT64 of 127.0.0.1
            ("64:ff9b::a9fe:a9fe", False),  # NAT64 of 169.254.169.254
            ("64:ff9b::808:808", True),  # NAT64 of 8.8.8.8
            ("::127.0.0.1", False),  # IPv4-compatible
            ("::192.168.1.1", False),
        ],
    )
    def test_an_ipv6_address_that_wraps_an_ipv4_address_is_judged_by_it(
        self, address: str, public: bool
    ) -> None:
        assert is_public_address(address) is public

    @pytest.mark.parametrize("text", ["", "dept.example.edu", "8.8.8.8.8", "%eth0", "8.8.8.8:80"])
    def test_text_that_is_not_an_address(self, text: str) -> None:
        assert not is_public_address(text)


class TestSystemResolver:
    def test_asks_the_operating_system_and_returns_each_address_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[tuple[Any, ...]] = []

        def lookup(host: str, port: object, **options: object) -> list[tuple[Any, ...]]:
            asked.append((host, port, options))
            stream = socket.SOCK_STREAM
            return [
                (socket.AF_INET, stream, 6, "", (PUBLIC_ADDRESS, 0)),
                (socket.AF_INET, stream, 6, "", (PUBLIC_ADDRESS, 0)),
                (socket.AF_INET6, stream, 6, "", (PUBLIC_V6, 0, 0, 0)),
            ]

        monkeypatch.setattr(socket, "getaddrinfo", lookup)
        assert system_resolver(NAME) == [PUBLIC_ADDRESS, PUBLIC_V6]
        assert asked == [(NAME, None, {"type": socket.SOCK_STREAM})]

    def test_an_unknown_name_is_an_os_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def lookup(host: str, port: object, **options: object) -> list[tuple[Any, ...]]:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", lookup)
        with pytest.raises(OSError, match="Name or service not known"):
            system_resolver("no-such-host.example.edu")

    def test_it_is_what_a_client_uses_unless_told_otherwise(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        respx_router: respx.MockRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        asked: list[str] = []

        def lookup(host: str, port: object, **options: object) -> list[tuple[Any, ...]]:
            asked.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", lookup)
        client = PoliteClient(http_settings, clock=fake_clock, sleep=fake_clock.sleep)
        with pytest.raises(ForbiddenHostError):
            client.get(PAGE)
        assert asked == [NAME]
        assert respx_router.calls.call_count == 0

    def test_the_guard_of_the_test_suite_catches_an_unstubbed_client(
        self, http_settings: HttpSettings, fake_clock: FakeClock, respx_router: respx.MockRouter
    ) -> None:
        client = PoliteClient(http_settings, clock=fake_clock, sleep=fake_clock.sleep)
        with pytest.raises(AssertionError, match=r"real DNS lookup of 'dept\.example\.edu'"):
            client.get(PAGE)
        assert respx_router.calls.call_count == 0


class TestResolvedAddresses:
    @pytest.mark.parametrize("addresses", PRIVATE_ANSWERS, ids=lambda answer: answer[0])
    def test_a_name_that_resolves_to_a_private_address_is_refused_without_a_request(
        self,
        client: PoliteClient,
        resolver: StubResolver,
        respx_router: respx.MockRouter,
        fake_clock: FakeClock,
        addresses: list[str],
    ) -> None:
        resolver.answers[NAME] = addresses
        robots = respx_router.get(ROBOTS).respond(404)
        page = respx_router.get(PAGE).respond(200, text="internal")
        with pytest.raises(ForbiddenHostError, match="resolves to a local or private network"):
            client.get(PAGE)
        assert (robots.call_count, page.call_count) == (0, 0)
        assert client.request_counts() == {NAME: 0}
        assert fake_clock.sleeps == []
        assert resolver.calls == [NAME]

    @pytest.mark.parametrize(
        "addresses",
        [[PUBLIC_ADDRESS, "10.0.0.5"], ["127.0.0.1", PUBLIC_ADDRESS], [PUBLIC_V6, "::1"]],
        ids=["private-last", "private-first", "ipv6"],
    )
    def test_one_private_address_among_public_ones_is_enough(
        self,
        client: PoliteClient,
        resolver: StubResolver,
        respx_router: respx.MockRouter,
        addresses: list[str],
    ) -> None:
        resolver.answers[NAME] = addresses
        with pytest.raises(ForbiddenHostError):
            client.get(PAGE)
        assert respx_router.calls.call_count == 0

    def test_a_refused_name_is_refused_every_time_and_leaves_no_record(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        resolver: StubResolver,
        respx_router: respx.MockRouter,
        tmp_path: Path,
    ) -> None:
        resolver.answers[NAME] = ["192.168.1.10"]
        client = PoliteClient(
            http_settings,
            cache_dir=tmp_path,
            clock=fake_clock,
            sleep=fake_clock.sleep,
            resolver=resolver,
        )
        for _ in range(3):
            with pytest.raises(ForbiddenHostError):
                client.get(PAGE)
        assert respx_router.calls.call_count == 0
        assert resolver.calls == [NAME] * 3
        assert list(tmp_path.rglob("*.json")) == []

    def test_the_page_request_is_checked_even_when_robots_was_fetched_before(
        self, client: PoliteClient, resolver: StubResolver, respx_router: respx.MockRouter
    ) -> None:
        resolver.script(NAME, [PUBLIC_ADDRESS], ["10.0.0.5"])
        robots = respx_router.get(ROBOTS).respond(404)
        page = respx_router.get(PAGE).respond(200, text="internal")
        with pytest.raises(ForbiddenHostError):
            client.get(PAGE)
        assert (robots.call_count, page.call_count) == (1, 0)
        assert client.request_counts() == {NAME: 0}

    def test_every_request_is_preceded_by_a_lookup(
        self, client: PoliteClient, resolver: StubResolver, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(ROBOTS).respond(404)
        respx_router.get(url__startswith=PAGE).respond(200, text="ok")
        client.get(f"{PAGE}?page=1")
        client.get(f"{PAGE}?page=2")
        assert resolver.calls == [NAME, NAME, NAME]

    def test_a_redirect_to_such_a_name_is_refused(
        self, client: PoliteClient, resolver: StubResolver, respx_router: respx.MockRouter
    ) -> None:
        resolver.answers[INTRANET] = ["192.168.1.10"]
        respx_router.get(ROBOTS).respond(404)
        respx_router.get(PAGE).respond(302, headers={"Location": f"https://{INTRANET}/admin"})
        inside = respx_router.get(url__startswith=f"https://{INTRANET}/").respond(200, text="x")
        with pytest.raises(ForbiddenHostError, match="resolves to a local or private network"):
            client.get(PAGE)
        assert inside.call_count == 0
        assert respx_router.calls.call_count == 2
        assert resolver.calls == [NAME, NAME, INTRANET]

    def test_a_robots_redirect_to_such_a_name_blocks_the_origin(
        self, client: PoliteClient, resolver: StubResolver, respx_router: respx.MockRouter
    ) -> None:
        resolver.answers[INTRANET] = ["169.254.169.254"]
        location = f"http://{INTRANET}/latest/meta-data/"
        robots = respx_router.get(ROBOTS).respond(302, headers={"Location": location})
        inside = respx_router.get(url__startswith=f"http://{INTRANET}/").respond(200, text="x")
        page = respx_router.get(PAGE).respond(200, text="ok")
        for _ in range(2):
            with pytest.raises(RobotsUnavailableError, match="must not be fetched") as caught:
                client.get(PAGE)
            assert str(caught.value).endswith(ROBOTS)
        assert (robots.call_count, inside.call_count, page.call_count) == (1, 0, 0)

    def test_the_name_that_is_looked_up_is_the_one_the_connection_goes_to(
        self, http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404 if request.url.path == "/robots.txt" else 200, text="ok")

        with PoliteClient(
            http_settings,
            clock=fake_clock,
            sleep=fake_clock.sleep,
            transport=httpx.MockTransport(handler),
            resolver=resolver,
        ) as client:
            client.get("https://Dept.Example.EDU/events")
            client.get("https://b\u00fccher.example/katalog")
            client.get("http://[2606:4700:4700::1111]:8080/status")
        assert sorted(set(resolver.calls)) == [
            "2606:4700:4700::1111",
            "dept.example.edu",
            "xn--bcher-kva.example",
        ]

    def test_a_url_the_transport_cannot_parse_is_rejected_before_any_lookup(
        self, client: PoliteClient, resolver: StubResolver, respx_router: respx.MockRouter
    ) -> None:
        with pytest.raises(UnsupportedSchemeError, match="invalid URL"):
            client.get(f"https://{NAME}:abc/events")
        assert resolver.calls == []
        assert respx_router.calls.call_count == 0
        respx_router.get(ROBOTS).respond(404)
        with pytest.raises(UnsupportedSchemeError, match="invalid URL"):
            client.get(f"{HOST}/a\x00b")
        assert resolver.calls == [NAME]
        assert respx_router.calls.call_count == 1


class TestNamesWithoutAnAddress:
    def test_a_failed_lookup_is_retried_like_a_network_error(
        self,
        client: PoliteClient,
        resolver: StubResolver,
        respx_router: respx.MockRouter,
        fake_clock: FakeClock,
    ) -> None:
        failure = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        resolver.script(NAME, [PUBLIC_ADDRESS], failure, [PUBLIC_ADDRESS])
        respx_router.get(ROBOTS).respond(404)
        page = respx_router.get(PAGE).respond(200, text="ok")
        assert client.get(PAGE).text == "ok\n"
        assert page.call_count == 1
        assert fake_clock.sleeps == [1.0, 1.0]
        assert client.request_counts() == {NAME: 2}

    @pytest.mark.parametrize(
        "answer",
        [socket.gaierror(socket.EAI_NONAME, "Name or service not known"), [], UnicodeError("idna")],
        ids=["no-such-name", "empty-answer", "not-a-name"],
    )
    def test_a_name_that_never_resolves_is_a_request_error_not_a_forbidden_host(
        self,
        client: PoliteClient,
        resolver: StubResolver,
        respx_router: respx.MockRouter,
        fake_clock: FakeClock,
        answer: list[str] | Exception,
    ) -> None:
        resolver.script(NAME, [PUBLIC_ADDRESS])
        resolver.answers[NAME] = answer
        respx_router.get(ROBOTS).respond(404)
        page = respx_router.get(PAGE).respond(200, text="ok")
        with pytest.raises(HttpRequestError, match=r"after 4 attempt\(s\) \(host name does not"):
            client.get(PAGE)
        assert page.call_count == 0
        assert fake_clock.sleeps == [1.0, 1.0, 2.0, 4.0]
        assert client.request_counts() == {NAME: 4}
        assert resolver.calls == [NAME] * 5

    def test_robots_of_a_name_that_does_not_resolve_is_unreachable(
        self,
        client: PoliteClient,
        resolver: StubResolver,
        respx_router: respx.MockRouter,
    ) -> None:
        resolver.answers[NAME] = OSError("no such name")
        with pytest.raises(RobotsUnavailableError, match="unreachable"):
            client.get(PAGE)
        assert respx_router.calls.call_count == 0
        assert resolver.calls == [NAME] * 4

    def test_failed_lookups_use_up_the_request_budget(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        resolver: StubResolver,
        respx_router: respx.MockRouter,
    ) -> None:
        resolver.script(NAME, [PUBLIC_ADDRESS])
        resolver.answers[NAME] = OSError("no such name")
        respx_router.get(ROBOTS).respond(404)
        client = PoliteClient(
            http_settings.evolve(max_requests_per_host=2),
            clock=fake_clock,
            sleep=fake_clock.sleep,
            resolver=resolver,
        )
        with pytest.raises(HostBudgetExceededError):
            client.get(PAGE)
        assert client.request_counts() == {NAME: 2}


class Connection:
    """Stand-in for the ``network_stream`` extension of a response: it knows its peer."""

    def __init__(self, *peers: object) -> None:
        self._peers = list(peers)
        self.asked: list[str] = []

    def get_extra_info(self, info: str) -> object:
        self.asked.append(info)
        peer = self._peers.pop(0) if len(self._peers) > 1 else self._peers[0]
        if isinstance(peer, Exception):
            raise peer
        return peer


class Body(httpx.SyncByteStream):
    """A response body that knows whether anybody read it."""

    def __init__(self, content: bytes = b"internal dashboard") -> None:
        self._content = content
        self.reads = 0
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        self.reads += 1
        yield self._content

    def close(self) -> None:
        self.closed = True


PRIVATE_PEERS: list[object] = [
    ("127.0.0.1", 443),
    ("10.0.0.5", 443),
    ("169.254.169.254", 80),
    ("::1", 443, 0, 0),
    ("fd00:ec2::254", 80, 0, 0),
    ("fe80::1%eth0", 443, 0, 3),
    ("::ffff:192.168.1.1", 443, 0, 0),
]
UNKNOWN_PEERS: list[object] = [None, "/run/proxy.sock", (), (None, 443), ("not an address", 443)]
"""Connections whose peer cannot be told are refused as well."""


class TestPeerAddress:
    """The rebinding case: the lookup said "public", the connection went somewhere else."""

    @staticmethod
    def build(
        settings: HttpSettings,
        clock: FakeClock,
        resolver: StubResolver,
        handler: Callable[[httpx.Request], httpx.Response],
        cache_dir: Path | None = None,
    ) -> PoliteClient:
        return PoliteClient(
            settings,
            cache_dir=cache_dir,
            clock=clock,
            sleep=clock.sleep,
            transport=httpx.MockTransport(handler),
            resolver=resolver,
        )

    @pytest.mark.parametrize("peer", [*PRIVATE_PEERS, *UNKNOWN_PEERS], ids=str)
    def test_an_answer_from_a_private_peer_is_refused_unread_uncached_and_not_retried(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        resolver: StubResolver,
        tmp_path: Path,
        peer: object,
    ) -> None:
        body = Body()
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/robots.txt":
                public = Connection((PUBLIC_ADDRESS, 443))
                return httpx.Response(404, extensions={"network_stream": public})
            return httpx.Response(
                200,
                stream=body,
                headers={"ETag": '"v1"', "Content-Type": "text/html"},
                extensions={"network_stream": Connection(peer)},
            )

        client = self.build(http_settings, fake_clock, resolver, handler, tmp_path)
        with pytest.raises(ForbiddenHostError, match="answer from a local or private network"):
            client.get(PAGE)
        assert paths == ["/robots.txt", "/events"]
        assert (body.reads, body.closed) == (0, True)
        assert HttpCache(tmp_path).page(PAGE) is None
        assert not (tmp_path / "pages").exists()
        assert resolver.calls == [NAME, NAME]

    def test_a_robots_answer_from_a_private_peer_is_refused_and_not_remembered(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        resolver: StubResolver,
        tmp_path: Path,
    ) -> None:
        body = Body(b"User-agent: *\nDisallow:\n")
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            connection = Connection(("10.0.0.5", 443))
            return httpx.Response(200, stream=body, extensions={"network_stream": connection})

        client = self.build(http_settings, fake_clock, resolver, handler, tmp_path)
        for _ in range(2):
            with pytest.raises(ForbiddenHostError):
                client.get(PAGE)
        assert paths == ["/robots.txt", "/robots.txt"]
        assert body.reads == 0
        assert HttpCache(tmp_path).host(NAME).robots == {}

    def test_a_redirect_from_a_private_peer_is_not_followed(
        self, http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
    ) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(
                302,
                headers={"Location": f"{HOST}/next"},
                extensions={"network_stream": Connection(("127.0.0.1", 443))},
            )

        client = self.build(http_settings, fake_clock, resolver, handler)
        with pytest.raises(ForbiddenHostError):
            client.get(PAGE)
        assert paths == ["/robots.txt", "/events"]

    @pytest.mark.parametrize(
        "peer", [(PUBLIC_ADDRESS, 443), (PUBLIC_V6, 443, 0, 0), ("::ffff:8.8.8.8", 80, 0, 0)]
    )
    def test_an_answer_from_a_public_peer_is_read(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        resolver: StubResolver,
        peer: object,
    ) -> None:
        connection = Connection(peer)

        def handler(request: httpx.Request) -> httpx.Response:
            status = 404 if request.url.path == "/robots.txt" else 200
            return httpx.Response(status, text="ok", extensions={"network_stream": connection})

        client = self.build(http_settings, fake_clock, resolver, handler)
        assert client.get(PAGE).text == "ok\n"
        assert connection.asked == ["server_addr", "server_addr"]

    def test_a_connection_that_is_gone_before_the_check_is_a_failed_attempt(
        self, http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
    ) -> None:
        connection = Connection(OSError(107, "Transport endpoint is not connected"), None)
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            lost = len(paths) == 2
            peer = connection if lost else Connection((PUBLIC_ADDRESS, 443))
            return httpx.Response(200, text="ok", extensions={"network_stream": peer})

        client = self.build(http_settings, fake_clock, resolver, handler)
        assert client.get(PAGE).text == "ok\n"
        assert paths == ["/robots.txt", "/events", "/events"]

    def test_a_transport_without_a_connection_has_no_peer_to_check(
        self, http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert "network_stream" not in request.extensions
            return httpx.Response(404 if request.url.path == "/robots.txt" else 200, text="ok")

        assert self.build(http_settings, fake_clock, resolver, handler).get(PAGE).text == "ok\n"


class _ToLoopback(httpx.BaseTransport):
    """Connects to a server on this machine whatever host a URL names: a rebinding answer."""

    def __init__(self, port: int) -> None:
        self._port = port
        self._inner = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        target = request.url.copy_with(scheme="http", host="127.0.0.1", port=self._port)
        return self._inner.handle_request(
            httpx.Request(request.method, target, headers=request.headers)
        )

    def close(self) -> None:
        self._inner.close()


class TestRealConnection:
    """The peer check against the real httpx stack, over the loopback interface only."""

    def test_the_real_transport_reports_its_peer_and_a_loopback_peer_is_refused(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        resolver: StubResolver,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        served: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                served.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "8")
                self.end_headers()
                self.wfile.write(b"internal")

            def log_message(self, format: str, *args: object) -> None:
                pass

        try:
            # A plain TCP server: http.server.HTTPServer would ask for the name of its host.
            server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        except OSError as error:  # pragma: no cover - a sandbox without loopback sockets
            pytest.skip(f"cannot listen on the loopback interface: {error}")
        port = server.server_address[1]

        def literal_only(host: str, *args: object, **kwargs: object) -> list[tuple[Any, ...]]:
            assert host == "127.0.0.1", "only the address literal may reach the socket layer"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        monkeypatch.setattr(socket, "getaddrinfo", literal_only)
        serve = functools.partial(server.serve_forever, poll_interval=0.01)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with (
                PoliteClient(
                    http_settings,
                    clock=fake_clock,
                    sleep=fake_clock.sleep,
                    transport=_ToLoopback(port),
                    resolver=resolver,
                ) as client,
                pytest.raises(ForbiddenHostError, match="answer from a local or private"),
            ):
                client.get(PAGE)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        assert served == ["/robots.txt"]  # the one blind request of the rebinding race
        assert resolver.calls == [NAME]


class TestProxies:
    @pytest.mark.parametrize("variable", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "https_proxy"])
    def test_the_environment_cannot_put_a_proxy_in_between(
        self, http_settings: HttpSettings, monkeypatch: pytest.MonkeyPatch, variable: str
    ) -> None:
        monkeypatch.setenv(variable, "http://127.0.0.1:3128")
        with PoliteClient(http_settings, resolver=StubResolver()) as client:
            inner = client._client
            assert inner.trust_env is False
            assert inner._mounts == {}
        with httpx.Client() as naive:
            assert naive._mounts != {}


class TestOfflineClient:
    def test_serves_a_recording_of_any_name_and_looks_nothing_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lookups: list[object] = []
        monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: lookups.append(args))
        store = FixtureStore(tmp_path / "sites-x")
        store.add(make_document(PAGE, "recorded"))
        client = OfflineHttpClient([store])
        assert client.get(PAGE).text == "recorded\n"
        assert lookups == []
