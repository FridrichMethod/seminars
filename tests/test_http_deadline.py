"""Tests for the two time limits of the live client: one answer, and one ``get`` as a whole."""

from __future__ import annotations

import functools
import itertools
import socket
import socketserver
import ssl
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest

from fakes import PUBLIC_ADDRESS, FakeClock, StubResolver
from seminars.config import HttpSettings, load_settings
from seminars.http import (
    DeadlineExceededError,
    HttpError,
    HttpRequestError,
    PoliteClient,
    RobotsUnavailableError,
)
from seminars.httpcache import HttpCache

REPO_ROOT = Path(__file__).resolve().parents[1]
NAME = "dept.example.edu"
HOST = f"https://{NAME}"
PAGE = f"{HOST}/events"
Handler = Callable[[httpx.Request], httpx.Response]


def build(
    settings: HttpSettings, clock: FakeClock, handler: Handler, cache_dir: Path | None = None
) -> PoliteClient:
    return PoliteClient(
        settings,
        cache_dir=cache_dir,
        clock=clock,
        sleep=clock.sleep,
        transport=httpx.MockTransport(handler),
        resolver=StubResolver(),
    )


class Dripping(httpx.SyncByteStream):
    """A body that arrives in pieces, each of which takes ``seconds`` of the fake clock."""

    def __init__(self, clock: FakeClock, seconds: float, pieces: int = 1000) -> None:
        self._clock, self._seconds, self._pieces = clock, seconds, pieces
        self.sent = 0

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(self._pieces):
            self._clock.advance(self._seconds)
            self.sent += 1
            yield b"x"


class TestSettings:
    def test_defaults(self, http_settings: HttpSettings) -> None:
        assert http_settings.max_exchange_seconds == 60.0
        assert http_settings.max_get_seconds == 600.0

    def test_the_committed_settings_state_them(self) -> None:
        settings = load_settings(REPO_ROOT / "config" / "settings.yaml")
        assert settings.http.max_exchange_seconds == 60.0
        assert settings.http.max_get_seconds == 600.0

    @pytest.mark.parametrize("name", ["max_exchange_seconds", "max_get_seconds"])
    def test_they_must_be_positive(self, name: str) -> None:
        with pytest.raises(ValueError, match=name):
            HttpSettings.model_validate({"user_agent": "x/1", name: 0})

    def test_the_error_is_a_request_error(self) -> None:
        error = DeadlineExceededError(PAGE, "gave up")
        assert isinstance(error, HttpRequestError)
        assert isinstance(error, HttpError)


