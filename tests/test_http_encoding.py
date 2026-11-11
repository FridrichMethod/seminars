"""Tests for compressed answers: the live client undoes one layer of gzip or deflate itself.

The HTTP library is never asked to decode a body. It builds one decoder per
coding a site lists and lets each produce its whole output at once, so a
few hundred bytes of stacked gzip can ask for gigabytes before the size of
the body is looked at. The client reads every answer raw and decompresses
it with a limit on each step; the memory tests here fail when that stops
being true.
"""

from __future__ import annotations

import contextlib
import functools
import gzip
import random
import tracemalloc
import zlib
from collections.abc import Callable, Iterator

import httpcore
import httpx
import pytest
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from fakes import PUBLIC_ADDRESS, FakeClock, StubResolver
from seminars.config import HttpSettings
from seminars.http import (
    HttpError,
    HttpRequestError,
    PoliteClient,
    ResponseTooLargeError,
    RobotsDisallowedError,
    RobotsUnavailableError,
    UnsupportedContentEncodingError,
)
from test_http_connect import FakeSockets, answer

HOST = "https://dept.example.edu"
PAGE = f"{HOST}/events"
TEXT = "Seminar at the caf\u00e9: \u2609 and more\n" * 400
BODY = TEXT.encode("utf-8")

Packer = Callable[[bytes], bytes]


def gz(data: bytes) -> bytes:
    return gzip.compress(data)


def zlib_stream(data: bytes) -> bytes:
    """``deflate`` as the standard defines it: a zlib stream (RFC 1950)."""
    return zlib.compress(data)


def bare_deflate(data: bytes) -> bytes:
    """``deflate`` as some servers send it: the deflate stream without the zlib wrapper."""
    packer = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return packer.compress(data) + packer.flush()


def plain(data: bytes) -> bytes:
    return data


@functools.cache
def zeros(megabytes: int, layers: int) -> bytes:
    """``megabytes`` MiB of zero bytes, gzipped ``layers`` times."""
    packer = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    block = bytes(1 << 20)
    payload = b"".join(packer.compress(block) for _ in range(megabytes)) + packer.flush()
    for _ in range(layers - 1):
        payload = gzip.compress(payload, 9)
    return payload


class Pieces(httpx.SyncByteStream):
    """A body that is handed out ``size`` bytes at a time and counts how often it is read."""

    def __init__(self, data: bytes, size: int | None = None) -> None:
        self._data, self._size = data, size or max(len(data), 1)
        self.reads = 0

    def __iter__(self) -> Iterator[bytes]:
        self.reads += 1
        for start in range(0, len(self._data), self._size):
            yield self._data[start : start + self._size]


class Site:
    """A fake site. ``serve`` says what a path answers with; everything else is "ok"."""

    def __init__(self) -> None:
        self.pages: dict[str, tuple[list[tuple[str, str]], bytes]] = {}
        self.requests: list[httpx.Request] = []
        self.bodies: list[Pieces] = []
        self.piece: int | None = None  # how many bytes of a body arrive at a time

    def serve(
        self,
        path: str,
        data: bytes,
        coding: str | list[str] | None = None,
        content_type: str = "text/html; charset=utf-8",
        declare_size: bool = True,
    ) -> None:
        headers = [("Content-Type", content_type)]
        if declare_size:
            headers.append(("Content-Length", str(len(data))))
        codings = [coding] if isinstance(coding, str) else coding or []
        headers.extend(("Content-Encoding", value) for value in codings)
        self.pages[path] = (headers, data)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        page = self.pages.get(request.url.path)
        if page is None:
            return httpx.Response(404 if request.url.path == "/robots.txt" else 200, text="ok")
        headers, data = page
        body = Pieces(data, self.piece)
        self.bodies.append(body)
        return httpx.Response(200, headers=headers, stream=body)


@pytest.fixture
def site() -> Site:
    return Site()


def build(site: Site, settings: HttpSettings, clock: FakeClock) -> PoliteClient:
    return PoliteClient(
        settings,
        clock=clock,
        sleep=clock.sleep,
        resolver=StubResolver(),
        transport=httpx.MockTransport(site),
    )


@pytest.fixture
def client(site: Site, http_settings: HttpSettings, fake_clock: FakeClock) -> PoliteClient:
    return build(site, http_settings, fake_clock)


@pytest.fixture
def small(site: Site, http_settings: HttpSettings, fake_clock: FakeClock) -> PoliteClient:
    """A client that reads bodies of at most 1000 bytes."""
    return build(site, http_settings.evolve(max_body_bytes=1000), fake_clock)


