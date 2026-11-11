"""Tests for the promise that ``get`` returns a document or raises an ``HttpError``.

Everything a site controls (URLs, Location, ETag, Retry-After) and everything
the disk can do to the cache must end in one of the two, never in another
exception: callers such as the fixture recorder only catch ``HttpError``.
"""

from __future__ import annotations

import errno
import logging
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fakes import FakeClock, StubResolver
from seminars.config import HttpSettings
from seminars.fixtures import main
from seminars.http import (
    HttpError,
    HttpRequestError,
    PoliteClient,
    ResponseTooLargeError,
    RobotsUnavailableError,
    UnsupportedSchemeError,
)
from seminars.httpcache import CachedPage, HostRecord, HttpCache
from seminars.httpwire import Exchange
from seminars.models import FetchedDocument

HOST = "https://dept.example.edu"
PAGE = f"{HOST}/events"
E_ACUTE = chr(0xE9)
Handler = Callable[[httpx.Request], httpx.Response]


class Network:
    """A fake network: answers through ``handler`` and remembers the requests."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def build(
    http_settings: HttpSettings, clock: FakeClock, handler: Handler, cache_dir: Path | None = None
) -> tuple[PoliteClient, Network]:
    network = Network(handler)
    client = PoliteClient(
        http_settings,
        cache_dir=cache_dir,
        clock=clock,
        sleep=clock.sleep,
        transport=httpx.MockTransport(network),
        resolver=StubResolver(),
    )
    return client, network


def redirecting(location: bytes, *, robots_too: bool = False) -> Handler:
    """Every page (and, if asked, robots.txt) redirects to ``location``, given as raw bytes."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt" and not robots_too:
            return httpx.Response(404)
        return httpx.Response(302, headers=[(b"location", location)])

    return handler


def page_with(headers: list[tuple[bytes, bytes]]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, headers=[*headers, (b"content-type", b"text/html")], content=b"hi"
        )

    return handler


BAD_LOCATIONS = [
    b"http://[",
    b"//[::1",
    b"https://[fe80::1%25eth0]/x",
    "https://example.edu\u2100/x".encode(),
    b"https://" + b"a" * 300 + b".edu/",
    b"https://example.edu:99999/",
    b"https://example.edu:0/",
    b"https://example.edu/\x1b[2J\x1b[31mPWNED",
    b"https://example.edu/\xed\xa0\xbd",
    b"https://user:secret@example.edu/",
    b"\xff\xfe\x00",
]


