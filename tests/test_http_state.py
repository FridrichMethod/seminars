"""Tests for the polite HTTP client: conditional requests, redirects, persistence, threads."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
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
from seminars.httpcache import CachedPage, HttpCache

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
