"""Tests for the polite HTTP client: robots, rate limit, budget and retries."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
import respx

from fakes import FakeClock
from seminars.config import HttpSettings
from seminars.http import (
    CrawlDelayTooLongError,
    HostBudgetExceededError,
    HttpClient,
    HttpError,
    HttpRequestError,
    HttpStatusError,
    PoliteClient,
    RobotsDisallowedError,
    RobotsUnavailableError,
    UnsupportedContentTypeError,
    UnsupportedSchemeError,
    is_binary_media_type,
    media_type,
    split_url,
)

HOST = "https://dept.example.edu"
ROBOTS = f"{HOST}/robots.txt"
PAGE = f"{HOST}/events"

Responder = Callable[[httpx.Request], httpx.Response]


def timed(clock: FakeClock, times: list[float], response: httpx.Response) -> Responder:
    """A respx side effect that records the fake time of each call."""

    def respond(request: httpx.Request) -> httpx.Response:
        times.append(clock())
        return response

    return respond


def no_robots(router: respx.MockRouter, host: str = HOST) -> respx.Route:
    return router.get(f"{host}/robots.txt").respond(404)


@pytest.fixture
def client(http_settings: HttpSettings, fake_clock: FakeClock) -> PoliteClient:
    return PoliteClient(http_settings, clock=fake_clock, sleep=fake_clock.sleep)


class TestBasics:
    def test_returns_a_fetched_document(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        no_robots(respx_router)
        respx_router.get(PAGE).respond(
            200, text="<html>hi</html>", headers={"Content-Type": "Text/HTML; charset=UTF-8"}
        )
        document = client.get(PAGE)
        assert document.url == PAGE
        assert document.final_url == PAGE
        assert document.status == 200
        assert document.content_type == "text/html"
        assert document.text == "<html>hi</html>"
        assert document.fetched_at == fake_clock.now()
        assert document.from_cache is False
        assert isinstance(client, HttpClient)

    def test_user_agent_is_fixed(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        robots = no_robots(respx_router)
        page = respx_router.get(PAGE).respond(200, text="ok")
        client.get(PAGE, headers={"User-Agent": "evil/1.0", "Accept": "application/json"})
        expected = "seminars-bot/1.0 (+https://example.org/seminars/)"
        assert robots.calls.last.request.headers["user-agent"] == expected
        assert page.calls.last.request.headers["user-agent"] == expected
        assert page.calls.last.request.headers["accept"] == "application/json"

    def test_timeout_comes_from_the_settings(self, http_settings: HttpSettings) -> None:
        seen: list[dict[str, float]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.extensions["timeout"])
            return httpx.Response(404 if request.url.path == "/robots.txt" else 200, text="ok")

        clock = FakeClock()
        with PoliteClient(
            http_settings, clock=clock, sleep=clock.sleep, transport=httpx.MockTransport(handler)
        ) as client:
            client.get(PAGE)
        assert all(timeout["read"] == 20.0 and timeout["connect"] == 20.0 for timeout in seen)

    @pytest.mark.parametrize(
        "url",
        ["ftp://dept.example.edu/file", "file:///etc/passwd", "/events", "dept.example.edu", ""],
    )
    def test_only_http_and_https(
        self, client: PoliteClient, respx_router: respx.MockRouter, url: str
    ) -> None:
        with pytest.raises(UnsupportedSchemeError, match="only absolute http"):
            client.get(url)
        assert respx_router.calls.call_count == 0

    def test_error_status_raises(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        no_robots(respx_router)
        page = respx_router.get(PAGE).respond(404, text="gone")
        with pytest.raises(HttpStatusError) as caught:
            client.get(PAGE)
        assert caught.value.status == 404
        assert caught.value.url == PAGE
        assert page.call_count == 1

    def test_allowed_error_status_is_returned(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        no_robots(respx_router)
        respx_router.get(PAGE).respond(404, text="gone")
        document = client.get(PAGE, allow_statuses={404})
        assert (document.status, document.text) == (404, "gone")

    @pytest.mark.parametrize("content_type", ["image/png", "application/pdf", "video/mp4"])
    def test_binary_content_is_refused(
        self, client: PoliteClient, respx_router: respx.MockRouter, content_type: str
    ) -> None:
        no_robots(respx_router)
        respx_router.get(PAGE).respond(
            200, content=b"\x89PNG", headers={"Content-Type": content_type}
        )
        with pytest.raises(UnsupportedContentTypeError) as caught:
            client.get(PAGE)
        assert caught.value.content_type == content_type

    def test_error_messages_are_ascii(self, client: PoliteClient) -> None:
        url = "ftp://dept.example.edu/caf" + chr(0xE9)
        with pytest.raises(HttpError) as caught:
            client.get(url)
        assert str(caught.value).isascii()
        assert "caf\\xe9" in str(caught.value)

    def test_helpers(self) -> None:
        assert media_type(None) is None
        assert media_type("application/vnd.api+json; charset=utf-8") == "application/vnd.api+json"
        assert is_binary_media_type("image/svg+xml")
        assert not is_binary_media_type("text/calendar")
        assert not is_binary_media_type(None)
        assert split_url("HTTPS://Dept.Example.edu/a?b=1#frag") == (
            "https://dept.example.edu",
            "dept.example.edu",
            "/a?b=1",
        )


class TestRobots:
    def test_disallow_is_honoured(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(ROBOTS).respond(200, text="User-agent: *\nDisallow: /private/\n")
        private = respx_router.get(f"{HOST}/private/page").respond(200, text="secret")
        respx_router.get(PAGE).respond(200, text="ok")
        with pytest.raises(RobotsDisallowedError, match=r"disallowed by robots\.txt"):
            client.get(f"{HOST}/private/page")
        assert private.call_count == 0
        assert client.get(PAGE).text == "ok"

    def test_rules_for_our_agent_are_used(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        text = "User-agent: *\nDisallow:\n\nUser-agent: seminars-bot\nDisallow: /\n"
        respx_router.get(ROBOTS).respond(200, text=text)
        with pytest.raises(RobotsDisallowedError):
            client.get(PAGE)

    def test_robots_is_fetched_once_per_host(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        robots = respx_router.get(ROBOTS).respond(200, text="User-agent: *\nDisallow: /x\n")
        respx_router.get(url__startswith=f"{HOST}/events").respond(200, text="ok")
        for page in range(3):
            client.get(f"{PAGE}?page={page}")
        assert robots.call_count == 1

    def test_missing_robots_allows_everything(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(ROBOTS).respond(403)
        respx_router.get(PAGE).respond(200, text="ok")
        assert client.get(PAGE).text == "ok"

    @pytest.mark.parametrize("status", [500, 503, 429])
    def test_unreachable_robots_blocks_the_host(
        self, client: PoliteClient, respx_router: respx.MockRouter, status: int
    ) -> None:
        robots = respx_router.get(ROBOTS).respond(status)
        page = respx_router.get(PAGE).respond(200, text="ok")
        with pytest.raises(RobotsUnavailableError, match=f"HTTP {status}"):
            client.get(PAGE)
        assert robots.call_count == 4
        assert page.call_count == 0

    def test_network_error_on_robots_blocks_the_host(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(ROBOTS).mock(side_effect=httpx.ConnectError("boom"))
        page = respx_router.get(PAGE).respond(200, text="ok")
        with pytest.raises(RobotsUnavailableError, match="unreachable"):
            client.get(PAGE)
        assert page.call_count == 0


class TestRateLimit:
    def test_crawl_delay_is_honoured(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        times: list[float] = []
        robots_text = "User-agent: *\nCrawl-delay: 30\n"
        respx_router.get(ROBOTS).mock(
            side_effect=timed(fake_clock, times, httpx.Response(200, text=robots_text))
        )
        respx_router.get(url__startswith=PAGE).mock(
            side_effect=timed(fake_clock, times, httpx.Response(200, text="ok"))
        )
        for page in range(3):
            client.get(f"{PAGE}?page={page}")
        start = times[0]
        assert [moment - start for moment in times] == [0.0, 30.0, 60.0, 90.0]
        assert fake_clock.sleeps == [30.0, 30.0, 30.0]

    def test_minimum_interval_applies_without_crawl_delay(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        no_robots(respx_router)
        respx_router.get(url__startswith=PAGE).respond(200, text="ok")
        client.get(f"{PAGE}?page=1")
        client.get(f"{PAGE}?page=2")
        assert fake_clock.sleeps == [1.0, 1.0]

    def test_interval_is_the_larger_of_minimum_and_crawl_delay(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        respx_router.get(ROBOTS).respond(200, text="User-agent: *\nCrawl-delay: 0.2\n")
        respx_router.get(PAGE).respond(200, text="ok")
        client.get(PAGE)
        assert fake_clock.sleeps == [1.0]

    def test_time_already_spent_counts_towards_the_interval(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        respx_router.get(ROBOTS).respond(200, text="User-agent: *\nCrawl-delay: 30\n")
        respx_router.get(PAGE).respond(200, text="ok")
        client.get(PAGE)
        fake_clock.advance(12.0)
        client.get(PAGE)
        fake_clock.advance(45.0)
        client.get(PAGE)
        assert fake_clock.sleeps == [30.0, 18.0]

    def test_excessive_crawl_delay_is_refused(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        respx_router.get(ROBOTS).respond(200, text="User-agent: *\nCrawl-delay: 3600\n")
        page = respx_router.get(PAGE).respond(200, text="ok")
        with pytest.raises(CrawlDelayTooLongError, match="3600 s"):
            client.get(PAGE)
        assert page.call_count == 0
        assert fake_clock.sleeps == []

    def test_hosts_are_limited_independently(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        other = "https://other.example.org"
        no_robots(respx_router)
        no_robots(respx_router, other)
        respx_router.get(PAGE).respond(200, text="a")
        respx_router.get(f"{other}/events").respond(200, text="b")
        client.get(PAGE)
        client.get(f"{other}/events")
        assert fake_clock.sleeps == [1.0, 1.0]


class TestBudget:
    def test_budget_is_enforced_with_a_clear_error(
        self,
        http_settings: HttpSettings,
        respx_router: respx.MockRouter,
        fake_clock: FakeClock,
    ) -> None:
        settings = http_settings.evolve(max_requests_per_host=3)
        client = PoliteClient(settings, clock=fake_clock, sleep=fake_clock.sleep)
        no_robots(respx_router)
        page = respx_router.get(url__startswith=PAGE).respond(200, text="ok")
        for number in range(3):
            client.get(f"{PAGE}?page={number}")
        with pytest.raises(HostBudgetExceededError) as caught:
            client.get(f"{PAGE}?page=3")
        message = str(caught.value)
        assert "request budget for host dept.example.edu is exhausted" in message
        assert "3 requests per run" in message
        assert page.call_count == 3
        assert client.request_counts() == {"dept.example.edu": 3}

    def test_budget_is_per_host_and_excludes_robots(
        self,
        http_settings: HttpSettings,
        respx_router: respx.MockRouter,
        fake_clock: FakeClock,
    ) -> None:
        settings = http_settings.evolve(max_requests_per_host=1)
        client = PoliteClient(settings, clock=fake_clock, sleep=fake_clock.sleep)
        other = "https://other.example.org"
        respx_router.get(ROBOTS).respond(200, text="User-agent: *\nDisallow:\n")
        no_robots(respx_router, other)
        respx_router.get(PAGE).respond(200, text="a")
        respx_router.get(f"{other}/events").respond(200, text="b")
        assert client.get(PAGE).text == "a"
        assert client.get(f"{other}/events").text == "b"
        with pytest.raises(HostBudgetExceededError):
            client.get(PAGE)

    def test_retries_consume_budget(
        self,
        http_settings: HttpSettings,
        respx_router: respx.MockRouter,
        fake_clock: FakeClock,
    ) -> None:
        settings = http_settings.evolve(max_requests_per_host=2)
        client = PoliteClient(settings, clock=fake_clock, sleep=fake_clock.sleep)
        no_robots(respx_router)
        page = respx_router.get(PAGE).respond(503)
        with pytest.raises(HostBudgetExceededError):
            client.get(PAGE)
        assert page.call_count == 2


class TestRetries:
    def test_server_errors_are_retried_with_exponential_backoff(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        no_robots(respx_router)
        page = respx_router.get(PAGE).mock(
            side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200, text="ok")]
        )
        assert client.get(PAGE).text == "ok"
        assert page.call_count == 3
        assert fake_clock.sleeps == [1.0, 1.0, 2.0]

    def test_gives_up_after_the_configured_retries(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        no_robots(respx_router)
        page = respx_router.get(PAGE).respond(500)
        with pytest.raises(HttpStatusError) as caught:
            client.get(PAGE)
        assert caught.value.status == 500
        assert page.call_count == 4
        assert fake_clock.sleeps == [1.0, 1.0, 2.0, 4.0]

    def test_network_errors_are_retried(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        no_robots(respx_router)
        page = respx_router.get(PAGE).mock(
            side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, text="ok")]
        )
        assert client.get(PAGE).text == "ok"
        assert page.call_count == 2

    def test_persistent_network_error_raises(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        no_robots(respx_router)
        page = respx_router.get(PAGE).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(HttpRequestError, match=r"after 4 attempt\(s\) \(ConnectError\)"):
            client.get(PAGE)
        assert page.call_count == 4

    def test_retry_after_is_respected(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        no_robots(respx_router)
        respx_router.get(PAGE).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "7"}),
                httpx.Response(200, text="ok"),
            ]
        )
        assert client.get(PAGE).text == "ok"
        assert fake_clock.sleeps == [1.0, 7.0]

    def test_retry_after_is_capped(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        no_robots(respx_router)
        respx_router.get(PAGE).mock(
            side_effect=[
                httpx.Response(503, headers={"Retry-After": "86400"}),
                httpx.Response(503, headers={"Retry-After": "not a number"}),
                httpx.Response(200, text="ok"),
            ]
        )
        assert client.get(PAGE).text == "ok"
        assert fake_clock.sleeps == [1.0, 60.0, 2.0]

    def test_client_errors_are_not_retried(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        no_robots(respx_router)
        page = respx_router.get(PAGE).respond(403)
        with pytest.raises(HttpStatusError):
            client.get(PAGE)
        assert page.call_count == 1

    def test_no_retries_when_disabled(
        self,
        http_settings: HttpSettings,
        respx_router: respx.MockRouter,
        fake_clock: FakeClock,
    ) -> None:
        client = PoliteClient(
            http_settings.evolve(retries=0), clock=fake_clock, sleep=fake_clock.sleep
        )
        no_robots(respx_router)
        page = respx_router.get(PAGE).respond(503)
        with pytest.raises(HttpStatusError):
            client.get(PAGE)
        assert page.call_count == 1
