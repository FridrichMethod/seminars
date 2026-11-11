"""Tests for the URL rules: canonical hosts, what is refused, and the URL that is sent."""

from __future__ import annotations

import itertools
import time
from urllib.parse import unquote

import httpx
import pytest
import respx
from hypothesis import given, settings
from hypothesis import strategies as st

from fakes import FakeClock, StubResolver
from seminars.config import HttpSettings
from seminars.fixtures import FixtureNotFoundError, OfflineHttpClient
from seminars.http import (
    ForbiddenHostError,
    HttpError,
    PoliteClient,
    RobotsDisallowedError,
    UnsupportedSchemeError,
    canonical_host,
    is_public_host,
    split_url,
)
from seminars.httpurl import MAX_URL_LENGTH, UrlParts, parse_url

HOST = "https://dept.example.edu"
ROBOTS = f"{HOST}/robots.txt"
PAGE = f"{HOST}/events"
LOOPBACK_SPELLINGS = ["2130706433", "127.1", "0x7f.1", "0177.0.0.1", "0x7f000001", "017700000001"]
LOOPBACK_SPELLINGS += ["127.0.0.1.", "0X7F.1"]
"""Decimal, short, hexadecimal and octal ways to write 127.0.0.1."""
U_UMLAUT = chr(0xFC)
IDEOGRAPHIC_DOT = chr(0x3002)
SOFT_HYPHEN = chr(0xAD)
ZERO_WIDTH_SPACE = chr(0x200B)
ACCOUNT_OF = chr(0x2100)  # NFKC turns it into "a/c"
JAPANESE_NAME = chr(0x4F8B) + chr(0x3048) + ".jp"
NOT_HOSTS = ["", ".", "..", "a..b", ".example.edu", "exa mple.edu", "a/b", "a@b", "exam%70le.edu"]
NOT_HOSTS += ["x" * 64 + ".edu", ".".join(["abcdefgh"] * 29), "2606:4700::1111%eth0", "[::1]"]
NOT_HOSTS += [f"exa{SOFT_HYPHEN}mple.edu", f"exa{ZERO_WIDTH_SPACE}mple.edu"]
NOT_HOSTS += [f"example.edu{ACCOUNT_OF}", "::1::", "1::2::3", "example.edu:80"]
NOT_HOSTS += [f"b{U_UMLAUT}cher example", f"b{U_UMLAUT}cher@example", f"{U_UMLAUT}%41.example"]
NOT_HOSTS += ["1.1.1.256", "1.1.65536", "1.16777216", "4294967296", "256.1.1.1", "1.256.1"]


def no_robots(router: respx.MockRouter, host: str = HOST) -> respx.Route:
    return router.get(f"{host}/robots.txt").respond(404)


@pytest.fixture
def client(
    http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
) -> PoliteClient:
    return PoliteClient(http_settings, clock=fake_clock, sleep=fake_clock.sleep, resolver=resolver)


