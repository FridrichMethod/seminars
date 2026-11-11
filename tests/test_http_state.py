"""Tests for the polite HTTP client: conditional requests, redirects, persistence, threads."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path

import httpx
import pytest
import respx

from fakes import FakeClock
from seminars.config import HttpSettings
from seminars.http import (
    HttpStatusError,
    PoliteClient,
    RobotsDisallowedError,
    TooManyRedirectsError,
    UnsupportedSchemeError,
)
from seminars.httpcache import CachedPage, CachedRobots, HostRecord, HttpCache

HOST = "https://dept.example.edu"
PAGE = f"{HOST}/events"


def make_client(settings: HttpSettings, clock: FakeClock, cache_dir: Path | None) -> PoliteClient:
    return PoliteClient(settings, cache_dir=cache_dir, clock=clock, sleep=clock.sleep)


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / ".cache" / "http"


class TestConditionalRequests:
    def test_etag_is_revalidated_and_the_cached_body_is_served(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        respx_router: respx.MockRouter,
        cache_dir: Path,
    ) -> None:
        respx_router.get(f"{HOST}/robots.txt").respond(404)
        page = respx_router.get(PAGE).mock(
            side_effect=[
                httpx.Response(
                    200,
                    text="<p>first</p>",
                    headers={"ETag": '"v1"', "Content-Type": "text/html"},
                ),
                httpx.Response(304),
            ]
        )
        client = make_client(http_settings, fake_clock, cache_dir)
        first = client.get(PAGE)
        second = client.get(PAGE)
        assert "if-none-match" not in page.calls[0].request.headers
        assert page.calls[1].request.headers["if-none-match"] == '"v1"'
        assert (first.text, first.from_cache) == ("<p>first</p>\n", False)
        assert (second.text, second.from_cache) == ("<p>first</p>\n", True)
        assert (second.status, second.content_type) == (200, "text/html")

    def test_last_modified_is_revalidated_by_a_new_client(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        respx_router: respx.MockRouter,
        cache_dir: Path,
    ) -> None:
        stamp = "Tue, 10 Nov 2026 08:00:00 GMT"
        respx_router.get(f"{HOST}/robots.txt").respond(404)
        page = respx_router.get(PAGE).mock(
            side_effect=[
                httpx.Response(200, text="body", headers={"Last-Modified": stamp}),
                httpx.Response(304),
            ]
        )
        make_client(http_settings, fake_clock, cache_dir).get(PAGE)
        document = make_client(http_settings, fake_clock, cache_dir).get(PAGE)
        assert page.calls[1].request.headers["if-modified-since"] == stamp
        assert (document.text, document.from_cache) == ("body\n", True)

    def test_changed_content_replaces_the_cache(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        respx_router: respx.MockRouter,
        cache_dir: Path,
    ) -> None:
        respx_router.get(f"{HOST}/robots.txt").respond(404)
        respx_router.get(PAGE).mock(
            side_effect=[
                httpx.Response(200, text="old", headers={"ETag": '"v1"'}),
                httpx.Response(200, text="new", headers={"ETag": '"v2"'}),
            ]
        )
        client = make_client(http_settings, fake_clock, cache_dir)
        client.get(PAGE)
        assert client.get(PAGE).text == "new\n"
        cached = HttpCache(cache_dir).page(PAGE)
        assert cached is not None
        assert (cached.etag, cached.text) == ('"v2"', "new")

    def test_responses_without_validators_are_not_cached(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        respx_router: respx.MockRouter,
        cache_dir: Path,
    ) -> None:
        respx_router.get(f"{HOST}/robots.txt").respond(404)
        page = respx_router.get(PAGE).respond(200, text="plain")
        client = make_client(http_settings, fake_clock, cache_dir)
        client.get(PAGE)
        client.get(PAGE)
        assert "if-none-match" not in page.calls[1].request.headers
        assert HttpCache(cache_dir).page(PAGE) is None

    def test_unexpected_304_is_an_error(
        self, http_settings: HttpSettings, fake_clock: FakeClock, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(f"{HOST}/robots.txt").respond(404)
        respx_router.get(PAGE).respond(304)
        with pytest.raises(HttpStatusError) as caught:
            make_client(http_settings, fake_clock, None).get(PAGE)
        assert caught.value.status == 304

    def test_corrupt_cache_files_are_ignored(self, cache_dir: Path) -> None:
        cache = HttpCache(cache_dir)
        cache.store_page(CachedPage(url=PAGE, etag='"v1"', text="body"))
        (stored,) = (cache_dir / "pages").iterdir()
        stored.write_text("{not json", encoding="utf-8")
        assert cache.page(PAGE) is None
        assert HttpCache(None).page(PAGE) is None


class TestRedirects:
    def test_redirects_are_followed_and_reported(
        self, http_settings: HttpSettings, fake_clock: FakeClock, respx_router: respx.MockRouter
    ) -> None:
        target = "https://www.example.edu/events/all"
        respx_router.get(f"{HOST}/robots.txt").respond(404)
        respx_router.get("https://www.example.edu/robots.txt").respond(404)
        respx_router.get(PAGE).respond(301, headers={"Location": target})
        respx_router.get(target).respond(200, text="moved here")
        document = make_client(http_settings, fake_clock, None).get(PAGE)
        assert (document.url, document.final_url, document.text) == (PAGE, target, "moved here\n")

    def test_relative_redirect(
        self, http_settings: HttpSettings, fake_clock: FakeClock, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(f"{HOST}/robots.txt").respond(404)
        respx_router.get(PAGE).respond(302, headers={"Location": "/events/upcoming"})
        respx_router.get(f"{HOST}/events/upcoming").respond(200, text="ok")
        document = make_client(http_settings, fake_clock, None).get(PAGE)
        assert document.final_url == f"{HOST}/events/upcoming"

    def test_redirect_target_obeys_its_own_robots(
        self, http_settings: HttpSettings, fake_clock: FakeClock, respx_router: respx.MockRouter
    ) -> None:
        target = "https://other.example.org/private/events"
        respx_router.get(f"{HOST}/robots.txt").respond(404)
        respx_router.get("https://other.example.org/robots.txt").respond(
            200, text="User-agent: *\nDisallow: /private/\n"
        )
        respx_router.get(PAGE).respond(302, headers={"Location": target})
        final = respx_router.get(target).respond(200, text="secret")
        with pytest.raises(RobotsDisallowedError):
            make_client(http_settings, fake_clock, None).get(PAGE)
        assert final.call_count == 0

    def test_redirect_to_another_scheme_is_refused(
        self, http_settings: HttpSettings, fake_clock: FakeClock, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(f"{HOST}/robots.txt").respond(404)
        respx_router.get(PAGE).respond(302, headers={"Location": "ftp://dept.example.edu/x"})
        with pytest.raises(UnsupportedSchemeError):
            make_client(http_settings, fake_clock, None).get(PAGE)

    def test_redirect_loops_end(
        self, http_settings: HttpSettings, fake_clock: FakeClock, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(f"{HOST}/robots.txt").respond(404)
        loop = respx_router.get(PAGE).respond(302, headers={"Location": PAGE})
        with pytest.raises(TooManyRedirectsError, match="more than 5 redirects"):
            make_client(http_settings, fake_clock, None).get(PAGE)
        assert loop.call_count == 6


class TestPersistence:
    def test_crawl_delay_holds_across_clients(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        respx_router: respx.MockRouter,
        cache_dir: Path,
    ) -> None:
        robots = respx_router.get(f"{HOST}/robots.txt").respond(
            200, text="User-agent: *\nCrawl-delay: 30\n"
        )
        respx_router.get(PAGE).respond(200, text="ok")
        make_client(http_settings, fake_clock, cache_dir).get(PAGE)
        assert fake_clock.sleeps == [30.0]
        fake_clock.advance(10.0)
        make_client(http_settings, fake_clock, cache_dir).get(PAGE)
        assert fake_clock.sleeps == [30.0, 20.0]
        assert robots.call_count == 1

    def test_robots_is_fetched_again_when_the_cached_copy_is_old(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        respx_router: respx.MockRouter,
        cache_dir: Path,
    ) -> None:
        robots = respx_router.get(f"{HOST}/robots.txt").respond(404)
        respx_router.get(PAGE).respond(200, text="ok")
        make_client(http_settings, fake_clock, cache_dir).get(PAGE)
        fake_clock.advance(http_settings.robots_cache_seconds + 1)
        make_client(http_settings, fake_clock, cache_dir).get(PAGE)
        assert robots.call_count == 2

    def test_without_a_cache_directory_nothing_is_written(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        respx_router: respx.MockRouter,
        tmp_path: Path,
    ) -> None:
        respx_router.get(f"{HOST}/robots.txt").respond(404)
        respx_router.get(PAGE).respond(200, text="ok", headers={"ETag": '"v1"'})
        make_client(http_settings, fake_clock, None).get(PAGE)
        assert list(tmp_path.iterdir()) == []


class _ConcurrencyProbe:
    """Transport handler that tracks how many requests per host overlap."""

    def __init__(self, barrier: threading.Barrier | None = None) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, int] = defaultdict(int)
        self._barrier = barrier
        self.max_active: dict[str, int] = defaultdict(int)
        self.max_total = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        with self._lock:
            self._active[host] += 1
            self.max_active[host] = max(self.max_active[host], self._active[host])
            self.max_total = max(self.max_total, sum(self._active.values()))
        try:
            if self._barrier is not None:
                self._barrier.wait(timeout=5)
            else:
                time.sleep(0.01)
        finally:
            with self._lock:
                self._active[host] -= 1
        return httpx.Response(200, text="ok")


class TestThreads:
    def test_requests_to_one_host_never_overlap(self, http_settings: HttpSettings) -> None:
        clock = FakeClock()
        probe = _ConcurrencyProbe()
        settings = http_settings.evolve(max_requests_per_host=50)
        with (
            PoliteClient(
                settings, clock=clock, sleep=clock.sleep, transport=httpx.MockTransport(probe)
            ) as client,
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            urls = [f"{HOST}/events?page={number}" for number in range(16)]
            documents = list(pool.map(client.get, urls))
        assert [document.text for document in documents] == ["ok\n"] * 16
        assert probe.max_active["dept.example.edu"] == 1
        assert client.request_counts() == {"dept.example.edu": 16}
        assert len(clock.sleeps) == 16

    def test_different_hosts_are_fetched_concurrently(self, http_settings: HttpSettings) -> None:
        clock = FakeClock()
        probe = _ConcurrencyProbe(barrier=threading.Barrier(2))
        with (
            PoliteClient(
                http_settings, clock=clock, sleep=clock.sleep, transport=httpx.MockTransport(probe)
            ) as client,
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            urls = ["https://a.example.edu/events", "https://b.example.edu/events"]
            documents = list(pool.map(client.get, urls))
        assert [document.text for document in documents] == ["ok\n", "ok\n"]
        assert probe.max_total == 2
        assert probe.max_active == {"a.example.edu": 1, "b.example.edu": 1}


posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="the per-host lock between processes needs flock"
)

TRY_LOCK = """\
import fcntl
import os
import sys