class TestRedirects:
    @pytest.mark.parametrize("location", BAD_LOCATIONS, ids=ascii)
    def test_a_redirect_to_something_that_is_no_url_is_an_http_error(
        self, http_settings: HttpSettings, fake_clock: FakeClock, tmp_path: Path, location: bytes
    ) -> None:
        client, network = build(http_settings, fake_clock, redirecting(location), tmp_path)
        with pytest.raises(HttpError) as caught:
            client.get(PAGE)
        assert str(caught.value).isascii()
        assert network.paths[:2] == ["/robots.txt", "/events"]

    def test_an_unparsable_location_names_the_problem(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        client, network = build(http_settings, fake_clock, redirecting(b"http://["))
        with pytest.raises(UnsupportedSchemeError, match="invalid URL in a redirect"):
            client.get(PAGE)
        assert network.paths == ["/robots.txt", "/events"]

    def test_a_location_the_http_library_rejects_is_a_failed_request(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        """httpx reads the Location of every redirect itself and gives up on some values."""
        client, network = build(http_settings, fake_clock, redirecting(b"//[::1"))
        with pytest.raises(HttpRequestError, match="RemoteProtocolError"):
            client.get(PAGE)
        assert network.paths == ["/robots.txt"] + ["/events"] * 4

    @pytest.mark.parametrize("location", BAD_LOCATIONS[:8], ids=ascii)
    def test_such_a_redirect_of_robots_txt_blocks_the_origin_for_the_run(
        self, http_settings: HttpSettings, fake_clock: FakeClock, tmp_path: Path, location: bytes
    ) -> None:
        handler = redirecting(location, robots_too=True)
        client, network = build(http_settings, fake_clock, handler, tmp_path)
        with pytest.raises(RobotsUnavailableError, match="host skipped"):
            client.get(f"{HOST}/a")
        sent = list(network.paths)
        assert sent in (["/robots.txt"], ["/robots.txt"] * 4)
        for path in ("/b", "/c"):
            with pytest.raises(RobotsUnavailableError, match="host skipped"):
                client.get(f"{HOST}{path}")
        assert network.paths == sent

    def test_the_target_of_a_redirect_is_worked_out_without_the_http_library(self) -> None:
        answer = Exchange(302, httpx.Headers({"Location": "//[::1"}), "")
        with pytest.raises(UnsupportedSchemeError, match="invalid URL in a redirect"):
            PoliteClient._redirect_target(PAGE, answer)
        relative = Exchange(307, httpx.Headers({"Location": "../x?y=1"}), "")
        assert PoliteClient._redirect_target(f"{PAGE}/all", relative) == f"{HOST}/x?y=1"
        assert PoliteClient._redirect_target(PAGE, Exchange(200, httpx.Headers(), "")) is None
        assert PoliteClient._redirect_target(PAGE, Exchange(302, httpx.Headers(), "")) is None


class TestValidators:
    """ETag and Last-Modified come back as request headers, which must be ASCII."""

    @pytest.mark.parametrize(
        "header",
        [
            (b"etag", b'"caf\xe9"'),
            (b"etag", '"caf\u00e9"'.encode()),
            (b"last-modified", "Mon, 01 Jan 2024 \u6708".encode()),
            (b"etag", b'"a\tb"'),
        ],
        ids=ascii,
    )
    def test_a_validator_that_cannot_be_sent_back_is_not_stored(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        tmp_path: Path,
        header: tuple[bytes, bytes],
    ) -> None:
        client, network = build(http_settings, fake_clock, page_with([header]), tmp_path)
        for _ in range(2):
            assert client.get(PAGE).text == "hi\n"
        assert not (tmp_path / "pages").exists()
        second = network.requests[-1]
        assert "if-none-match" not in second.headers
        assert "if-modified-since" not in second.headers

    def test_the_other_validator_is_still_used(
        self, http_settings: HttpSettings, fake_clock: FakeClock, tmp_path: Path
    ) -> None:
        stamp = b"Tue, 10 Nov 2026 08:00:00 GMT"
        headers = [(b"etag", b'"caf\xe9"'), (b"last-modified", stamp)]
        client, network = build(http_settings, fake_clock, page_with(headers), tmp_path)
        client.get(PAGE)
        client.get(PAGE)
        second = network.requests[-1]
        assert second.headers["if-modified-since"] == stamp.decode()
        assert "if-none-match" not in second.headers

    @pytest.mark.parametrize(
        "stored",
        [
            CachedPage(url=PAGE, etag=f'"caf{E_ACUTE}"', text="old"),
            CachedPage(url=PAGE, last_modified="Mon\n01 Jan", text="old"),
            CachedPage(url=PAGE, etag='"ok"', last_modified=f"1 janv. {E_ACUTE}", text="old"),
            CachedPage(url=PAGE, text="old"),
        ],
    )
    def test_a_stored_entry_with_such_a_validator_is_ignored(
        self, http_settings: HttpSettings, fake_clock: FakeClock, tmp_path: Path, stored: CachedPage
    ) -> None:
        """Such an entry made every later run fail on this URL until the file was deleted."""
        HttpCache(tmp_path).store_page(stored)
        assert HttpCache(tmp_path).page(PAGE) is None
        client, network = build(http_settings, fake_clock, page_with([]), tmp_path)
        assert client.get(PAGE).text == "hi\n"
        assert "if-none-match" not in network.requests[-1].headers
        assert "if-modified-since" not in network.requests[-1].headers


class TestDeclaredSize:
    @pytest.mark.parametrize("declared", ["9" * 5000, "1" + "0" * 30, "5000001", "0005000001"])
    def test_a_huge_declared_size_is_too_large_however_many_digits_it_has(
        self, http_settings: HttpSettings, fake_clock: FakeClock, declared: str
    ) -> None:
        """Python refuses to convert a number of more than 4300 digits."""
        handler = page_with([(b"content-length", declared.encode("ascii"))])
        client, _ = build(http_settings, fake_clock, handler)
        with pytest.raises(ResponseTooLargeError):
            client.get(PAGE)

    @pytest.mark.parametrize("declared", [b"\xb2", b"\xb9\xb2\xb3", b"-1", b"1e9", b"", b"2 "])
    def test_a_declared_size_that_is_no_number_is_ignored(
        self, http_settings: HttpSettings, fake_clock: FakeClock, declared: bytes
    ) -> None:
        """A superscript two is a digit to str.isdigit and not a number to int."""
        client, _ = build(http_settings, fake_clock, page_with([(b"content-length", declared)]))
        assert client.get(PAGE).text == "hi\n"


RETRY_AFTER_VALUES = ["nan", "NaN", "-nan", "1e400", "inf", "-inf", "-5", "99999999999", "0x10"]
RETRY_AFTER_VALUES += [
    "1_0",
    "",
    "Mon, 01 Jan 9999 00:00:00 GMT",
    "Fri, 31 Dec 9999 23:59:59 -2359",
]
RETRY_AFTER_VALUES += [
    "Mon, 01 Jan 0001 00:00:00 +2359",
    "1 Jan 1 00:00:00",
    "32 Jan 2024 00:00:00 GMT",
]
RETRY_AFTER_VALUES += ["1 Jan 99999 00:00:00 GMT", "Thu, 01 Jan 1970 00:00:00 -0000"]


class TestRetryAfter:
    @pytest.mark.parametrize("value", RETRY_AFTER_VALUES)
    def test_no_value_breaks_the_pause_between_two_attempts(
        self, http_settings: HttpSettings, fake_clock: FakeClock, value: str
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(503, headers={"Retry-After": value})

        client, network = build(http_settings, fake_clock, handler)
        with pytest.raises(HttpError):
            client.get(PAGE)
        assert len(network.requests) == 5
        assert len(fake_clock.sleeps) == 4
        assert all(1.0 <= pause <= 60.0 for pause in fake_clock.sleeps), fake_clock.sleeps


class Raising(httpx.BaseTransport):
    """A transport whose page requests fail with an exception of the test's choice."""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        self.calls += 1
        raise self._error


class TestTheLastNet:
    @pytest.mark.parametrize(
        "error",
        [
            ValueError("bad value"),
            OverflowError("connect(): port must be 0-65535"),
            OSError(36, "File name too long"),
            UnicodeEncodeError("ascii", "caf\u00e9", 3, 4, "ordinal not in range"),
            httpx.InvalidURL("Invalid IPv6 URL"),
            httpx.StreamClosed(),
            httpx.HTTPError("anything else of httpx"),
        ],
        ids=lambda error: type(error).__name__,
    )
    def test_an_error_nobody_foresaw_becomes_a_request_error(
        self, http_settings: HttpSettings, fake_clock: FakeClock, error: BaseException
    ) -> None:
        transport = Raising(error)
        client = PoliteClient(
            http_settings,
            clock=fake_clock,
            sleep=fake_clock.sleep,
            transport=transport,
            resolver=StubResolver(),
        )
        with pytest.raises(HttpRequestError, match=type(error).__name__) as caught:
            client.get(PAGE)
        assert caught.value.url == PAGE
        assert caught.value.__cause__ is error
        assert transport.calls == 1  # what is not understood is not retried

    def test_an_http_error_passes_unchanged(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        client, _ = build(http_settings, fake_clock, redirecting(b"ftp://example.edu/x"))
        with pytest.raises(UnsupportedSchemeError, match="only absolute http"):
            client.get(PAGE)

    def test_a_failed_assertion_is_not_swallowed(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        """The guards of the test suite raise AssertionError; they must stay visible."""
        client = PoliteClient(
            http_settings,
            clock=fake_clock,
            sleep=fake_clock.sleep,
            transport=Raising(AssertionError("a guard")),
            resolver=StubResolver(),
        )
        with pytest.raises(AssertionError, match="a guard"):
            client.get(PAGE)


class TestCacheDirectoryTrouble:
    """The cache is an optimisation: when the disk refuses, the request still happens."""

    def test_a_cache_directory_that_cannot_be_written_is_like_none(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        blocker = tmp_path / "cache"
        blocker.write_text("a file where the directory should be\n", encoding="utf-8")
        handler = page_with([(b"etag", b'"v1"')])
        client, network = build(http_settings, fake_clock, handler, blocker)
        with caplog.at_level(logging.WARNING, logger="seminars.httpcache"):
            for _ in range(3):
                assert client.get(PAGE).text == "hi\n"
        assert network.paths == ["/robots.txt", "/events", "/events", "/events"]
        assert blocker.read_text(encoding="utf-8").startswith("a file")
        warnings = [record.getMessage() for record in caplog.records]
        assert len(warnings) == 1
        assert "HTTP cache" in warnings[0]

    @pytest.mark.skipif(sys.platform == "win32", reason="there is no lock on Windows")
    def test_a_file_system_that_cannot_lock_is_like_no_lock(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def no_locks(descriptor: int, operation: int) -> None:
            raise OSError(errno.ENOLCK, "No locks available")

        monkeypatch.setattr("fcntl.flock", no_locks)
        client, network = build(http_settings, fake_clock, page_with([]), tmp_path)
        with caplog.at_level(logging.WARNING, logger="seminars.httpcache"):
            for _ in range(2):
                assert client.get(PAGE).text == "hi\n"
            with HttpCache(tmp_path).host_lock("dept.example.edu"):
                pass
        assert network.paths == ["/robots.txt", "/events", "/events"]
        assert [record.getMessage().count("No locks available") for record in caplog.records] == [
            1,
            1,
        ]
        assert HttpCache(tmp_path).host("dept.example.edu").last_request_at == fake_clock()

    @pytest.mark.skipif(sys.platform == "win32", reason="there is no lock on Windows")
    def test_a_lock_file_that_cannot_be_opened_is_like_no_lock(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        hosts = tmp_path / "hosts"
        (hosts / "dept.example.edu.lock").mkdir(parents=True)  # where the lock file should be
        cache = HttpCache(tmp_path)
        with caplog.at_level(logging.WARNING, logger="seminars.httpcache"):
            for _ in range(2):
                with cache.host_lock("dept.example.edu"):
                    cache.store_host("dept.example.edu", HostRecord(last_request_at=1.0))
        assert len(caplog.records) == 1
        assert "locking" in caplog.records[0].getMessage()
        assert cache.host("dept.example.edu").last_request_at == 1.0
        assert sorted(path.name for path in hosts.iterdir()) == [
            "dept.example.edu.json",
            "dept.example.edu.lock",
            "dept.example.edu.queue",
        ]

    def test_a_long_host_gets_a_short_file_name(self, tmp_path: Path) -> None:
        cache = HttpCache(tmp_path)
        host = ".".join(["a" * 60] * 4)  # 243 characters, a valid name
        other = ".".join(["a" * 60] * 3 + ["b" * 60])
        with cache.host_lock(host), cache.host_lock(other):
            cache.store_host(host, HostRecord(last_request_at=1.0))
            cache.store_host(other, HostRecord(last_request_at=2.0))
        assert cache.host(host).last_request_at == 1.0
        assert cache.host(other).last_request_at == 2.0
        names = sorted(path.name for path in (tmp_path / "hosts").iterdir())
        assert len(names) == 6  # a record, a lock and a queue for each of the two
        assert all(len(name) < 120 for name in names)

    def test_hosts_that_differ_only_in_odd_characters_do_not_share_a_file(
        self, tmp_path: Path
    ) -> None:
        cache = HttpCache(tmp_path)
        cache.store_host("2606:4700::1111", HostRecord(last_request_at=1.0))
        cache.store_host("2606_4700__1111", HostRecord(last_request_at=2.0))
        cache.store_host("dept.example.edu", HostRecord(last_request_at=3.0))
        assert cache.host("2606:4700::1111").last_request_at == 1.0
        assert cache.host("2606_4700__1111").last_request_at == 2.0
        assert (tmp_path / "hosts" / "dept.example.edu.json").is_file()

    def test_a_request_to_a_host_with_a_very_long_name_works_with_a_cache(
        self, http_settings: HttpSettings, fake_clock: FakeClock, tmp_path: Path
    ) -> None:
        host = ".".join(["a" * 60] * 4)
        client, _ = build(http_settings, fake_clock, page_with([(b"etag", b'"v1"')]), tmp_path)
        assert client.get(f"https://{host}/x").text == "hi\n"
        assert HttpCache(tmp_path).host(host).last_request_at == fake_clock()


PIECES = ["http://", "https://", "//", "/", ":", "@", "[", "]", "%", "..", "\\", "?", "#", " "]
PIECES += ["\t", "example.edu", "dept.example.edu", "127.0.0.1", "0x7f.1", "::1", "99999", "0"]
PIECES += ["a" * 70, "xn--", chr(0xE9), chr(0x3002), chr(0x2100), chr(0xD800), "\x00", "\x7f"]
PIECES += ["%2e%2e", "%00", "localhost"]
URL_PIECES = st.sampled_from(PIECES)
URLS = st.one_of(st.text(max_size=40), st.lists(URL_PIECES, max_size=8).map("".join))
HEADER_BYTES = st.one_of(
    st.binary(max_size=40),
    st.lists(URL_PIECES, max_size=8).map(lambda pieces: "".join(pieces).encode("utf-8", "replace")),
)


def attempt(client: PoliteClient, url: str) -> FetchedDocument | HttpError:
    """What ``get`` gives for ``url``; any exception that is not an ``HttpError`` propagates."""
    try:
        return client.get(url)
    except HttpError as error:
        return error


class TestAnyInput:
    @settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(url=URLS, location=HEADER_BYTES, validator=HEADER_BYTES, robots_too=st.booleans())
    def test_get_returns_a_document_or_raises_an_http_error(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        url: str,
        location: bytes,
        validator: bytes,
        robots_too: bool,
    ) -> None:
        def clean(value: bytes) -> bytes:
            return value.replace(b"\r", b"").replace(b"\n", b"")  # one header, not several

        def handler(request: httpx.Request) -> httpx.Response:
            first = len(seen) == 0
            seen.append(request.url.path)
            if request.url.path == "/robots.txt" and not robots_too:
                return httpx.Response(404)
            headers = [(b"etag", clean(validator)), (b"retry-after", clean(validator))]
            headers.append((b"content-length", clean(validator)))
            if first or robots_too:
                return httpx.Response(302, headers=[(b"location", clean(location)), *headers])
            return httpx.Response(200, headers=headers, content=b"ok")

        seen: list[str] = []
        clock = FakeClock()
        base = HttpSettings(user_agent="seminars-bot/1.0 (+https://example.org/seminars/)")
        client = PoliteClient(
            base.evolve(retries=1, max_redirects=2),
            cache_dir=tmp_path_factory.mktemp("cache"),
            clock=clock,
            sleep=clock.sleep,
            transport=httpx.MockTransport(handler),
            resolver=StubResolver(),
        )
        for target in (url, PAGE, PAGE):
            outcome = attempt(client, target)
            assert isinstance(outcome, HttpError) or outcome.url == target
            assert str(outcome).isascii() or not isinstance(outcome, HttpError)


class TestCommandLine:
    def test_a_hostile_url_does_not_stop_the_urls_after_it(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            if request.url.path == "/hostile":
                return httpx.Response(302, headers={"Location": "http://["})
            return httpx.Response(200, text="fine", headers={"Content-Type": "text/html"})

        for command in (["fetch"], ["record", "--source-id", "probe-src"]):
            client, network = build(http_settings, fake_clock, handler, tmp_path / "cache")
            urls = ["https://a.example.edu/hostile", "https://example.edu:99999/", f"{HOST}/fine"]
            assert main(["--root", str(tmp_path), *command, *urls], http=client) == 1
            captured = capsys.readouterr()
            assert captured.err.count("error: ") == 2
            assert ("fetched" in captured.out) or ("recorded" in captured.out)
            assert network.paths[-1] == "/fine"