class TestPublicHosts:
    @pytest.mark.parametrize("host", [*LOOPBACK_SPELLINGS, "10.1", "3232235777", "0", "0x0", "0x"])
    def test_other_spellings_of_private_ipv4_addresses_are_not_public(self, host: str) -> None:
        """A resolver reads these as 127.0.0.1, 10.0.0.1, 192.168.1.1 and 0.0.0.0."""
        assert not is_public_host(host)

    @pytest.mark.parametrize(
        "host", ["1.2.3.4.5", "999.1.1.1", "example.123", "a.b.0x1f", "08.8.8.8", "1.2.3.4 5"]
    )
    def test_a_host_that_ends_in_a_number_but_is_no_address_is_refused(self, host: str) -> None:
        assert not is_public_host(host)
        with pytest.raises(ValueError, match="is not a host name or an IP address"):
            canonical_host(host)

    @pytest.mark.parametrize("host", ["134744072", "0x08080808", "8.8.2056", "010.010.010.010"])
    def test_other_spellings_of_public_ipv4_addresses_stay_public(self, host: str) -> None:
        assert is_public_host(host)
        assert canonical_host(host) == "8.8.8.8"

    @pytest.mark.parametrize("host", ["123.example.edu", "1e3.example.edu", "x1", "a.b.0x1g"])
    def test_names_with_digits_are_names(self, host: str) -> None:
        assert is_public_host(host)
        assert canonical_host(host) == host

    def test_full_width_characters_and_ideographic_dots_do_not_hide_an_address(self) -> None:
        """HTTP clients map these to ASCII, so they are judged in that form."""
        wide_digits = "".join(chr(0xFF10 + int(digit)) for digit in "127") + ".0.0.1"
        ideographic_dots = "127.0.0.1".replace(".", IDEOGRAPHIC_DOT)
        full_width_dots = "localhost" + chr(0xFF0E)
        for host in (wide_digits, ideographic_dots, full_width_dots):
            assert not is_public_host(host), ascii(host)
        assert canonical_host(ideographic_dots) == "127.0.0.1"
        assert canonical_host("WWW.Example.EDU.") == "www.example.edu"
        assert is_public_host("www" + IDEOGRAPHIC_DOT + "example" + IDEOGRAPHIC_DOT + "edu")

    def test_ipv4_mapped_ipv6_addresses_are_judged_by_their_ipv4_address(self) -> None:
        assert not is_public_host("::ffff:127.0.0.1")
        assert not is_public_host("::ffff:192.168.1.10")
        assert is_public_host("::ffff:8.8.8.8")
        assert is_public_host("2606:4700:4700::1111")

    @pytest.mark.parametrize(
        "host", ["64:ff9b::7f00:1", "::127.0.0.1", "::ffff:0:10.0.0.5", "224.0.0.1", "ff02::1"]
    )
    def test_other_wrapped_ipv4_addresses_and_multicast_are_not_public(self, host: str) -> None:
        assert not is_public_host(host)
        with pytest.raises(ForbiddenHostError):
            split_url(f"http://[{host}]/" if ":" in host else f"http://{host}/")

    @pytest.mark.parametrize("host", NOT_HOSTS, ids=ascii)
    def test_text_that_is_not_a_host_is_not_a_public_host(self, host: str) -> None:
        assert not is_public_host(host)
        with pytest.raises(ValueError, match="is not a host name or an IP address"):
            canonical_host(host)


class TestCanonicalHost:
    @pytest.mark.parametrize(
        ("host", "canonical"),
        [
            ("Dept.Example.EDU", "dept.example.edu"),
            ("dept.example.edu.", "dept.example.edu"),
            ("a_b.example.com", "a_b.example.com"),
            ("-x.example.com", "-x.example.com"),
            ("x" * 63 + ".edu", "x" * 63 + ".edu"),
            (f"b{U_UMLAUT}cher.example", "xn--bcher-kva.example"),
            (f"B{U_UMLAUT.upper()}CHER.Example.", "xn--bcher-kva.example"),
            ("xn--bcher-kva.example", "xn--bcher-kva.example"),
            (JAPANESE_NAME, "xn--r8jz45g.jp"),
            ("xn--r8jz45g.jp", "xn--r8jz45g.jp"),
            ("1.1.1.1", "1.1.1.1"),
            ("0x1.1.1.1", "1.1.1.1"),
            ("0x01.1.1.1", "1.1.1.1"),
            ("16843009", "1.1.1.1"),
            ("1.1.257", "1.1.1.1"),
            ("01.01.01.01", "1.1.1.1"),
            ("0x01010101", "1.1.1.1"),
            ("2606:4700:4700:0:0:0:0:1111", "2606:4700:4700::1111"),
            ("2606:4700:4700::1111", "2606:4700:4700::1111"),
            ("2606:4700:4700::0:1111", "2606:4700:4700::1111"),
            ("2606:4700:4700::ABCD", "2606:4700:4700::abcd"),
        ],
    )
    def test_every_spelling_has_one_canonical_form(self, host: str, canonical: str) -> None:
        assert canonical_host(host) == canonical
        assert canonical_host(canonical) == canonical