class TestOneAnswer:
    """``max_exchange_seconds``: a request and its answer, headers and body."""

    def test_a_body_that_keeps_dripping_is_given_up(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        """Every piece arrives within the read timeout, so that timeout never fires."""
        body = Dripping(fake_clock, seconds=19.0)
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, stream=body)

        client = build(http_settings, fake_clock, handler)
        started = fake_clock()
        with pytest.raises(HttpRequestError, match="no complete answer within 60 s") as caught:
            client.get(PAGE)
        assert not isinstance(caught.value, DeadlineExceededError)
        assert "max_exchange_seconds" in str(caught.value)
        assert body.sent == 4  # 76 s: the first look at the clock after the limit
        assert paths == ["/robots.txt", "/events"]  # not retried
        assert fake_clock() - started < 80.0

    def test_an_answer_within_the_limit_is_read(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, stream=Dripping(fake_clock, seconds=19.0, pieces=3))

        assert build(http_settings, fake_clock, handler).get(PAGE).text == "xxx\n"

    def test_headers_that_took_too_long_are_not_followed_by_a_body(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        body = Dripping(fake_clock, seconds=0.0)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            fake_clock.advance(61.0)
            return httpx.Response(200, stream=body, headers={"ETag": '"v1"'})

        client = build(http_settings, fake_clock, handler)
        with pytest.raises(HttpRequestError, match="no complete answer within 60 s"):
            client.get(PAGE)
        assert body.sent == 0

    def test_a_slow_robots_file_blocks_its_origin(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(200, stream=Dripping(fake_clock, seconds=25.0))

        client = build(http_settings, fake_clock, handler)
        for _ in range(2):
            with pytest.raises(RobotsUnavailableError, match="unreachable"):
                client.get(PAGE)
        assert paths == ["/robots.txt"]

    def test_the_limit_comes_from_the_settings(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        body = Dripping(fake_clock, seconds=1.0)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, stream=body)

        client = build(http_settings.evolve(max_exchange_seconds=2.5), fake_clock, handler)
        with pytest.raises(HttpRequestError, match=r"within 2\.5 s"):
            client.get(PAGE)
        assert body.sent == 3


def stubborn(clock: FakeClock, requests: list[tuple[str, str]]) -> Handler:
    """A site that stays within every single limit of the client and still never finishes.

    Every URL answers 503 with a long Retry-After three times, robots.txt
    files redirect through a chain of hosts and end in ``Crawl-delay: 120``,
    and pages redirect to the next host.
    """
    seen: dict[tuple[str, str], int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        requests.append((host, path))
        count = seen.get((host, path), 0)
        seen[(host, path)] = count + 1
        if count < 3:
            return httpx.Response(503, headers={"Retry-After": "3600"})
        _, page, hop = host.split(".")[0].split("-")
        if path == "/robots.txt":
            if int(hop) < 5:
                target = f"https://r-{page}-{int(hop) + 1}.evil.example/robots.txt"
                return httpx.Response(302, headers={"Location": target})
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 120\n")
        if int(page) < 5:
            return httpx.Response(
                302, headers={"Location": f"https://p-{int(page) + 1}-0.evil.example/x"}
            )
        return httpx.Response(200, text="done")

    return handler


class TestOneGet:
    """``max_get_seconds``: everything one call does, waits included."""

    def test_a_site_that_stays_within_every_single_limit_is_given_up(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        """Without the limit this call made 168 requests and waited 2.4 hours."""
        requests: list[tuple[str, str]] = []
        client = build(http_settings, fake_clock, stubborn(fake_clock, requests))
        started = fake_clock()
        with pytest.raises(DeadlineExceededError, match="gave up after 600 s") as caught:
            client.get("https://p-0-0.evil.example/x")
        assert "max_get_seconds" in str(caught.value)
        assert caught.value.url == "https://p-0-0.evil.example/x"
        assert fake_clock() - started <= 600.0
        assert len(requests) < 20

    def test_a_wait_that_would_end_after_the_deadline_is_not_begun(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nCrawl-delay: 120\n")
            return httpx.Response(200, text="ok")

        client = build(http_settings.evolve(max_get_seconds=100.0), fake_clock, handler)
        started = fake_clock()
        with pytest.raises(DeadlineExceededError, match="gave up after 100 s"):
            client.get(PAGE)
        assert fake_clock.sleeps == []
        assert fake_clock() == started
        fake_clock.advance(30.0)
        assert client.get(PAGE).text == "ok\n"  # 90 s are left to wait: that fits
        assert fake_clock.sleeps == [90.0]

    def test_giving_up_is_not_held_against_the_host(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        """Running out of time while robots.txt is fetched does not block the origin."""
        answers = [httpx.Response(503, headers={"Retry-After": "50"}), httpx.Response(404)]
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/robots.txt":
                return answers.pop(0)
            return httpx.Response(200, text="ok")

        client = build(http_settings.evolve(max_get_seconds=30.0), fake_clock, handler)
        with pytest.raises(DeadlineExceededError):
            client.get(PAGE)
        assert paths == ["/robots.txt"]
        fake_clock.advance(60.0)
        assert client.get(PAGE).text == "ok\n"
        assert paths == ["/robots.txt", "/robots.txt", "/events"]

    def test_an_answer_may_only_take_what_is_left_of_the_call(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        body = Dripping(fake_clock, seconds=4.0)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, stream=body)

        client = build(http_settings.evolve(max_get_seconds=10.0), fake_clock, handler)
        started = fake_clock()
        with pytest.raises(DeadlineExceededError, match="gave up after 10 s"):
            client.get(PAGE)
        assert body.sent == 3
        assert fake_clock() - started == 13.0  # one piece past the limit, then it is noticed

    def test_redirects_share_the_time_of_one_call(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        hops: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            hops.append(request.url.path)
            fake_clock.advance(8.0)
            number = int(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(302, headers={"Location": f"/hop/{number + 1}"})

        client = build(http_settings.evolve(max_get_seconds=20.0), fake_clock, handler)
        with pytest.raises(DeadlineExceededError):
            client.get(f"{HOST}/hop/0")
        assert hops == ["/hop/0", "/hop/1", "/hop/2"]  # 9 s each; five redirects are allowed

    def test_every_call_has_its_own_time(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            fake_clock.advance(15.0)
            return httpx.Response(200, text="ok")

        client = build(http_settings.evolve(max_get_seconds=20.0), fake_clock, handler)
        for number in range(4):
            assert client.get(f"{PAGE}?page={number}").text == "ok\n"


class Ticking(FakeClock):
    """A clock that moves on by ``step`` seconds every time somebody looks at it."""

    step = 1.0

    def __call__(self) -> float:
        self.advance(self.step)
        return super().__call__()


class TestWaitingForOthers:
    """Somebody else has the host: the call waits for its turn, but not past its deadline."""

    @pytest.fixture(autouse=True)
    def short_polls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("seminars.httppace.LOCK_POLL_SECONDS", 0.001)

    def test_a_host_locked_by_another_process_is_given_up_at_the_deadline(
        self, http_settings: HttpSettings, tmp_path: Path
    ) -> None:
        clock = Ticking()
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            return httpx.Response(200, text="ok")

        client = build(http_settings.evolve(max_get_seconds=30.0), clock, handler, tmp_path)
        with (
            HttpCache(tmp_path).host_lock(NAME),  # a second lock file handle, as another process
            pytest.raises(DeadlineExceededError, match="gave up after 30 s"),
        ):
            client.get(PAGE)
        assert requests == []
        assert client.get(PAGE).text == "ok\n"  # the lock is free again

    def test_a_host_another_thread_is_using_is_given_up_at_the_deadline(
        self, http_settings: HttpSettings
    ) -> None:
        clock = Ticking()
        client = build(http_settings.evolve(max_get_seconds=30.0), clock, lambda request: None)  # type: ignore[arg-type,return-value]
        state = client._pacer.state(NAME)
        with state.lock, pytest.raises(DeadlineExceededError):
            client.get(PAGE)
        assert not state.lock.locked()

    def test_a_robots_file_another_thread_is_fetching_is_not_waited_for_forever(
        self, http_settings: HttpSettings
    ) -> None:
        clock = Ticking()
        client = build(http_settings.evolve(max_get_seconds=30.0), clock, lambda request: None)  # type: ignore[arg-type,return-value]
        state = client._pacer.state(NAME)
        with state.robots_lock, pytest.raises(DeadlineExceededError):
            client.get(PAGE)
        assert not state.robots_lock.locked()

    def test_a_turn_that_becomes_free_in_time_is_taken(
        self, http_settings: HttpSettings, tmp_path: Path
    ) -> None:
        clock = FakeClock()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404 if request.url.path == "/robots.txt" else 200, text="ok")

        client = build(http_settings, clock, handler, tmp_path)
        held = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with HttpCache(tmp_path).host_lock(NAME):
                held.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold)
        holder.start()
        assert held.wait(timeout=5)
        timer = threading.Timer(0.05, release.set)
        timer.start()
        try:
            assert client.get(PAGE).text == "ok\n"
        finally:
            release.set()
            holder.join(timeout=5)
            timer.cancel()
        assert clock.sleeps == [1.0]  # waiting for the lock is not one of the client's pauses


class SlowStream(httpcore.NetworkStream):
    """A connection of ``SlowSockets``: every operation takes time on the fake clock."""

    def __init__(self, sockets: SlowSockets) -> None:
        self._sockets = sockets
        self._pending: list[bytes] = []

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self._sockets.timeouts.append(("read", timeout))
        self._sockets.clock.advance(self._sockets.read_seconds)
        return self._pending.pop(0) if self._pending else b""

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._sockets.timeouts.append(("write", timeout))
        if buffer.endswith(b"\r\n\r\n"):
            path = buffer.split(b" ", 2)[1]
            if path == b"/robots.txt":
                self._pending = [b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"]
            else:
                self._pending = [bytes([byte]) for byte in self._sockets.answer]

    def close(self) -> None:
        pass

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        self._sockets.timeouts.append(("tls", timeout))
        self._sockets.clock.advance(self._sockets.tls_seconds)
        return self

    def get_extra_info(self, info: str) -> Any:
        return {"server_addr": (PUBLIC_ADDRESS, 443), "client_addr": ("192.0.2.10", 5000)}.get(info)


class SlowSockets(httpcore.NetworkBackend):
    """A socket layer on the fake clock that hands a page out one byte per read."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.read_seconds = 0.0
        self.tls_seconds = 0.0
        self.connect_seconds = 0.0
        self.answer = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        self.timeouts: list[tuple[str, float | None]] = []

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        self.timeouts.append(("connect", timeout))
        self.clock.advance(self.connect_seconds)
        return SlowStream(self)


class TestOwnTransport:
    """With the client's own transport the limit also holds while the headers arrive."""

    @pytest.fixture
    def sockets(self, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> SlowSockets:
        slow = SlowSockets(fake_clock)
        monkeypatch.setattr(httpcore, "SyncBackend", lambda: slow)
        return slow

    @pytest.fixture
    def client(
        self, http_settings: HttpSettings, fake_clock: FakeClock, sockets: SlowSockets
    ) -> Iterator[PoliteClient]:
        with PoliteClient(
            http_settings, clock=fake_clock, sleep=fake_clock.sleep, resolver=StubResolver()
        ) as polite:
            yield polite

    def test_an_answer_that_arrives_byte_by_byte_in_time_is_read(
        self, client: PoliteClient, sockets: SlowSockets
    ) -> None:
        sockets.read_seconds = 1.0
        assert client.get(PAGE).text == "ok\n"

    def test_headers_that_drip_are_given_up(
        self, client: PoliteClient, sockets: SlowSockets, fake_clock: FakeClock
    ) -> None:
        """One header byte every 19 s never trips the read timeout of 20 s."""
        client.get(f"{HOST}/first")
        sockets.read_seconds = 19.0
        sockets.timeouts.clear()
        started = fake_clock()
        with pytest.raises(HttpRequestError, match="no complete answer within 60 s"):
            client.get(PAGE)
        reads = [timeout for kind, timeout in sockets.timeouts if kind == "read"]
        assert reads == [20.0, 20.0, 20.0, 3.0]  # never longer than what is left
        assert fake_clock() - started <= 78.0

    def test_no_socket_operation_gets_more_time_than_is_left(
        self, client: PoliteClient, sockets: SlowSockets
    ) -> None:
        sockets.connect_seconds = 15.0
        sockets.tls_seconds = 30.0
        sockets.read_seconds = 10.0
        with pytest.raises(HttpRequestError, match="no complete answer within 60 s"):
            client.get(PAGE)
        steps = [step for step, _ in itertools.groupby(sockets.timeouts)]
        assert steps == [
            ("connect", 20.0),
            ("tls", 20.0),  # 45 s are left after connecting
            ("write", 15.0),  # 15 s are left after the handshake
            ("read", 15.0),
            ("write", 20.0),  # the next request has 60 s of its own
            ("read", 20.0),
            ("read", 10.0),  # five reads of 10 s later
        ]

    def test_a_handshake_after_the_limit_is_not_begun(
        self, client: PoliteClient, sockets: SlowSockets
    ) -> None:
        sockets.connect_seconds = 61.0
        with pytest.raises(RobotsUnavailableError, match="unreachable"):
            client.get(PAGE)
        assert [kind for kind, _ in sockets.timeouts] == ["connect"]

    def test_the_time_left_of_the_call_bounds_the_sockets_too(
        self, http_settings: HttpSettings, fake_clock: FakeClock, sockets: SlowSockets
    ) -> None:
        sockets.read_seconds = 4.0
        with (
            PoliteClient(
                http_settings.evolve(max_get_seconds=10.0),
                clock=fake_clock,
                sleep=fake_clock.sleep,
                resolver=StubResolver(),
            ) as client,
            pytest.raises(DeadlineExceededError),
        ):
            client.get(PAGE)
        assert max(timeout or 0.0 for _, timeout in sockets.timeouts) <= 10.0


class TestRealSocketsAndRealTime:
    """The limit against a server that really drips, over the loopback interface."""

    @pytest.fixture
    def port(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
        stop = threading.Event()

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                connection: socket.socket = self.request
                request = b""
                while b"\r\n\r\n" not in request:
                    request += connection.recv(4096)
                if b"GET /robots.txt " in request:
                    connection.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                    return
                if b"GET /slow-headers " in request:
                    connection.sendall(b"HTTP/1.1 200 OK\r\n")
                    drip = b"X-Padding: " + b"x" * 4000
                else:
                    connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4000\r\n\r\n")
                    drip = b"x" * 4000
                for byte in drip:
                    if stop.wait(0.02):  # far below the read timeout of the client
                        return
                    try:
                        connection.sendall(bytes([byte]))
                    except OSError:  # the client hung up, as it should
                        return

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        try:
            server = Server(("127.0.0.1", 0), Handler)
        except OSError as error:  # pragma: no cover - a sandbox without loopback sockets
            pytest.skip(f"cannot listen on the loopback interface: {error}")

        def literal_only(host: str, to: int, *args: object) -> list[tuple[Any, ...]]:
            assert host == "127.0.0.1", "only the address may reach the socket layer, not a name"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, to))]

        monkeypatch.setattr(socket, "getaddrinfo", literal_only)
        thread = threading.Thread(
            target=functools.partial(server.serve_forever, poll_interval=0.01), daemon=True
        )
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            stop.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @pytest.mark.parametrize("path", ["/slow-headers", "/slow-body"])
    def test_a_dripping_answer_ends_when_its_time_is_over(
        self, http_settings: HttpSettings, port: int, path: str
    ) -> None:
        """Dripped in full, either answer would take 80 s; no read ever times out."""
        settings = http_settings.evolve(
            min_interval_seconds=0.0, timeout_seconds=5.0, max_exchange_seconds=0.4, retries=0
        )
        with PoliteClient(settings, resolver=StubResolver({NAME: ["127.0.0.1"]})) as client:
            guard: Any = client._wire._guard
            guard._place = lambda address: None  # let this one test talk to the loopback
            guard.connection_place = lambda stream: None
            started = time.monotonic()
            with pytest.raises(HttpRequestError, match=r"no complete answer within 0\.4 s"):
                client.get(f"http://{NAME}:{port}{path}")
            elapsed = time.monotonic() - started
        assert 0.3 < elapsed < 3.0