descriptor = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("busy")
else:
    print("free")
"""
"""Reports whether the lock file given as the argument can be taken right now."""

RECORDER = """\
import sys
import time
from pathlib import Path

import httpx

from seminars.config import HttpSettings
from seminars.http import PoliteClient

cache_dir, log = Path(sys.argv[1]), Path(sys.argv[2])


def handler(request):
    begin = time.time()
    time.sleep(0.05)
    robots = request.url.path == "/robots.txt"
    with log.open("a", encoding="utf-8") as stream:
        stream.write(f"{request.url.path} {begin!r} {time.time()!r}\\n")
    return httpx.Response(200, text="User-agent: *\\nCrawl-delay: 0.3\\n" if robots else "ok")


settings = HttpSettings(user_agent="seminars-bot/1.0 (+https://example.org/)")
settings = settings.evolve(min_interval_seconds=0.0)
with PoliteClient(settings, cache_dir=cache_dir, transport=httpx.MockTransport(handler)) as client:
    print("ready", flush=True)
    sys.stdin.readline()
    client.get("https://dept.example.edu/events")
"""
"""One fixture recorder run: a live client with the real clock, a fake network and a log."""


class TestHostRecord:
    def test_merge_keeps_the_later_request_and_the_newer_robots(self) -> None:
        origin, other_origin = "https://dept.example.edu", "http://dept.example.edu"
        mine = HostRecord(
            last_request_at=100.0,
            robots={origin: CachedRobots(fetched_at=50.0, text="old")},
        )
        theirs = HostRecord(
            last_request_at=130.0,
            robots={
                origin: CachedRobots(fetched_at=90.0, text="new"),
                other_origin: CachedRobots(fetched_at=10.0),
            },
        )
        merged = mine.merge(theirs)
        assert merged.last_request_at == 130.0
        assert merged.robots[origin].text == "new"
        assert merged.robots[other_origin].text is None
        assert theirs.merge(mine) == merged
        assert mine.merge(HostRecord()) == mine
        assert HostRecord().merge(HostRecord()) == HostRecord()


class TestSharedCacheDirectory:
    def test_two_clients_at_once_take_turns_and_keep_the_crawl_delay(
        self, http_settings: HttpSettings, cache_dir: Path
    ) -> None:
        clock = FakeClock()
        guard = threading.Lock()
        active = 0
        max_active = 0
        seen: list[tuple[str, float]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
                seen.append((request.url.path, clock()))
            time.sleep(0.02)
            with guard:
                active -= 1
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nCrawl-delay: 30\n")
            return httpx.Response(200, text="ok")

        start = threading.Barrier(2)

        def run_once() -> str:
            with PoliteClient(
                http_settings,
                cache_dir=cache_dir,
                clock=clock,
                sleep=clock.sleep,
                transport=httpx.MockTransport(handler),
            ) as client:
                start.wait(timeout=5)
                return client.get(PAGE).text

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run_once) for _ in range(2)]
            texts = [future.result(timeout=10) for future in futures]
        assert texts == ["ok\n", "ok\n"]
        assert max_active == 1
        assert [path for path, _ in seen] == ["/robots.txt", "/events", "/events"]
        first = seen[0][1]
        assert [moment - first for _, moment in seen] == [0.0, 30.0, 60.0]

    def test_a_client_sees_requests_other_clients_made_after_it_started(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        respx_router: respx.MockRouter,
        cache_dir: Path,
    ) -> None:
        respx_router.get(f"{HOST}/robots.txt").respond(200, text="User-agent: *\nCrawl-delay: 30\n")
        respx_router.get(PAGE).respond(200, text="ok")
        early = make_client(http_settings, fake_clock, cache_dir)
        early.get(PAGE)
        fake_clock.advance(100.0)
        make_client(http_settings, fake_clock, cache_dir).get(PAGE)
        fake_clock.advance(10.0)
        early.get(PAGE)
        assert fake_clock.sleeps == [30.0, 20.0]

    def test_clients_with_different_directories_do_not_see_each_other(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        respx_router: respx.MockRouter,
        tmp_path: Path,
    ) -> None:
        """Why worktrees need SEMINARS_HTTP_CACHE_DIR: this is the unprotected case."""
        respx_router.get(f"{HOST}/robots.txt").respond(200, text="User-agent: *\nCrawl-delay: 30\n")
        respx_router.get(PAGE).respond(200, text="ok")
        make_client(http_settings, fake_clock, tmp_path / "one").get(PAGE)
        make_client(http_settings, fake_clock, tmp_path / "two").get(PAGE)
        assert fake_clock.sleeps == [30.0, 30.0]

    @posix_only
    def test_host_lock_excludes_other_processes(self, cache_dir: Path) -> None:
        cache = HttpCache(cache_dir)

        def probe() -> str:
            (lock_file,) = (cache_dir / "hosts").glob("*.lock")
            command = [sys.executable, "-c", TRY_LOCK, str(lock_file)]
            return subprocess.run(command, capture_output=True, text=True, check=True).stdout

        with cache.host_lock("Dept.Example.edu"):
            assert probe() == "busy\n"
        assert probe() == "free\n"
        assert [path.name for path in (cache_dir / "hosts").iterdir()] == ["dept.example.edu.lock"]

    def test_host_lock_is_released_when_the_block_fails(self, cache_dir: Path) -> None:
        cache = HttpCache(cache_dir)
        with pytest.raises(RuntimeError, match="boom"), cache.host_lock("dept.example.edu"):
            raise RuntimeError("boom")
        with cache.host_lock("dept.example.edu"):
            pass

    def test_no_lock_file_without_a_cache_directory(self, tmp_path: Path) -> None:
        with HttpCache(None).host_lock("dept.example.edu"):
            pass
        assert list(tmp_path.iterdir()) == []

    @posix_only
    def test_two_processes_started_together_do_not_overlap(self, tmp_path: Path) -> None:
        """Two parallel fixture recorder runs for one host, as two real processes."""
        log = tmp_path / "requests.log"
        command = [sys.executable, "-c", RECORDER, str(tmp_path / "http"), str(log)]
        runs = [
            subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        try:
            for run in runs:
                assert run.stdout is not None
                assert run.stdout.readline() == "ready\n"
            for run in runs:
                assert run.stdin is not None
                run.stdin.write("go\n")
                run.stdin.flush()
            assert [run.wait(timeout=30) for run in runs] == [0, 0]
        finally:
            for run in runs:
                run.kill()
                run.communicate()
        requests = [line.split() for line in log.read_text(encoding="utf-8").splitlines()]
        spans = sorted((float(begin), float(end), path) for path, begin, end in requests)
        assert [path for _, _, path in spans] == ["/robots.txt", "/events", "/events"]
        for (_, previous_end, _), (begin, _, _) in pairwise(spans):
            assert begin - previous_end >= 0.3 - 0.01