class TestHostileHosts:
    def test_a_very_long_host_is_refused_without_being_encoded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The IDNA encoder is not meant for text of arbitrary length."""
        encoded: list[str] = []

        def recording(url: str) -> httpx.URL:
            encoded.append(url)
            return real_url(url)

        real_url = httpx.URL
        monkeypatch.setattr(httpx, "URL", recording)
        marks = chr(0x301) + chr(0x316)
        hosts = ["a" + marks * 4000 + ".edu", U_UMLAUT * 64 + ".edu", (U_UMLAUT * 60 + ".") * 5]
        started = time.perf_counter()
        for host in hosts:
            with pytest.raises(ValueError, match="is not a host name or an IP address"):
                canonical_host(host)
            with pytest.raises(UnsupportedSchemeError):
                parse_url(f"https://{host[:8000]}/x")
        assert time.perf_counter() - started < 1.0
        assert encoded == []
        assert canonical_host(U_UMLAUT * 20 + ".edu").startswith("xn--")
        assert len(encoded) == 1


def sent_host(url: str) -> str:
    """The host a request for ``url`` goes to, as the HTTP library works it out."""
    return httpx.URL(url).raw_host.decode("ascii")


SEPARATORS = ["@", "\\", "#", "?", "/", ":", "[", "]", "%", " ", "\t", ";", "&", "%40", "%2f"]
SEPARATORS += [
    "%5c",
    "%23",
    "..",
    "\x0b",
    "\x7f",
    chr(0xFF20),
    chr(0xFF0F),
    chr(0xFF1A),
    chr(0x2044),
]
SEPARATORS += [chr(0xFE55), chr(0xFF03), ".", "-", "_", chr(0x3002), chr(0xAD), "xn--"]
FUZZ_HOSTS = [
    "a.example",
    "b.example",
    "1.1.1.1",
    "8.8.8.8",
    "0x8.8.8.8",
    f"b{U_UMLAUT}cher.example",
]
URL_PIECES = st.sampled_from([*SEPARATORS, *FUZZ_HOSTS, "http://", "https://", "//", "443", "x"])


class TestWhatIsCheckedIsWhatIsSent:
    """Two URL parsers that disagree on the host are how a check gets bypassed.

    The client never sends a URL as it was written. It sends the canonical
    URL, and for that URL the HTTP library must find exactly the host,
    port and path that were checked.
    """

    @staticmethod
    def check(url: str) -> bool:
        try:
            parts = parse_url(url)
        except HttpError:
            return False
        sent = httpx.URL(parts.url)
        assert sent.raw_host.decode("ascii") == parts.host, ascii(url)
        assert sent.userinfo == b"", ascii(url)
        assert sent.scheme in ("http", "https")
        origin = f"{sent.scheme}://{sent.netloc.decode('ascii')}"
        assert origin == parts.origin, ascii(url)
        assert unquote(sent.raw_path.decode("ascii")) == unquote(parts.path), ascii(url)
        assert is_public_host(parts.host)
        assert parse_url(parts.url) == parts
        return True

    def test_for_hosts_glued_together_in_every_way(self) -> None:
        accepted = 0
        for scheme in ("http://", "HTTPS://", "http:/", "http:", "http:\\\\", " http://"):
            for first, second in itertools.permutations(FUZZ_HOSTS[:4], 2):
                for one in SEPARATORS:
                    accepted += self.check(f"{scheme}{first}{one}{second}/p")
                    for other in SEPARATORS:
                        accepted += self.check(f"{scheme}{first}{one}{other}{second}/p")
        assert accepted > 1000

    @settings(max_examples=500, deadline=None)
    @given(st.lists(URL_PIECES, min_size=1, max_size=9).map("".join))
    def test_for_urls_made_of_arbitrary_pieces(self, url: str) -> None:
        self.check(url)

    @settings(max_examples=300, deadline=None)
    @given(st.text(max_size=30))
    def test_for_arbitrary_text_after_a_scheme(self, tail: str) -> None:
        self.check("https://" + tail)
        self.check("http://example.edu" + tail)


class TestParseUrl:
    def test_the_parts_of_a_url(self) -> None:
        assert parse_url("HTTPS://Dept.Example.EDU./events?page=1#today") == UrlParts(
            url="https://dept.example.edu/events?page=1",
            origin="https://dept.example.edu",
            host="dept.example.edu",
            path="/events?page=1",
        )
        assert split_url("HTTPS://Dept.Example.EDU./events?page=1#today") == (
            "https://dept.example.edu",
            "dept.example.edu",
            "/events?page=1",
        )

    @pytest.mark.parametrize(
        ("url", "canonical", "origin"),
        [
            ("https://example.edu", "https://example.edu/", "https://example.edu"),
            ("https://example.edu:443/x", "https://example.edu/x", "https://example.edu"),
            ("http://example.edu:80/x", "http://example.edu/x", "http://example.edu"),
            ("http://example.edu:443/x", "http://example.edu:443/x", "http://example.edu:443"),
            (
                "https://example.edu:8443/x",
                "https://example.edu:8443/x",
                "https://example.edu:8443",
            ),
            (
                "https://example.edu:08443/x",
                "https://example.edu:8443/x",
                "https://example.edu:8443",
            ),
            ("https://example.edu:/x", "https://example.edu/x", "https://example.edu"),
            ("https://0x8.8.8.8/x", "https://8.8.8.8/x", "https://8.8.8.8"),
            ("https://134744072/x", "https://8.8.8.8/x", "https://8.8.8.8"),
            (
                "http://[2606:4700:4700:0:0:0:0:1111]:8080/status",
                "http://[2606:4700:4700::1111]:8080/status",
                "http://[2606:4700:4700::1111]:8080",
            ),
            (
                f"https://b{U_UMLAUT}cher.example/katalog?q={U_UMLAUT}",
                f"https://xn--bcher-kva.example/katalog?q={U_UMLAUT}",
                "https://xn--bcher-kva.example",
            ),
            ("https://example.edu?a=1", "https://example.edu/?a=1", "https://example.edu"),
            ("https://example.edu/a%2Fb?x=%5B1%5D", "https://example.edu/a%2Fb?x=%5B1%5D", None),
            ("https://example.edu#@127.0.0.1/x", "https://example.edu/", None),
            ("https://example.edu?@127.0.0.1/x", "https://example.edu/?@127.0.0.1/x", None),
        ],
    )
    def test_the_canonical_url(self, url: str, canonical: str, origin: str | None) -> None:
        parts = parse_url(url)
        assert parts.url == canonical
        assert parts.origin == (origin or "https://example.edu")
        assert parts.url == parts.origin + parts.path
        assert parse_url(parts.url) == parts

    @pytest.mark.parametrize(
        "url",
        [
            "https://user:pass@example.edu/x",
            "https://user@example.edu/x",
            "https://@example.edu/x",
            "https://example.edu\\@127.0.0.1/x",
            "https://127.0.0.1\\@example.edu/x",
            "https://example.edu%2f@127.0.0.1/x",
        ],
    )
    def test_a_url_with_a_user_name_or_password_is_refused(self, url: str) -> None:
        with pytest.raises(UnsupportedSchemeError, match="user name or password"):
            parse_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.edu:0/x",
            "https://example.edu:65536/x",
            "https://example.edu:99999/x",
            "https://example.edu:-1/x",
            "https://example.edu:abc/x",
            "https://example.edu:80:80/x",
            "https://[/x",
            "https://[::1/x",
            "https://]/x",
            f"https://example.edu{ACCOUNT_OF}/x",
            "https://example.edu\t.evil.example/x",
            "https://example.edu/x\ny",
            "https://example.edu/a\x00b",
            "https://example.edu/x\x1b[31m",
            "https://example.edu/x\x7f",
            "https://exa mple.edu/x",
            "https://exam%70le.edu/x",
            "https://example.edu\\.evil.example/x",
            "https://a..edu/x",
            "https://.edu/x",
            "https://" + "a" * 64 + ".edu/x",
            "https://" + "a" * 300 + ".edu/x",
            "https://" + ".".join(["abcdefgh"] * 29) + "/x",
            f"https://exa{SOFT_HYPHEN}mple.edu/x",
            f"https://exa{ZERO_WIDTH_SPACE}mple.edu/x",
            "https://[fe80::1%25eth0]/x",
            "https://[2606:4700:4700::1111%25eth0]/x",
            "https://1.2.3.4.5/x",
            "https://example.123/x",
            "https://example.edu/" + "x" * MAX_URL_LENGTH,
        ],
        ids=ascii,
    )
    def test_what_is_not_a_url_is_refused_as_such(self, url: str) -> None:
        with pytest.raises(UnsupportedSchemeError, match="invalid URL") as caught:
            parse_url(url)
        assert isinstance(caught.value, HttpError)
        assert str(caught.value).isascii()
        assert len(str(caught.value)) < 400

    @pytest.mark.parametrize(
        ("written", "sent"),
        [
            ("/public/../private/x", "/private/x"),
            ("/./private/x", "/private/x"),
            ("/a/b/../../c", "/c"),
            ("/a/b/../c/./d", "/a/c/d"),
            ("/a/..", "/"),
            ("/a/b/..", "/a/"),
            ("/a/.", "/a/"),
            ("/..", "/"),
            ("/../../x", "/x"),
            ("/.", "/"),
            ("/a/%2e%2e/b", "/b"),
            ("/a/%2E%2e/b", "/b"),
            ("/a/.%2e/b", "/b"),
            ("/a/%2E./b", "/b"),
            ("/a/%2e/b", "/a/b"),
            ("/a/b/%2e%2e", "/a/"),
            ("/a//b", "/a//b"),
            ("/a/.../b", "/a/.../b"),
            ("/a/..b/c", "/a/..b/c"),
            ("/a/b../c", "/a/b../c"),
            ("/a%2f../b", "/a%2f../b"),
            ("/a/b/..?x=../y&z=/./w", "/a/?x=../y&z=/./w"),
            ("/a/b/", "/a/b/"),
            ("", "/"),
        ],
    )
    def test_dot_segments_are_removed_before_anything_looks_at_the_path(
        self, written: str, sent: str
    ) -> None:
        """Browsers and servers read %2e as a dot, so both spellings are removed."""
        parts = parse_url(f"https://example.edu{written}")
        assert parts.path == sent
        assert parts.url == f"https://example.edu{sent}"
        assert parse_url(parts.url) == parts

    @pytest.mark.parametrize(
        "url", ["https://localhost./x", "https://foo.localhost/x", "https://LOCALHOST:8443/"]
    )
    def test_local_names_are_forbidden_in_any_spelling(self, url: str) -> None:
        with pytest.raises(ForbiddenHostError):
            parse_url(url)


class TestTheUrlThatIsSent:
    """The client sends the canonical URL, so what was checked is what goes out."""

    @staticmethod
    def build(
        settings: HttpSettings, clock: FakeClock, resolver: StubResolver
    ) -> tuple[PoliteClient, list[httpx.Request]]:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(404 if request.url.path == "/robots.txt" else 200, text="ok")

        client = PoliteClient(
            settings,
            clock=clock,
            sleep=clock.sleep,
            transport=httpx.MockTransport(handler),
            resolver=resolver,
        )
        return client, requests

    def test_every_spelling_of_a_host_shares_one_queue_one_budget_and_one_robots_file(
        self, http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
    ) -> None:
        client, requests = self.build(http_settings, fake_clock, resolver)
        spellings = [
            PAGE,
            "https://DEPT.example.edu./events",
            "HTTPS://dept.example.edu:443/events",
        ]
        spellings.append("https://dept" + IDEOGRAPHIC_DOT + "example.edu/events#top")
        for url in spellings:
            document = client.get(url)
            assert (document.url, document.final_url) == (url, url)
        assert [str(request.url) for request in requests] == [ROBOTS, PAGE, PAGE, PAGE, PAGE]
        assert client.request_counts() == {"dept.example.edu": 4}
        assert set(resolver.calls) == {"dept.example.edu"}
        assert fake_clock.sleeps == [1.0] * 4

    def test_the_unicode_and_the_ascii_spelling_of_a_name_are_one_host(
        self, http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
    ) -> None:
        client, requests = self.build(http_settings, fake_clock, resolver)
        client.get(f"https://{JAPANESE_NAME}/a")
        client.get("https://xn--r8jz45g.jp/b")
        assert [request.url.raw_host for request in requests] == [b"xn--r8jz45g.jp"] * 3
        assert [request.url.path for request in requests] == ["/robots.txt", "/a", "/b"]
        assert client.request_counts() == {"xn--r8jz45g.jp": 2}

    def test_numeric_spellings_of_one_address_are_one_host(
        self, http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
    ) -> None:
        client, requests = self.build(
            http_settings.evolve(max_requests_per_host=3), fake_clock, resolver
        )
        outcomes = []
        for zeros in range(6):
            try:
                client.get(f"https://0x{'0' * zeros}1.1.1.1/p")
                outcomes.append("ok")
            except HttpError as error:
                outcomes.append(type(error).__name__)
        assert outcomes == ["ok"] * 3 + ["HostBudgetExceededError"] * 3
        assert {request.url.host for request in requests} == {"1.1.1.1"}
        assert len(requests) == 4
        assert client.request_counts() == {"1.1.1.1": 3}

    def test_no_credentials_from_a_link_ever_leave(
        self, http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
    ) -> None:
        client, requests = self.build(http_settings, fake_clock, resolver)
        for number in range(5):
            with pytest.raises(UnsupportedSchemeError, match="user name or password"):
                client.get(f"https://u{number}:guess{number}@victim.example.edu/p{number}")
        assert requests == []
        assert resolver.calls == []
        assert client.request_counts() == {}

    def test_a_redirect_with_credentials_is_not_followed(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        no_robots(respx_router)
        location = "https://admin:hunter2@www.example.edu/private"
        respx_router.get(PAGE).respond(302, headers={"Location": location})
        with pytest.raises(UnsupportedSchemeError, match="user name or password"):
            client.get(PAGE)
        assert respx_router.calls.call_count == 2

    def test_a_cached_page_is_shared_by_all_spellings(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        resolver: StubResolver,
        respx_router: respx.MockRouter,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        no_robots(respx_router)
        page = respx_router.get(PAGE).mock(
            side_effect=[
                httpx.Response(200, text="body", headers={"ETag": '"v1"'}),
                httpx.Response(304),
            ]
        )
        client = PoliteClient(
            http_settings,
            cache_dir=tmp_path_factory.mktemp("cache"),
            clock=fake_clock,
            sleep=fake_clock.sleep,
            resolver=resolver,
        )
        client.get(PAGE)
        again = client.get("https://Dept.Example.edu:443/events#later")
        assert (again.from_cache, again.text) == (True, "body\n")
        assert page.calls[1].request.headers["if-none-match"] == '"v1"'


DOT_SPELLINGS = ["/public/../private/x", "/./private/x", "/private/./x", "/public/%2e%2e/private/x"]
DOT_SPELLINGS += ["/a/b/../../private/x", "/public/%2E./private/x", "/private/y/../x"]
"""Ways to write /private/x that do not start with /private/ as text."""


class TestDotSegmentsAndRobots:
    """robots.txt is matched against the path that is sent, not against the path as written."""

    @pytest.mark.parametrize("path", DOT_SPELLINGS)
    def test_a_disallowed_path_cannot_be_reached_through_dot_segments(
        self, client: PoliteClient, respx_router: respx.MockRouter, path: str
    ) -> None:
        respx_router.get(ROBOTS).respond(200, text="User-agent: *\nDisallow: /private/\n")
        private = respx_router.get(url__startswith=f"{HOST}/").respond(200, text="secret")
        with pytest.raises(RobotsDisallowedError):
            client.get(f"{HOST}{path}")
        assert private.call_count == 0

    @pytest.mark.parametrize("path", DOT_SPELLINGS)
    def test_nor_through_an_absolute_redirect(
        self, client: PoliteClient, respx_router: respx.MockRouter, path: str
    ) -> None:
        respx_router.get(ROBOTS).respond(200, text="User-agent: *\nDisallow: /private/\n")
        respx_router.get(PAGE).respond(302, headers={"Location": f"{HOST}{path}"})
        private = respx_router.get(url__startswith=f"{HOST}/p").respond(200, text="secret")
        with pytest.raises(RobotsDisallowedError):
            client.get(PAGE)
        assert private.call_count == 0

    def test_an_allowed_path_is_requested_without_its_dot_segments(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(ROBOTS).respond(200, text="User-agent: *\nDisallow: /private/\n")
        page = respx_router.get(PAGE).respond(200, text="ok")
        written = f"{HOST}/private/../events"
        document = client.get(written)
        assert (document.url, document.final_url, document.text) == (written, written, "ok\n")
        assert page.calls.last.request.url.raw_path == b"/events"


class TestOfflineClient:
    def test_applies_the_same_url_rules(self) -> None:
        client = OfflineHttpClient([])
        with pytest.raises(UnsupportedSchemeError, match="user name or password"):
            client.get("https://user:pass@example.edu/x")
        with pytest.raises(UnsupportedSchemeError, match="invalid URL"):
            client.get("https://example.edu:99999/x")
        with pytest.raises(ForbiddenHostError):
            client.get("https://0x7f.1/x")
        with pytest.raises(FixtureNotFoundError):
            client.get("https://example.edu/x")
        assert client.requests == ["https://example.edu/x"]
