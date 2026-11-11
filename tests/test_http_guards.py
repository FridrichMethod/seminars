"""Tests for the guards of the polite HTTP client: robots redirects and body size."""

from __future__ import annotations

import gzip
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
import respx

from fakes import FakeClock, StubResolver
from seminars.config import HttpSettings
from seminars.http import (
    CrawlDelayTooLongError,
    HttpError,
    PoliteClient,
    ResponseTooLargeError,
    RobotsDisallowedError,
    RobotsUnavailableError,
)
from seminars.httpcache import CachedRobots, HostRecord, HttpCache

HOST = "https://dept.example.edu"
ROBOTS = f"{HOST}/robots.txt"
PAGE = f"{HOST}/events"


def no_robots(router: respx.MockRouter, host: str = HOST) -> respx.Route:
    return router.get(f"{host}/robots.txt").respond(404)


@pytest.fixture
def client(
    http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
) -> PoliteClient:
    return PoliteClient(http_settings, clock=fake_clock, sleep=fake_clock.sleep, resolver=resolver)


OTHER = "https://www.example.edu"


class TestRobotsRedirects:
    def test_redirect_on_the_same_host_is_followed(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(ROBOTS).respond(301, headers={"Location": "/static/robots.txt"})
        moved = respx_router.get(f"{HOST}/static/robots.txt").respond(
            200, text="User-agent: *\nDisallow: /private/\n"
        )
        respx_router.get(PAGE).respond(200, text="ok")
        with pytest.raises(RobotsDisallowedError):
            client.get(f"{HOST}/private/x")
        assert client.get(PAGE).text == "ok\n"
        assert moved.call_count == 1

    def test_redirect_to_another_host_is_followed_and_applies_to_the_first_host(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        respx_router.get(ROBOTS).respond(302, headers={"Location": f"{OTHER}/robots.txt"})
        target = respx_router.get(f"{OTHER}/robots.txt").respond(
            200, text="User-agent: *\nCrawl-delay: 30\nDisallow: /private/\n"
        )
        private = respx_router.get(f"{HOST}/private/x").respond(200, text="secret")
        respx_router.get(PAGE).respond(200, text="ok")
        with pytest.raises(RobotsDisallowedError):
            client.get(f"{HOST}/private/x")
        assert private.call_count == 0
        assert client.get(PAGE).text == "ok\n"
        assert target.call_count == 1
        assert fake_clock.sleeps == [30.0]
        assert client.request_counts() == {"dept.example.edu": 1, "www.example.edu": 0}

    def test_a_hop_waits_for_its_turn_on_the_host_it_goes_to(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        times: list[float] = []

        def other_robots(request: httpx.Request) -> httpx.Response:
            times.append(fake_clock())
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 30\n")

        respx_router.get(f"{OTHER}/robots.txt").mock(side_effect=other_robots)
        respx_router.get(f"{OTHER}/events").respond(200, text="ok")
        respx_router.get(ROBOTS).respond(302, headers={"Location": f"{OTHER}/robots.txt"})
        respx_router.get(PAGE).respond(200, text="ok")
        client.get(f"{OTHER}/events")
        used_other_at = fake_clock()
        client.get(PAGE)
        assert times[1] - used_other_at == 30.0

    @pytest.mark.parametrize(
        "location",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost/robots.txt",
            "http://2130706433/robots.txt",
            "ftp://dept.example.edu/robots.txt",
        ],
    )
    def test_redirect_to_an_address_that_must_not_be_fetched_blocks_the_host(
        self, client: PoliteClient, respx_router: respx.MockRouter, location: str
    ) -> None:
        robots = respx_router.get(ROBOTS).respond(302, headers={"Location": location})
        page = respx_router.get(PAGE).respond(200, text="ok")
        for _ in range(2):
            with pytest.raises(RobotsUnavailableError, match="must not be fetched") as caught:
                client.get(PAGE)
            assert str(caught.value).endswith(ROBOTS)
        assert respx_router.calls.call_count == 1
        assert robots.call_count == 1
        assert page.call_count == 0

    def test_redirect_loop_blocks_the_host(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        robots = respx_router.get(ROBOTS).respond(302, headers={"Location": "/robots.txt"})
        page = respx_router.get(PAGE).respond(200, text="ok")
        with pytest.raises(RobotsUnavailableError, match="redirects more than 5 times"):
            client.get(PAGE)
        assert robots.call_count == 6
        assert page.call_count == 0
        assert client.request_counts() == {"dept.example.edu": 0}

    def test_a_chain_within_the_limit_is_followed(
        self, http_settings: HttpSettings, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        client = PoliteClient(
            http_settings.evolve(max_redirects=2),
            clock=fake_clock,
            sleep=fake_clock.sleep,
            resolver=StubResolver(),
        )
        respx_router.get(ROBOTS).respond(301, headers={"Location": "/r1"})
        respx_router.get(f"{HOST}/r1").respond(307, headers={"Location": f"{HOST}/r2"})
        respx_router.get(f"{HOST}/r2").respond(200, text="User-agent: *\nDisallow: /events\n")
        with pytest.raises(RobotsDisallowedError):
            client.get(PAGE)

    def test_redirect_without_a_location_is_an_unusable_answer(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(ROBOTS).respond(302)
        with pytest.raises(RobotsUnavailableError, match="HTTP 302"):
            client.get(PAGE)

    def test_failure_behind_a_redirect_blocks_the_first_host(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(ROBOTS).respond(302, headers={"Location": f"{OTHER}/robots.txt"})
        target = respx_router.get(f"{OTHER}/robots.txt").respond(503)
        page = respx_router.get(PAGE).respond(200, text="ok")
        with pytest.raises(RobotsUnavailableError, match="HTTP 503") as caught:
            client.get(PAGE)
        assert str(caught.value).endswith(ROBOTS)
        assert target.call_count == 4
        assert page.call_count == 0


PLAIN = "http://dept.example.edu"
"""The same host as HOST over http: another origin, with a robots.txt of its own."""


class TestRobotsOfAnotherOriginOnAKnownHost:
    """What one origin of a host asked for also slows down the first visit to the next one."""

    def test_the_request_waits_for_the_crawl_delay_another_origin_asked_for(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        times: dict[str, float] = {}

        def at(name: str, response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
            def respond(request: httpx.Request) -> httpx.Response:
                times[name] = fake_clock()
                return response

            return respond

        respx_router.get(ROBOTS).mock(
            side_effect=at(
                "https robots", httpx.Response(200, text="User-agent: *\nCrawl-delay: 30\n")
            )
        )
        respx_router.get(PAGE).mock(side_effect=at("https page", httpx.Response(200, text="ok")))
        respx_router.get(f"{PLAIN}/robots.txt").mock(
            side_effect=at("http robots", httpx.Response(404))
        )
        respx_router.get(f"{PLAIN}/events").mock(
            side_effect=at("http page", httpx.Response(200, text="ok"))
        )
        client.get(PAGE)
        fake_clock.advance(1.0)
        client.get(f"{PLAIN}/events")
        start = times["https robots"]
        assert {name: moment - start for name, moment in times.items()} == {
            "https robots": 0.0,
            "https page": 30.0,
            "http robots": 60.0,
            "http page": 61.0,
        }

    def test_the_wait_is_capped_like_any_crawl_delay(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        respx_router.get(ROBOTS).respond(200, text="User-agent: *\nCrawl-delay: 3600\n")
        respx_router.get(f"{PLAIN}/robots.txt").respond(404)
        respx_router.get(f"{PLAIN}/events").respond(200, text="ok")
        with pytest.raises(CrawlDelayTooLongError):
            client.get(PAGE)
        assert fake_clock.sleeps == []
        client.get(f"{PLAIN}/events")
        assert fake_clock.sleeps == [120.0, 1.0]

    def test_the_longest_known_delay_of_the_host_counts(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        other_port = "https://dept.example.edu:8443"
        respx_router.get(f"{other_port}/robots.txt").respond(404)  # first: ROBOTS fits any port
        respx_router.get(ROBOTS).respond(200, text="User-agent: *\nCrawl-delay: 10\n")
        respx_router.get(f"{PLAIN}/robots.txt").respond(
            200, text="User-agent: *\nCrawl-delay: 45\n"
        )
        respx_router.get(url__regex=r".*/events$").respond(200, text="ok")
        client.get(PAGE)
        client.get(f"{PLAIN}/events")
        assert fake_clock.sleeps == [10.0, 10.0, 45.0]
        client.get(f"{other_port}/events")
        assert fake_clock.sleeps == [10.0, 10.0, 45.0, 45.0, 1.0]

    def test_a_delay_another_client_recorded_in_the_cache_directory_counts(
        self,
        http_settings: HttpSettings,
        respx_router: respx.MockRouter,
        fake_clock: FakeClock,
        tmp_path: Path,
    ) -> None:
        respx_router.get(ROBOTS).respond(200, text="User-agent: *\nCrawl-delay: 30\n")
        respx_router.get(PAGE).respond(200, text="ok")
        respx_router.get(f"{PLAIN}/robots.txt").respond(404)
        respx_router.get(f"{PLAIN}/events").respond(200, text="ok")

        def new_client() -> PoliteClient:
            return PoliteClient(
                http_settings,
                cache_dir=tmp_path,
                clock=fake_clock,
                sleep=fake_clock.sleep,
                resolver=StubResolver(),
            )

        new_client().get(PAGE)
        assert fake_clock.sleeps == [30.0]
        fake_clock.advance(5.0)
        new_client().get(f"{PLAIN}/events")
        assert fake_clock.sleeps == [30.0, 25.0, 1.0]

    def test_an_outdated_copy_in_the_cache_directory_does_not_count(
        self,
        http_settings: HttpSettings,
        respx_router: respx.MockRouter,
        fake_clock: FakeClock,
        tmp_path: Path,
    ) -> None:
        respx_router.get(ROBOTS).respond(200, text="User-agent: *\nCrawl-delay: 30\n")
        respx_router.get(PAGE).respond(200, text="ok")
        respx_router.get(f"{PLAIN}/robots.txt").respond(404)
        respx_router.get(f"{PLAIN}/events").respond(200, text="ok")
        fake_clock.advance(http_settings.robots_cache_seconds + 1.0)
        old_copy = CachedRobots(
            fetched_at=fake_clock() - http_settings.robots_cache_seconds - 1.0,
            text="User-agent: *\nCrawl-delay: 30\n",
        )
        HttpCache(tmp_path).store_host(
            "dept.example.edu", HostRecord(last_request_at=fake_clock(), robots={HOST: old_copy})
        )
        client = PoliteClient(
            http_settings,
            cache_dir=tmp_path,
            clock=fake_clock,
            sleep=fake_clock.sleep,
            resolver=StubResolver(),
        )
        client.get(f"{PLAIN}/events")
        assert fake_clock.sleeps == [1.0, 1.0]

    def test_a_missing_robots_file_of_another_origin_asks_for_nothing(
        self,
        http_settings: HttpSettings,
        respx_router: respx.MockRouter,
        fake_clock: FakeClock,
        tmp_path: Path,
    ) -> None:
        respx_router.get(f"{PLAIN}/robots.txt").respond(404)
        respx_router.get(f"{PLAIN}/events").respond(200, text="ok")
        HttpCache(tmp_path).store_host(
            "dept.example.edu",
            HostRecord(
                last_request_at=fake_clock(),
                robots={HOST: CachedRobots(fetched_at=fake_clock(), text=None)},
            ),
        )
        client = PoliteClient(
            http_settings,
            cache_dir=tmp_path,
            clock=fake_clock,
            sleep=fake_clock.sleep,
            resolver=StubResolver(),
        )
        client.get(f"{PLAIN}/events")
        assert fake_clock.sleeps == [1.0, 1.0]


class TestBodyLimit:
    @pytest.fixture
    def small(self, http_settings: HttpSettings, fake_clock: FakeClock) -> PoliteClient:
        settings = http_settings.evolve(max_body_bytes=1000)
        return PoliteClient(
            settings, clock=fake_clock, sleep=fake_clock.sleep, resolver=StubResolver()
        )

    def test_default_limit(self, http_settings: HttpSettings) -> None:
        assert http_settings.max_body_bytes == 5_000_000

    def test_a_body_up_to_the_limit_is_read(
        self, small: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        no_robots(respx_router)
        respx_router.get(PAGE).respond(200, content=b"x" * 1000)
        assert len(small.get(PAGE).text) == 1001

    def test_a_declared_length_over_the_limit_is_refused_without_retries(
        self, small: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        no_robots(respx_router)
        page = respx_router.get(PAGE).respond(200, content=b"x" * 1001)
        with pytest.raises(ResponseTooLargeError, match="larger than 1000 bytes") as caught:
            small.get(PAGE)
        assert caught.value.limit == 1000
        assert isinstance(caught.value, HttpError)
        assert page.call_count == 1

    def test_reading_stops_when_an_undeclared_body_grows_over_the_limit(
        self, http_settings: HttpSettings
    ) -> None:
        produced = 0

        def endless() -> Iterator[bytes]:
            nonlocal produced
            while True:
                produced += 1
                yield b"x" * 400

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, content=endless())

        clock = FakeClock()
        settings = http_settings.evolve(max_body_bytes=1000)
        with (
            PoliteClient(
                settings,
                clock=clock,
                sleep=clock.sleep,
                resolver=StubResolver(),
                transport=httpx.MockTransport(handler),
            ) as client,
            pytest.raises(ResponseTooLargeError),
        ):
            client.get(PAGE)
        assert produced == 3

    def test_the_limit_counts_the_decompressed_body(
        self, small: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        packed = gzip.compress(b"0" * 100_000)
        assert len(packed) < 1000
        no_robots(respx_router)
        respx_router.get(PAGE).respond(
            200, content=packed, headers={"Content-Encoding": "gzip", "Content-Type": "text/html"}
        )
        with pytest.raises(ResponseTooLargeError):
            small.get(PAGE)

    def test_an_oversized_robots_file_blocks_the_host(
        self, small: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        robots = respx_router.get(ROBOTS).respond(200, content=b"# padding\n" * 200)
        page = respx_router.get(PAGE).respond(200, text="ok")
        for _ in range(2):
            with pytest.raises(RobotsUnavailableError, match="larger than 1000 bytes"):
                small.get(PAGE)
        assert robots.call_count == 1
        assert page.call_count == 0
