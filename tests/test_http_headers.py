"""Tests for what leaves with a request: the headers of the caller and (never) cookies."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from fakes import PUBLIC_ADDRESS, FakeClock, StubResolver
from seminars.config import HttpSettings
from seminars.http import ForbiddenHostError, PoliteClient

API = "https://api.source.example.edu"
EVENTS = f"{API}/v1/events"
ELSEWHERE = "https://elsewhere.example"
SECRETS = {"Authorization": "Bearer SOURCE-API-TOKEN", "X-Api-Key": "k-123", "Cookie": "a=b"}
HARMLESS = {"Accept": "application/json", "Accept-Language": "en"}
Handler = Callable[[httpx.Request], httpx.Response]


class Network:
    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404, headers={"Set-Cookie": "robots_seen=1; Path=/"})
        return self._handler(request)

    def pages(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path != "/robots.txt"]


def build(http_settings: HttpSettings, clock: FakeClock, handler: Handler) -> PoliteClient:
    return PoliteClient(
        http_settings,
        clock=clock,
        sleep=clock.sleep,
        transport=httpx.MockTransport(handler),
        resolver=StubResolver(),
    )


def sent(request: httpx.Request) -> dict[str, str]:
    """The headers of ``request`` that a caller or a cookie jar could have put there."""
    names = ("authorization", "x-api-key", "cookie", "accept", "accept-language")
    return {name: request.headers[name] for name in names if name in request.headers}


class TestHeadersOfTheCaller:
    def test_they_go_to_the_origin_that_was_asked(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        network = Network(lambda request: httpx.Response(200, text="[]"))
        client = build(http_settings, fake_clock, network)
        client.get(EVENTS, headers={**SECRETS, **HARMLESS})
        robots, page = network.requests
        assert sent(page) == {
            "authorization": "Bearer SOURCE-API-TOKEN",
            "x-api-key": "k-123",
            "cookie": "a=b",
            "accept": "application/json",
            "accept-language": "en",
        }
        assert sent(robots) == {"accept": "*/*"}

    def test_a_redirect_to_another_origin_gets_only_the_harmless_ones(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.source.example.edu":
                return httpx.Response(302, headers={"Location": f"{ELSEWHERE}/collect"})
            return httpx.Response(200, text="thanks")

        network = Network(handler)
        client = build(http_settings, fake_clock, network)
        document = client.get(EVENTS, headers={**SECRETS, **HARMLESS, "X-Custom": "1"})
        assert document.final_url == f"{ELSEWHERE}/collect"
        first, second = network.pages()
        assert sent(first)["authorization"] == "Bearer SOURCE-API-TOKEN"
        assert second.url.host == "elsewhere.example"
        assert sent(second) == {"accept": "application/json", "accept-language": "en"}
        assert "x-custom" not in second.headers
        assert second.headers["user-agent"] == http_settings.user_agent

    @pytest.mark.parametrize(
        "location",
        [
            "http://api.source.example.edu/v1/events",  # another scheme
            "https://api.source.example.edu:8443/v1/events",  # another port
            "https://source.example.edu/v1/events",  # the parent domain
            "https://evil.api.source.example.edu/v1/events",  # a subdomain
        ],
    )
    def test_another_scheme_port_or_name_is_another_origin(
        self, http_settings: HttpSettings, fake_clock: FakeClock, location: str
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == EVENTS:
                return httpx.Response(307, headers={"Location": location})
            return httpx.Response(200, text="ok")

        network = Network(handler)
        build(http_settings, fake_clock, network).get(EVENTS, headers=SECRETS)
        assert sent(network.pages()[-1]) == {"accept": "*/*"}

    def test_a_redirect_within_the_origin_keeps_them(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/events":
                return httpx.Response(301, headers={"Location": "/v2/events"})
            return httpx.Response(200, text="ok")

        network = Network(handler)
        build(http_settings, fake_clock, network).get(
            "HTTPS://API.Source.Example.EDU:443/v1/events", headers=SECRETS
        )
        first, second = network.pages()
        assert sent(first) == sent(second)
        assert sent(second)["x-api-key"] == "k-123"

    def test_a_redirect_that_comes_back_gets_them_again(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/events":
                return httpx.Response(302, headers={"Location": f"{ELSEWHERE}/login"})
            if request.url.host == "elsewhere.example":
                return httpx.Response(302, headers={"Location": f"{API}/v1/events?ok=1"})
            return httpx.Response(200, text="ok")

        def routed(request: httpx.Request) -> httpx.Response:
            if request.url.query:
                return httpx.Response(200, text="ok")
            return handler(request)

        network = Network(routed)
        build(http_settings, fake_clock, network).get(EVENTS, headers=SECRETS)
        first, away, back = network.pages()
        assert "authorization" in sent(first)
        assert sent(away) == {"accept": "*/*"}
        assert sent(back)["authorization"] == "Bearer SOURCE-API-TOKEN"

    def test_robots_requests_never_carry_them(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        network = Network(lambda request: httpx.Response(200, text="ok"))
        build(http_settings, fake_clock, network).get(EVENTS, headers=SECRETS)
        assert sent(network.requests[0]) == {"accept": "*/*"}


class TestCookies:
    """The client keeps no cookies: they would be state a fixture cannot reproduce."""

    def test_a_cookie_a_site_sets_is_never_sent_back(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            cookies = ["sid=abc; Path=/", "wide=1; Domain=example.edu; Path=/"]
            return httpx.Response(
                200, text="ok", headers=[("Set-Cookie", cookie) for cookie in cookies]
            )

        network = Network(handler)
        client = build(http_settings, fake_clock, network)
        client.get(EVENTS)
        client.get(f"{API}/v1/events?page=2")
        client.get("https://www.example.edu/other")
        assert len(network.requests) == 5
        assert all("cookie" not in request.headers for request in network.requests)
        assert list(client._wire._client.cookies.jar) == []

    def test_a_cookie_of_a_redirect_does_not_travel_with_it(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/events":
                headers = {"Location": "/v2/events", "Set-Cookie": "sid=abc; Path=/"}
                return httpx.Response(302, headers=headers)
            return httpx.Response(200, text="ok")

        network = Network(handler)
        build(http_settings, fake_clock, network).get(EVENTS)
        assert all("cookie" not in request.headers for request in network.requests)

    def test_nothing_of_a_refused_answer_is_kept(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        """An answer from a private address is refused unread; its cookie must go with it."""

        class Peer:
            def __init__(self, address: str) -> None:
                self._address = address

            def get_extra_info(self, info: str) -> object:
                return (self._address, 80) if info == "server_addr" else None

        peers = ["10.0.0.5", PUBLIC_ADDRESS]

        def handler(request: httpx.Request) -> httpx.Response:
            peer = Peer(peers.pop(0) if len(peers) > 1 else peers[0])
            return httpx.Response(
                200,
                text="internal",
                headers={"Set-Cookie": "session=INTERNAL-SECRET; Path=/"},
                extensions={"network_stream": peer},
            )

        network = Network(handler)
        client = build(http_settings, fake_clock, network)
        with pytest.raises(ForbiddenHostError):
            client.get("http://rebind.example.edu/a")
        other = build(http_settings, fake_clock, network)
        other._wire._client = client._wire._client  # the same connection pool and cookie jar
        other.get("http://rebind.example.edu/b")
        assert all("cookie" not in request.headers for request in network.requests)
        assert list(client._wire._client.cookies.jar) == []

    def test_a_cookie_the_caller_passes_is_a_header_like_any_other(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        network = Network(lambda request: httpx.Response(200, text="ok"))
        client = build(http_settings, fake_clock, network)
        client.get(EVENTS, headers={"Cookie": "consent=yes"})
        client.get(f"{API}/v1/events?page=2")
        first, second = network.pages()
        assert first.headers["cookie"] == "consent=yes"
        assert "cookie" not in second.headers