@pytest.fixture
def sockets(monkeypatch: pytest.MonkeyPatch) -> FakeSockets:
    """The socket layer under the client's own transport, without sockets."""
    fake = FakeSockets()
    monkeypatch.setattr(httpcore, "SyncBackend", lambda: fake)
    return fake


class TestWhatIsAskedFor:
    def test_every_request_asks_for_gzip_or_deflate(self, client: PoliteClient, site: Site) -> None:
        client.get(PAGE)
        assert site.paths() == ["/robots.txt", "/events"]
        assert [request.headers["accept-encoding"] for request in site.requests] == [
            "gzip, deflate",
            "gzip, deflate",
        ]

    def test_what_the_http_library_could_decode_does_not_matter(
        self,
        site: Site,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With brotli or zstandard installed, the library would offer them by itself."""
        monkeypatch.setattr("httpx._client.ACCEPT_ENCODING", "gzip, deflate, br, zstd")
        assert httpx.Client().headers["accept-encoding"] == "gzip, deflate, br, zstd"
        build(site, http_settings, fake_clock).get(PAGE)
        assert [request.headers["accept-encoding"] for request in site.requests] == [
            "gzip, deflate",
            "gzip, deflate",
        ]

    def test_a_caller_cannot_ask_for_another_coding(self, client: PoliteClient, site: Site) -> None:
        client.get(PAGE, headers={"Accept-Encoding": "br", "accept-encoding": "zstd"})
        assert site.requests[-1].headers.get_list("accept-encoding") == ["gzip, deflate"]

    def test_the_own_transport_sends_it_too(
        self, sockets: FakeSockets, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        client = PoliteClient(
            http_settings, clock=fake_clock, sleep=fake_clock.sleep, resolver=StubResolver()
        )
        client.get(PAGE)
        assert sockets.requests == [(PUBLIC_ADDRESS, "/robots.txt"), (PUBLIC_ADDRESS, "/events")]
        sent = b"".join(stream.written for stream in sockets.streams)
        assert sent.count(b"Accept-Encoding: gzip, deflate\r\n") == 2
        assert sent.lower().count(b"accept-encoding") == 2


ONE_LAYER = [
    pytest.param("gzip", gz, id="gzip"),
    pytest.param("x-gzip", gz, id="x-gzip"),
    pytest.param("GZip", gz, id="mixed-case"),
    pytest.param("deflate", zlib_stream, id="deflate-zlib"),
    pytest.param("deflate", bare_deflate, id="deflate-bare"),
    pytest.param("identity", plain, id="identity"),
    pytest.param(None, plain, id="none"),
    pytest.param("gzip, identity", gz, id="gzip-and-identity"),
    pytest.param(["identity", "deflate"], zlib_stream, id="two-header-lines"),
    pytest.param("", plain, id="empty-value"),
]


class TestOneLayer:
    @pytest.mark.parametrize(("coding", "pack"), ONE_LAYER)
    @pytest.mark.parametrize("piece", [None, 1, 7], ids=["whole", "bytewise", "pieces"])
    def test_the_body_comes_back_as_it_was_before_it_was_packed(
        self,
        client: PoliteClient,
        site: Site,
        coding: str | list[str] | None,
        pack: Packer,
        piece: int | None,
    ) -> None:
        site.piece = piece
        site.serve("/events", pack(BODY), coding)
        document = client.get(PAGE)
        assert document.text == TEXT
        assert document.content_type == "text/html"

    @pytest.mark.parametrize("coding", ["gzip", "deflate"])
    def test_an_empty_body_needs_no_decoding(
        self, client: PoliteClient, site: Site, coding: str
    ) -> None:
        site.serve("/events", b"", coding)
        assert client.get(PAGE).text == "\n"

    def test_a_packed_robots_file_is_honoured(self, client: PoliteClient, site: Site) -> None:
        site.serve("/robots.txt", gz(b"User-agent: *\nDisallow: /private/\n"), "gzip", "text/plain")
        with pytest.raises(RobotsDisallowedError):
            client.get(f"{HOST}/private/x")
        assert client.get(PAGE).text == "ok\n"

    def test_a_packed_body_through_the_own_transport(
        self, sockets: FakeSockets, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        sockets.pages["/events"] = [answer(200, gz(BODY), "Content-Encoding: gzip\r\n")]
        sockets.pages["/more"] = [answer(200, bare_deflate(BODY), "Content-Encoding: deflate\r\n")]
        client = PoliteClient(
            http_settings, clock=fake_clock, sleep=fake_clock.sleep, resolver=StubResolver()
        )
        assert client.get(PAGE).text == TEXT
        assert client.get(f"{HOST}/more").text == TEXT


DAMAGED = [
    pytest.param("gzip", gz(BODY)[:-9], id="gzip-cut-before-the-trailer"),
    pytest.param("gzip", gz(BODY)[:-3], id="gzip-cut-in-the-trailer"),
    pytest.param("gzip", gz(BODY)[:40], id="gzip-cut-in-the-data"),
    pytest.param("gzip", gz(BODY)[:5], id="gzip-cut-in-the-header"),
    pytest.param("deflate", zlib_stream(BODY)[:-1], id="zlib-cut"),
    pytest.param("deflate", bare_deflate(BODY)[:-1], id="bare-deflate-cut"),
    pytest.param("deflate", b"\x78", id="one-byte"),
    pytest.param("gzip", gz(BODY) + b"x", id="gzip-and-garbage"),
    pytest.param("gzip", gz(BODY) + gz(BODY), id="two-gzip-members"),
    pytest.param("deflate", zlib_stream(BODY) + b"\x00" * 300, id="zlib-and-garbage"),
    pytest.param("deflate", bare_deflate(BODY) + b"tail", id="bare-deflate-and-garbage"),
    pytest.param("gzip", BODY, id="not-packed-at-all"),
    pytest.param("deflate", b"\x00\x01\x02\x03" * 50, id="noise"),
    pytest.param("gzip", gz(BODY)[:60] + b"\xff" * 40 + gz(BODY)[100:], id="gzip-corrupted"),
]


class TestDamagedBodies:
    @pytest.mark.parametrize(("coding", "data"), DAMAGED)
    @pytest.mark.parametrize("piece", [None, 1, 64], ids=["whole", "bytewise", "pieces"])
    def test_a_body_that_cannot_be_unpacked_is_a_failed_request(
        self, client: PoliteClient, site: Site, coding: str, data: bytes, piece: int | None
    ) -> None:
        site.piece = piece
        site.serve("/events", data, coding)
        with pytest.raises(HttpRequestError, match=r"failed after 4 attempt\(s\)") as caught:
            client.get(PAGE)
        assert type(caught.value) is HttpRequestError
        assert site.paths() == ["/robots.txt"] + ["/events"] * 4  # retried like a network error

    def test_a_damaged_robots_file_blocks_the_origin(
        self, client: PoliteClient, site: Site
    ) -> None:
        site.serve("/robots.txt", gz(b"User-agent: *\nDisallow: /\n")[:-4], "gzip", "text/plain")
        for _ in range(2):
            with pytest.raises(RobotsUnavailableError, match="unreachable"):
                client.get(PAGE)
        assert site.paths() == ["/robots.txt"] * 4

    def test_a_cut_body_through_the_own_transport(
        self, sockets: FakeSockets, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        sockets.pages["/events"] = [answer(200, gz(BODY)[:-9], "Content-Encoding: gzip\r\n")]
        client = PoliteClient(
            http_settings, clock=fake_clock, sleep=fake_clock.sleep, resolver=StubResolver()
        )
        with pytest.raises(HttpRequestError, match=r"after 4 attempt\(s\)"):
            client.get(PAGE)


PACKED = [
    pytest.param("gzip", gz, id="gzip"),
    pytest.param("deflate", zlib_stream, id="deflate-zlib"),
    pytest.param("deflate", bare_deflate, id="deflate-bare"),
]


class TestSizeLimit:
    """``max_body_bytes`` counts the body after it was unpacked (here: 1000 bytes)."""

    @pytest.mark.parametrize(("coding", "pack"), PACKED)
    @pytest.mark.parametrize("piece", [None, 1], ids=["whole", "bytewise"])
    def test_a_body_exactly_at_the_limit_is_read(
        self, small: PoliteClient, site: Site, coding: str, pack: Packer, piece: int | None
    ) -> None:
        site.piece = piece
        site.serve("/events", pack(b"x" * 1000), coding)
        assert small.get(PAGE).text == "x" * 1000 + "\n"

    @pytest.mark.parametrize(("coding", "pack"), PACKED)
    @pytest.mark.parametrize("piece", [None, 1], ids=["whole", "bytewise"])
    def test_one_byte_more_is_too_large_and_not_asked_for_again(
        self, small: PoliteClient, site: Site, coding: str, pack: Packer, piece: int | None
    ) -> None:
        site.piece = piece
        site.serve("/events", pack(b"x" * 1001), coding)
        with pytest.raises(ResponseTooLargeError, match="larger than 1000 bytes"):
            small.get(PAGE)
        assert site.paths() == ["/robots.txt", "/events"]

    @pytest.mark.parametrize("declare_size", [True, False])
    def test_the_body_as_it_is_sent_may_not_be_larger_either(
        self, small: PoliteClient, site: Site, declare_size: bool
    ) -> None:
        """Bytes that do not compress: 990 of them are more than 1000 once they are packed."""
        packed = gz(random.Random(0).randbytes(990))
        assert len(packed) > 1000
        site.serve("/events", packed, "gzip", declare_size=declare_size)
        with pytest.raises(ResponseTooLargeError):
            small.get(PAGE)

    def test_the_limit_holds_through_the_own_transport(
        self, sockets: FakeSockets, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        sockets.pages["/fits"] = [answer(200, gz(b"x" * 1000), "Content-Encoding: gzip\r\n")]
        sockets.pages["/events"] = [answer(200, gz(b"x" * 1001), "Content-Encoding: gzip\r\n")]
        client = PoliteClient(
            http_settings.evolve(max_body_bytes=1000),
            clock=fake_clock,
            sleep=fake_clock.sleep,
            resolver=StubResolver(),
        )
        assert client.get(f"{HOST}/fits").text == "x" * 1000 + "\n"
        with pytest.raises(ResponseTooLargeError):
            client.get(PAGE)


REFUSED = [
    "gzip, gzip",
    "gzip, gzip, gzip",
    "deflate, gzip",
    "x-gzip, deflate",
    "gzip, identity, gzip",
    "br",
    "zstd",
    "compress",
    "gzip, br",
    "br, gzip",
    "gzip;q=1.0",
    "g zip",
]


class TestCodingsThatAreNotUndone:
    @pytest.mark.parametrize("coding", REFUSED)
    def test_the_answer_is_refused_unread_and_not_asked_for_again(
        self, client: PoliteClient, site: Site, coding: str
    ) -> None:
        site.serve("/events", gz(gz(BODY)), coding)
        with pytest.raises(UnsupportedContentEncodingError, match="refusing content encoding"):
            client.get(PAGE)
        assert site.paths() == ["/robots.txt", "/events"]
        assert [body.reads for body in site.bodies] == [0]

    def test_two_header_lines_are_two_codings(self, client: PoliteClient, site: Site) -> None:
        site.serve("/events", gz(gz(BODY)), ["gzip", "gzip"])
        with pytest.raises(UnsupportedContentEncodingError, match="gzip, gzip"):
            client.get(PAGE)
        assert [body.reads for body in site.bodies] == [0]

    def test_it_is_an_http_error_that_names_the_url(self, client: PoliteClient, site: Site) -> None:
        site.serve("/events", b"", "br")
        with pytest.raises(HttpError) as caught:
            client.get(PAGE)
        assert str(caught.value) == f"refusing content encoding br: {PAGE}"
        assert caught.value.url == PAGE

    def test_a_long_list_of_codings_is_cut_in_the_message(
        self, client: PoliteClient, site: Site
    ) -> None:
        site.serve("/events", b"", ", ".join(["gzip"] * 500))
        with pytest.raises(UnsupportedContentEncodingError) as caught:
            client.get(PAGE)
        assert len(str(caught.value)) < 200

    @pytest.mark.parametrize("coding", ["gzip, gzip", "br"])
    def test_such_a_robots_file_blocks_its_origin(
        self, client: PoliteClient, site: Site, coding: str
    ) -> None:
        site.serve("/robots.txt", gz(gz(b"User-agent: *\nDisallow:\n")), coding, "text/plain")
        for _ in range(2):
            with pytest.raises(RobotsUnavailableError, match="packed in a way that is not read"):
                client.get(PAGE)
        assert site.paths() == ["/robots.txt"]
        assert [body.reads for body in site.bodies] == [0]


@contextlib.contextmanager
def peak_memory() -> Iterator[list[int]]:
    """Collects the most memory that was in use inside the block, in bytes, above the start."""
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    tracemalloc.reset_peak()
    before = tracemalloc.get_traced_memory()[0]
    peak: list[int] = []
    try:
        yield peak
    finally:
        peak.append(tracemalloc.get_traced_memory()[1] - before)
        if not was_tracing:
            tracemalloc.stop()


LIMIT = 2_000_000
BOMB_MEGABYTES = 64
"""What the bombs below unpack to: 32 times the limit of the client that gets them."""

EXPECTED = {
    ("page", 1): (ResponseTooLargeError, "larger than 2000000 bytes"),
    ("page", 2): (UnsupportedContentEncodingError, "refusing content encoding gzip, gzip"),
    ("page", 3): (UnsupportedContentEncodingError, "refusing content encoding gzip, gzip, gzip"),
    ("robots", 1): (RobotsUnavailableError, "larger than 2000000 bytes"),
    ("robots", 2): (RobotsUnavailableError, "packed in a way that is not read"),
    ("robots", 3): (RobotsUnavailableError, "packed in a way that is not read"),
}


class TestMemory:
    """However little arrives, reading it never takes more memory than a body at the limit."""

    @pytest.fixture
    def settings(self, http_settings: HttpSettings) -> HttpSettings:
        return http_settings.evolve(max_body_bytes=LIMIT)

    def test_the_bombs_are_what_they_claim_to_be(self) -> None:
        assert len(zeros(BOMB_MEGABYTES, 1)) < 70_000
        assert len(zeros(BOMB_MEGABYTES, 2)) < 1000
        assert len(zeros(BOMB_MEGABYTES, 3)) < 1000
        assert len(gzip.decompress(gzip.decompress(zeros(1, 2)))) == 1 << 20

    @pytest.mark.parametrize("layers", [1, 2, 3])
    @pytest.mark.parametrize("where", ["page", "robots"])
    def test_stacked_gzip_through_a_transport_that_is_passed_in(
        self, site: Site, settings: HttpSettings, fake_clock: FakeClock, where: str, layers: int
    ) -> None:
        path = "/events" if where == "page" else "/robots.txt"
        site.piece = 8192  # as from a socket; the client's own transport below gets it in one read
        site.serve(path, zeros(BOMB_MEGABYTES, layers), ", ".join(["gzip"] * layers), "text/plain")
        client = build(site, settings, fake_clock)
        kind, message = EXPECTED[where, layers]
        with peak_memory() as peak, pytest.raises(HttpError, match=message) as caught:
            client.get(PAGE)
        assert type(caught.value) is kind
        assert peak[0] < 3 * LIMIT
        assert site.paths().count(path) == 1  # too large or refused: never asked for again

    @pytest.mark.parametrize("layers", [1, 2, 3])
    @pytest.mark.parametrize("where", ["page", "robots"])
    def test_stacked_gzip_through_the_own_transport(
        self,
        sockets: FakeSockets,
        settings: HttpSettings,
        fake_clock: FakeClock,
        where: str,
        layers: int,
    ) -> None:
        path = "/events" if where == "page" else "/robots.txt"
        coding = ", ".join(["gzip"] * layers)
        headers = f"Content-Encoding: {coding}\r\nContent-Type: text/plain\r\n"
        sockets.pages[path] = [answer(200, zeros(BOMB_MEGABYTES, layers), headers)]
        client = PoliteClient(
            settings, clock=fake_clock, sleep=fake_clock.sleep, resolver=StubResolver()
        )
        kind, message = EXPECTED[where, layers]
        with peak_memory() as peak, pytest.raises(HttpError, match=message) as caught:
            client.get(PAGE)
        assert type(caught.value) is kind
        assert peak[0] < 3 * LIMIT
        assert [requested for _, requested in sockets.requests].count(path) == 1


CODINGS = st.sampled_from(
    ["gzip", "x-gzip", "deflate", "identity", "gzip, gzip", "br", "deflate, deflate", ""]
)
PACKERS = st.sampled_from([gz, zlib_stream, bare_deflate, plain])


class TestAnyBody:
    @hypothesis_settings(max_examples=300, deadline=None)
    @given(
        data=st.binary(max_size=300),
        pack=PACKERS,
        coding=CODINGS,
        cut=st.integers(min_value=0, max_value=400),
        tail=st.binary(max_size=8),
        piece=st.sampled_from([None, 1, 5]),
    )
    def test_get_returns_a_document_or_raises_an_http_error(
        self,
        data: bytes,
        pack: Packer,
        coding: str,
        cut: int,
        tail: bytes,
        piece: int | None,
    ) -> None:
        clock = FakeClock()
        site = Site()
        site.piece = piece
        site.serve("/events", pack(data)[:cut] + tail, coding)
        settings = HttpSettings(user_agent="seminars-bot/1.0 (+https://example.org/)")
        client = build(site, settings.evolve(max_body_bytes=200, retries=0), clock)
        try:
            document = client.get(PAGE)
        except HttpError:
            return
        assert len(document.text.encode("utf-8")) <= 4 * 200 + 1
