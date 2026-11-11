"""Tests for how many requests one host can get from one client, robots.txt included."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import httpx
import pytest

from fakes import FakeClock, StubResolver
from seminars.config import HttpSettings
from seminars.http import (
    HostBudgetExceededError,
    HttpClient,
    HttpError,
    PoliteClient,
    RobotsDisallowedError,
    RobotsUnavailableError,
)
from seminars.httpcache import CachedRobots, HostRecord, HttpCache

VICTIM = "victim.example.edu"
ROBOTS_TEXT = "User-agent: *\nDisallow: /private/\n"


class Site:
    """A fake network that counts what every host receives."""

    def __init__(self, robots_text: str = ROBOTS_TEXT) -> None:
        self.robots_text = robots_text
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if host.endswith(".evil.example") and request.url.path == "/robots.txt":
            return httpx.Response(302, headers={"Location": f"https://{VICTIM}/robots.txt"})
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=self.robots_text)
        return httpx.Response(200, text="ok")

    def received(self, host: str) -> Counter[str]:
        """How many robots.txt and page requests ``host`` got."""
        kinds = Counter[str]()
        for request in self.requests:
            if request.url.host == host:
                kinds["robots" if request.url.path == "/robots.txt" else "pages"] += 1
        return kinds


def build(
    settings: HttpSettings, clock: FakeClock, site: Site, cache_dir: Path | None = None
) -> PoliteClient:
    return PoliteClient(
        settings,
        cache_dir=cache_dir,
        clock=clock,
        sleep=clock.sleep,
        transport=httpx.MockTransport(site),
        resolver=StubResolver(),
    )


def outcomes(client: PoliteClient, urls: list[str]) -> Counter[str]:
    seen = Counter[str]()
    for url in urls:
        try:
            client.get(url)
            seen["ok"] += 1
        except HttpError as error:
            seen[type(error).__name__] += 1
    return seen


class TestRequestsPerHost:
    """A page of links cannot make the client send more than twice the budget to one host."""

    def test_plain_links(self, http_settings: HttpSettings, fake_clock: FakeClock) -> None:
        site = Site()
        client = build(http_settings, fake_clock, site)
        urls = [f"https://{VICTIM}/p{number}" for number in range(60)]
        assert outcomes(client, urls) == {"ok": 20, "HostBudgetExceededError": 40}
        assert site.received(VICTIM) == {"robots": 1, "pages": 20}

    def test_links_to_many_ports_of_one_host(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        site = Site()
        client = build(http_settings, fake_clock, site)
        urls = [f"https://{VICTIM}:{8000 + number}/p{number}" for number in range(60)]
        assert outcomes(client, urls) == {"ok": 20, "HostBudgetExceededError": 40}
        assert site.received(VICTIM) == {"robots": 20, "pages": 20}
        assert client.request_counts() == {VICTIM: 20}

    def test_links_with_a_user_name_send_nothing(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        site = Site()
        client = build(http_settings, fake_clock, site)
        urls = [f"https://u{number}:guess{number}@{VICTIM}/p{number}" for number in range(60)]
        assert outcomes(client, urls) == {"UnsupportedSchemeError": 60}
        assert site.requests == []

    def test_numeric_spellings_of_one_address(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        site = Site()
        client = build(http_settings, fake_clock, site)
        urls = [f"https://0x{'0' * zeros}1.1.1.1/p{zeros}" for zeros in range(30)]
        assert outcomes(client, urls) == {"ok": 20, "HostBudgetExceededError": 10}
        assert site.received("1.1.1.1") == {"robots": 1, "pages": 20}

    def test_robots_redirects_of_many_other_hosts(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        """Hosts an attacker controls point their robots.txt at a third party."""
        site = Site()
        client = build(http_settings, fake_clock, site)
        urls = [f"https://a{number}.evil.example/x" for number in range(60)]
        assert outcomes(client, urls) == {"ok": 20, "RobotsUnavailableError": 40}
        assert site.received(VICTIM) == {"robots": 20}
        with pytest.raises(RobotsUnavailableError, match="request budget is used up"):
            client.get("https://a59.evil.example/y")
        assert site.received(VICTIM) == {"robots": 20}

    def test_the_message_names_the_budget_that_ran_out(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        client = build(http_settings.evolve(max_requests_per_host=2), fake_clock, Site())
        for port in (8001, 8002):
            client.get(f"https://{VICTIM}:{port}/a")
        with pytest.raises(HostBudgetExceededError) as pages:
            client.get(f"https://{VICTIM}:8001/b")
        assert f"request budget for host {VICTIM} is exhausted (2 requests per run)" in str(
            pages.value
        )
        with pytest.raises(HostBudgetExceededError) as robots:
            client.get(f"https://{VICTIM}:8003/a")
        assert "robots.txt request budget" in str(robots.value)
        assert f"host {VICTIM} is exhausted (2 requests per run)" in str(robots.value)

    def test_retries_of_a_robots_request_count(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        answers: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            answers.append(503)
            return httpx.Response(503)

        client = PoliteClient(
            http_settings.evolve(max_requests_per_host=6),
            clock=fake_clock,
            sleep=fake_clock.sleep,
            transport=httpx.MockTransport(handler),
            resolver=StubResolver(),
        )
        with pytest.raises(RobotsUnavailableError, match="HTTP 503"):
            client.get(f"https://{VICTIM}/a")
        with pytest.raises(HostBudgetExceededError, match=r"robots\.txt request budget"):
            client.get(f"http://{VICTIM}/a")
        assert len(answers) == 6


class TestLimitsBelongToTheHostName:
    """What the limits do not cover, as the module docstring says under "What remains"."""

    def test_names_that_share_an_address_are_paced_and_counted_apart(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        """Every name resolves to the same address here; each is a host of its own."""
        site = Site(robots_text="")
        client = build(http_settings, fake_clock, site)
        names = [f"n{number}.example.net" for number in range(30)]
        assert outcomes(client, [f"https://{name}/x" for name in names]) == {"ok": 30}
        assert client.request_counts() == dict.fromkeys(sorted(names), 1)
        assert len(site.requests) == 60
        assert fake_clock.sleeps == [1.0] * 30  # robots.txt, then the page; no pause between names

    def test_a_redirect_hop_is_charged_to_the_host_it_goes_to(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            if request.url.host == "sender.example.org":
                target = f"https://{VICTIM}/landing?{request.url.path[1:]}"
                return httpx.Response(302, headers={"Location": target})
            return httpx.Response(200, text="ok")

        client = PoliteClient(
            http_settings,
            clock=fake_clock,
            sleep=fake_clock.sleep,
            transport=httpx.MockTransport(handler),
            resolver=StubResolver(),
        )
        for number in range(20):
            assert client.get(f"https://sender.example.org/{number}").text == "ok\n"
        assert client.request_counts() == {"sender.example.org": 20, VICTIM: 20}
        with pytest.raises(HostBudgetExceededError, match=f"host {VICTIM} is exhausted"):
            client.get(f"https://{VICTIM}/events")

    def test_the_docstrings_say_so(self) -> None:
        rules, remains = (
            " ".join(part.split()) for part in http_docstring().split("What remains:")
        )
        assert "one host NAME at most ``2 * max_requests_per_host`` requests per run" in rules
        assert "is charged to the host it leads to" in rules
        for phrase in (
            "per host NAME, not per address",
            "a redirect hop is charged to the host it goes to",
            "``seminars.sources.base.on_source_host``",
        ):
            assert phrase in remains
        contract = " ".join((HttpClient.get.__doc__ or "").split())
        assert "per host NAME, not per address" in contract
        assert "a redirect hop is charged to the host it goes to" in contract
        assert "``seminars.sources.base.on_source_host``" in contract


def http_docstring() -> str:
    import seminars.http

    return seminars.http.__doc__ or ""


class TestHostRecordSize:
    """What is kept about a host between processes stays small."""

    def test_only_the_newest_robots_copies_are_kept(
        self, http_settings: HttpSettings, fake_clock: FakeClock, tmp_path: Path
    ) -> None:
        site = Site()
        client = build(http_settings, fake_clock, site, tmp_path)
        for port in range(8000, 8010):
            client.get(f"https://{VICTIM}:{port}/p")
        kept = HttpCache(tmp_path).host(VICTIM).robots
        assert sorted(kept) == [f"https://{VICTIM}:{port}" for port in range(8006, 8010)]
        assert site.received(VICTIM) == {"robots": 10, "pages": 10}
        client.get(f"https://{VICTIM}:8000/q")  # the client itself still knows the policy
        with pytest.raises(RobotsDisallowedError):
            client.get(f"https://{VICTIM}:8000/private/x")
        assert site.received(VICTIM) == {"robots": 10, "pages": 11}

    def test_outdated_copies_are_dropped(
        self, http_settings: HttpSettings, fake_clock: FakeClock, tmp_path: Path
    ) -> None:
        cache = HttpCache(tmp_path)
        now = fake_clock()
        old = CachedRobots(fetched_at=now - http_settings.robots_cache_seconds - 5, text="old")
        fresh = CachedRobots(fetched_at=now - 60, text=ROBOTS_TEXT)
        ahead = CachedRobots(fetched_at=now + 3600, text="from a clock that runs ahead")
        cache.store_host(
            VICTIM,
            HostRecord(
                last_request_at=now - 60,
                robots={
                    f"http://{VICTIM}:1": old,
                    f"http://{VICTIM}:2": fresh,
                    f"http://{VICTIM}:3": ahead,
                },
            ),
        )
        client = build(http_settings, fake_clock, Site(), tmp_path)
        client.get(f"https://{VICTIM}/p")
        assert sorted(cache.host(VICTIM).robots) == [f"http://{VICTIM}:2", f"https://{VICTIM}"]

    def test_a_long_robots_file_is_cut_at_a_line_end(
        self, http_settings: HttpSettings, fake_clock: FakeClock, tmp_path: Path
    ) -> None:
        """RFC 9309 lets a crawler stop parsing after 500 kibibytes."""
        early = "User-agent: *\nDisallow: /private/\n"
        late = "Disallow: /late/\n"
        site = Site(early + "# padding padding padding padding\n" * 20_000 + late)
        assert len(site.robots_text) > 600_000
        client = build(http_settings, fake_clock, site, tmp_path)
        with pytest.raises(RobotsDisallowedError):
            client.get(f"https://{VICTIM}/private/x")
        client.get(f"https://{VICTIM}/late/x")
        stored = HttpCache(tmp_path).host(VICTIM).robots[f"https://{VICTIM}"].text
        assert stored is not None
        assert 500_000 < len(stored) <= 512_000
        assert stored.endswith("# padding padding padding padding")
        size = (tmp_path / "hosts" / f"{VICTIM}.json").stat().st_size
        assert size < 600_000

    def test_a_line_that_does_not_end_within_the_limit_is_not_used(
        self, http_settings: HttpSettings, fake_clock: FakeClock, tmp_path: Path
    ) -> None:
        site = Site("User-agent: *\nDisallow: /private/\nAllow: /private/" + "x" * 600_000)
        client = build(http_settings, fake_clock, site, tmp_path)
        with pytest.raises(RobotsDisallowedError):
            client.get(f"https://{VICTIM}/private/{'x' * 100}")
        stored = HttpCache(tmp_path).host(VICTIM).robots[f"https://{VICTIM}"].text
        assert stored == "User-agent: *\nDisallow: /private/"

    def test_a_file_that_is_one_endless_line_has_no_rules(
        self, http_settings: HttpSettings, fake_clock: FakeClock, tmp_path: Path
    ) -> None:
        site = Site("Disallow: /" + "x" * 600_000)
        client = build(http_settings, fake_clock, site, tmp_path)
        assert client.get(f"https://{VICTIM}/x").text == "ok\n"
        assert HttpCache(tmp_path).host(VICTIM).robots[f"https://{VICTIM}"].text == ""


class TestTrimmedRecord:
    def test_keeps_the_newest_copies_within_the_window(self) -> None:
        copies = {f"https://x:{port}": CachedRobots(fetched_at=float(port)) for port in range(10)}
        record = HostRecord(last_request_at=9.0, robots=copies)
        trimmed = record.trimmed(oldest=2.0, newest=8.0, keep=3)
        assert sorted(trimmed.robots) == ["https://x:6", "https://x:7", "https://x:8"]
        assert trimmed.last_request_at == 9.0
        assert record.trimmed(oldest=0.0, newest=9.0, keep=20) == record
        assert sorted(record.trimmed(oldest=0.0, newest=9.0, keep=0).robots) == []
