"""Tests for addresses that are globally routable and still not a public site.

Two kinds: an address of this very machine (a host with a public address on
one of its interfaces reaches its own services through it), and addresses
on a deny list (``http.forbidden_networks``), such as the platform address
of a cloud.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pydantic import ValidationError

from fakes import PUBLIC_ADDRESS, FakeClock, StubResolver
from seminars.config import HttpSettings, load_settings
from seminars.http import (
    ForbiddenHostError,
    PoliteClient,
    RobotsUnavailableError,
    is_public_address,
    is_public_host,
    split_url,
)
from seminars.httpcache import HttpCache
from seminars.httpurl import DEFAULT_FORBIDDEN_NETWORKS, parse_networks
from seminars.httpwire import system_own_address

REPO_ROOT = Path(__file__).resolve().parents[1]
NAME = "dept.example.edu"
PAGE = f"https://{NAME}/events"
MY_ADDRESS = "171.64.1.20"  # stands for a public address of the machine the client runs on
MY_ADDRESS_V6 = "2607:f6d0::20"
AZURE_PLATFORM = "168.63.129.16"
real_own_address = system_own_address  # conftest replaces the module attribute for every test


class Connection:
    """Stand-in for the ``network_stream`` of a response: it knows both ends."""

    def __init__(self, server: object, client: object = ("192.0.2.10", 50000)) -> None:
        self._ends = {"server_addr": server, "client_addr": client}

    def get_extra_info(self, info: str) -> object:
        return self._ends.get(info)


class Body(httpx.SyncByteStream):
    def __init__(self) -> None:
        self.reads = 0

    def __iter__(self) -> Any:
        self.reads += 1
        yield b"a service of this machine"


def build(
    settings: HttpSettings,
    clock: FakeClock,
    resolver: StubResolver,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    own: Callable[[str], bool] | None = None,
    cache_dir: Path | None = None,
) -> PoliteClient:
    return PoliteClient(
        settings,
        cache_dir=cache_dir,
        clock=clock,
        sleep=clock.sleep,
        transport=httpx.MockTransport(handler),
        resolver=resolver,
        own_address=own,
    )


def is_mine(address: str) -> bool:
    return address in (MY_ADDRESS, MY_ADDRESS_V6)


class TestLookupAnswersThatAreThisMachine:
    @pytest.mark.parametrize(
        "addresses",
        [[MY_ADDRESS], [PUBLIC_ADDRESS, MY_ADDRESS], [MY_ADDRESS_V6], [MY_ADDRESS, "10.0.0.1"]],
    )
    def test_a_name_that_resolves_to_an_address_of_this_machine_costs_no_request(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        resolver: StubResolver,
        addresses: list[str],
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, text="a service of this machine")

        resolver.answers[NAME] = addresses
        client = build(http_settings, fake_clock, resolver, handler, own=is_mine)
        with pytest.raises(ForbiddenHostError, match=r"this machine|local or private"):
            client.get(PAGE)
        assert requests == []
        assert client.request_counts() == {NAME: 0}
        assert fake_clock.sleeps == []

    def test_the_address_written_as_a_url_is_refused_the_same_way(
        self, http_settings: HttpSettings, fake_clock: FakeClock
    ) -> None:
        """Its spelling is that of any public address; the lookup of the literal finds it out."""
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, text="x")

        assert is_public_host(MY_ADDRESS)
        resolver = StubResolver({MY_ADDRESS: [MY_ADDRESS], MY_ADDRESS_V6: [MY_ADDRESS_V6]})
        client = build(http_settings, fake_clock, resolver, handler, own=is_mine)
        for url in (f"http://{MY_ADDRESS}:8080/admin", f"http://[{MY_ADDRESS_V6}]:8080/admin"):
            with pytest.raises(ForbiddenHostError, match="an address of this machine"):
                client.get(url)
        assert requests == []
        assert resolver.calls == [MY_ADDRESS, MY_ADDRESS_V6]

    def test_a_redirect_to_this_machine_is_refused(
        self, http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
    ) -> None:
        """A site learns the address from the request it gets and answers with a redirect to it."""
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(f"{request.url.host}{request.url.path}")
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(302, headers={"Location": f"http://{MY_ADDRESS}:8080/admin"})

        resolver.answers[MY_ADDRESS] = [MY_ADDRESS]
        client = build(http_settings, fake_clock, resolver, handler, own=is_mine)
        with pytest.raises(ForbiddenHostError, match="an address of this machine"):
            client.get(PAGE)
        assert paths == [f"{NAME}/robots.txt", f"{NAME}/events"]

    def test_robots_txt_redirected_to_this_machine_blocks_the_origin(
        self, http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
    ) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(f"{request.url.host}{request.url.path}")
            return httpx.Response(302, headers={"Location": f"http://{MY_ADDRESS}/robots.txt"})

        resolver.answers[MY_ADDRESS] = [MY_ADDRESS]
        client = build(http_settings, fake_clock, resolver, handler, own=is_mine)
        with pytest.raises(RobotsUnavailableError, match="must not be fetched"):
            client.get(PAGE)
        assert paths == [f"{NAME}/robots.txt"]

    def test_the_check_gets_every_address_of_the_answer(
        self, http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
    ) -> None:
        asked: list[str] = []

        def own(address: str) -> bool:
            asked.append(address)
            return False

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404 if request.url.path == "/robots.txt" else 200, text="ok")

        resolver.answers[NAME] = [PUBLIC_ADDRESS, "2606:4700:4700::1111"]
        client = build(http_settings, fake_clock, resolver, handler, own=own)
        assert client.get(PAGE).text == "ok\n"
        assert asked == [PUBLIC_ADDRESS, "2606:4700:4700::1111"] * 2


class TestConnectionsThatEndOnThisMachine:
    @pytest.mark.parametrize(
        ("server", "local"),
        [
            ((MY_ADDRESS, 8080), (MY_ADDRESS, 51000)),
            ((MY_ADDRESS_V6, 8080, 0, 0), (MY_ADDRESS_V6, 51000, 0, 0)),
            (("2607:F6D0:0:0:0:0:0:20", 8080, 0, 0), (MY_ADDRESS_V6, 51000, 0, 0)),
            ((PUBLIC_ADDRESS, 443), (PUBLIC_ADDRESS, 51000)),
        ],
        ids=["ipv4", "ipv6", "ipv6-other-spelling", "any-address"],
    )
    def test_an_answer_over_a_connection_with_both_ends_at_one_address_is_refused_unread(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        resolver: StubResolver,
        tmp_path: Path,
        server: object,
        local: object,
    ) -> None:
        body = Body()
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(404, extensions={"network_stream": Connection(server)})
            return httpx.Response(
                200,
                stream=body,
                headers={"ETag": '"v1"'},
                extensions={"network_stream": Connection(server, local)},
            )

        client = build(http_settings, fake_clock, resolver, handler, cache_dir=tmp_path)
        with pytest.raises(ForbiddenHostError, match="an answer from this machine"):
            client.get(PAGE)
        with pytest.raises(ForbiddenHostError, match="earlier in this run"):
            client.get(PAGE)
        assert paths == ["/robots.txt", "/events"]
        assert body.reads == 0
        assert HttpCache(tmp_path).page(PAGE) is None

    def test_a_peer_that_is_an_address_of_this_machine_is_refused_whatever_the_local_end(
        self, http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
    ) -> None:
        body = Body()

        def handler(request: httpx.Request) -> httpx.Response:
            connection = Connection((MY_ADDRESS, 8080), ("127.0.0.1", 51000))
            return httpx.Response(200, stream=body, extensions={"network_stream": connection})

        client = build(http_settings, fake_clock, resolver, handler, own=is_mine)
        with pytest.raises(ForbiddenHostError, match="an answer from this machine"):
            client.get(PAGE)
        assert body.reads == 0

    @pytest.mark.parametrize("local", [("192.0.2.10", 51000), None, "unix", (), (None, 1)])
    def test_an_answer_from_elsewhere_is_read(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        resolver: StubResolver,
        local: object,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            status = 404 if request.url.path == "/robots.txt" else 200
            connection = Connection((PUBLIC_ADDRESS, 443), local)
            return httpx.Response(status, text="ok", extensions={"network_stream": connection})

        client = build(http_settings, fake_clock, resolver, handler)
        assert client.get(PAGE).text == "ok\n"


class TestForbiddenNetworks:
    def test_the_platform_address_of_azure_is_on_the_list_by_default(self) -> None:
        assert DEFAULT_FORBIDDEN_NETWORKS == ("168.63.129.16/32",)
        assert HttpSettings(user_agent="x/1").forbidden_networks == ["168.63.129.16/32"]
        assert not is_public_address(AZURE_PLATFORM)
        assert not is_public_address(f"::ffff:{AZURE_PLATFORM}")
        assert not is_public_address("64:ff9b::a83f:8110")  # the same address behind NAT64
        assert is_public_address("168.63.129.17")
        assert not is_public_host(AZURE_PLATFORM)
        assert not is_public_host("2822734096")  # the same address as one number
        with pytest.raises(ForbiddenHostError):
            split_url(f"http://{AZURE_PLATFORM}/metadata")

    def test_the_committed_settings_have_the_list(self) -> None:
        settings = load_settings(REPO_ROOT / "config" / "settings.yaml")
        assert settings.http.forbidden_networks == ["168.63.129.16/32"]

    def test_is_public_address_takes_another_list(self) -> None:
        networks = parse_networks(["203.0.112.0/22", "2001:4860::/32"])
        assert not is_public_address("203.0.113.7", networks)
        assert not is_public_address("2001:4860:4860::8888", networks)
        assert is_public_address(AZURE_PLATFORM, networks)
        assert is_public_address("8.8.8.8", networks)
        assert not is_public_address("10.0.0.1", ())

    @pytest.mark.parametrize("address", [AZURE_PLATFORM, "8.8.4.4", "2001:4860:4860::8844"])
    def test_a_name_that_resolves_into_a_forbidden_network_is_refused_without_a_request(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        resolver: StubResolver,
        address: str,
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, text="x")

        settings = http_settings.evolve(
            forbidden_networks=["168.63.129.16/32", "8.8.4.0/24", "2001:4860::/32"]
        )
        resolver.answers[NAME] = [PUBLIC_ADDRESS, address]
        client = build(settings, fake_clock, resolver, handler)
        with pytest.raises(ForbiddenHostError, match="resolves to a local or private network"):
            client.get(PAGE)
        assert requests == []

    def test_an_answer_from_a_forbidden_network_is_refused_unread(
        self, http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
    ) -> None:
        body = Body()

        def handler(request: httpx.Request) -> httpx.Response:
            connection = Connection((AZURE_PLATFORM, 80))
            return httpx.Response(200, stream=body, extensions={"network_stream": connection})

        client = build(http_settings, fake_clock, resolver, handler)
        with pytest.raises(ForbiddenHostError, match="answer from a local or private network"):
            client.get(PAGE)
        assert body.reads == 0

    def test_an_empty_list_forbids_nothing_more_than_the_address_classes(
        self, http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404 if request.url.path == "/robots.txt" else 200, text="ok")

        resolver.answers[NAME] = [AZURE_PLATFORM]
        settings = http_settings.evolve(forbidden_networks=[])
        assert build(settings, fake_clock, resolver, handler).get(PAGE).text == "ok\n"
        resolver.answers[NAME] = ["10.0.0.1"]
        with pytest.raises(ForbiddenHostError):
            build(settings, fake_clock, resolver, handler).get(PAGE)

    @pytest.mark.parametrize(
        ("value", "stored"),
        [
            ("168.63.129.16", "168.63.129.16/32"),
            ("203.0.113.9/24", "203.0.113.0/24"),
            ("2001:DB8::/32", "2001:db8::/32"),
        ],
    )
    def test_networks_are_stored_in_one_spelling(self, value: str, stored: str) -> None:
        settings = HttpSettings(user_agent="x/1", forbidden_networks=[value])
        assert settings.forbidden_networks == [stored]

    @pytest.mark.parametrize("value", ["", "example.edu", "10.0.0.0/33", "1.2.3", "10.0.0.0/8/8"])
    def test_what_is_not_a_network_is_a_configuration_error(self, value: str) -> None:
        with pytest.raises(ValidationError, match="forbidden_networks"):
            HttpSettings(user_agent="x/1", forbidden_networks=[value])

    def test_the_setting_is_read_from_the_settings_file(self, tmp_path: Path) -> None:
        data = yaml.safe_load((REPO_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
        data["http"]["forbidden_networks"] = ["168.63.129.16/32", "198.51.100.0/24"]
        path = tmp_path / "settings.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        assert load_settings(path).http.forbidden_networks == [
            "168.63.129.16/32",
            "198.51.100.0/24",
        ]


class FakeSocket:
    """A UDP socket that picks its source address from a table instead of the routing table."""

    routes: dict[str, str]
    made: list[tuple[int, int]]

    def __init__(self, family: int, kind: int) -> None:
        self.made.append((family, kind))
        self._source = ""

    def __enter__(self) -> FakeSocket:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def connect(self, address: tuple[str, int]) -> None:
        if address[0] not in self.routes:
            raise OSError(101, "Network is unreachable")
        self._source = self.routes[address[0]]

    def getsockname(self) -> tuple[str, int]:
        return (self._source, 40000)


class TestSystemOwnAddress:
    @pytest.fixture
    def sockets(self, monkeypatch: pytest.MonkeyPatch) -> type[FakeSocket]:
        FakeSocket.routes = {
            MY_ADDRESS: MY_ADDRESS,
            PUBLIC_ADDRESS: MY_ADDRESS,
            "2607:f6d0::20": "2607:f6d0:0:0::20",
            "2606:4700:4700::1111": "2607:f6d0::20",
            "fe80::1": "fe80::1%eth0",
        }
        FakeSocket.made = []
        monkeypatch.setattr(socket, "socket", FakeSocket)
        return FakeSocket

    def test_an_address_the_kernel_routes_to_itself_is_this_machine(
        self, sockets: type[FakeSocket]
    ) -> None:
        assert real_own_address(MY_ADDRESS)
        assert real_own_address(MY_ADDRESS_V6)
        assert real_own_address("2607:F6D0:0:0:0:0:0:20")
        assert real_own_address("fe80::1%eth0")
        assert sockets.made[0] == (socket.AF_INET, socket.SOCK_DGRAM)
        assert sockets.made[1] == (socket.AF_INET6, socket.SOCK_DGRAM)

    def test_an_address_elsewhere_is_not(self, sockets: type[FakeSocket]) -> None:
        assert not real_own_address(PUBLIC_ADDRESS)
        assert not real_own_address("2606:4700:4700::1111")

    def test_an_address_without_a_route_is_not(self, sockets: type[FakeSocket]) -> None:
        assert not real_own_address("203.0.113.1")

    @pytest.mark.parametrize("text", ["", "dept.example.edu", "1.2.3", "%eth0"])
    def test_text_that_is_not_an_address_is_not(self, sockets: type[FakeSocket], text: str) -> None:
        assert not real_own_address(text)
        assert sockets.made == []

    def test_the_loopback_address_is_this_machine_for_real(self) -> None:
        """One real probe: a UDP socket is connected, which sends nothing."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("127.0.0.1", 9))
        except OSError as error:  # pragma: no cover - a sandbox without sockets
            pytest.skip(f"no UDP socket here: {error}")
        assert real_own_address("127.0.0.1")

    def test_it_is_what_a_client_uses_unless_told_otherwise(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        resolver: StubResolver,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        asked: list[str] = []

        def probe(address: str) -> bool:
            asked.append(address)
            return True

        monkeypatch.setattr("seminars.httpwire.system_own_address", probe)
        client = PoliteClient(
            http_settings, clock=fake_clock, sleep=fake_clock.sleep, resolver=resolver
        )
        with pytest.raises(ForbiddenHostError, match="an address of this machine"):
            client.get(PAGE)
        assert asked == [PUBLIC_ADDRESS]

    def test_the_test_suite_never_probes_for_real(self) -> None:
        from seminars import httpwire

        assert httpwire.system_own_address is not real_own_address
        assert httpwire.system_own_address(MY_ADDRESS) is False
