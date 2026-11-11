"""Tests for the polite HTTP client: robots, rate limit, budget and retries."""

from __future__ import annotations

import gzip
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
import respx

from fakes import FakeClock
from seminars.config import HttpSettings
from seminars.http import (
    CrawlDelayTooLongError,
    ForbiddenHostError,
    HostBudgetExceededError,
    HttpClient,
    HttpError,
    HttpRequestError,
    HttpStatusError,
    PoliteClient,
    ResponseTooLargeError,
    RobotsDisallowedError,
    RobotsUnavailableError,
    UnsupportedContentTypeError,
    UnsupportedSchemeError,
    canonical_host,
    decode_body,
    is_binary_media_type,
    is_public_host,
    media_type,
    split_url,
)

HOST = "https://dept.example.edu"
ROBOTS = f"{HOST}/robots.txt"
PAGE = f"{HOST}/events"

Responder = Callable[[httpx.Request], httpx.Response]

LOOPBACK_SPELLINGS = ["2130706433", "127.1", "0x7f.1", "0177.0.0.1", "0x7f000001", "017700000001"]
LOOPBACK_SPELLINGS += ["127.0.0.1.", "0X7F.1"]
"""Decimal, short, hexadecimal and octal ways to write 127.0.0.1."""


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
        assert document.text == "<html>hi</html>\n"
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

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8000/events",
            "http://127.0.0.1/admin",
            "http://[::1]/",
            "http://10.0.0.5/",
            "http://192.168.1.10/feed",
            "http://169.254.169.254/latest/meta-data/",
            "https://printer.local/",
            "https://build.internal/secrets",
            "http://2130706433/",
            "http://127.1/admin",
            "http://0x7f.1/",
            "http://0177.0.0.1/",
            "http://0xa9fea9fe/latest/meta-data/",
            "http://[::ffff:127.0.0.1]/",
            "http://[::ffff:10.0.0.5]/",
        ],
    )
    def test_local_and_private_addresses_are_refused(
        self, client: PoliteClient, respx_router: respx.MockRouter, url: str
    ) -> None:
        with pytest.raises(ForbiddenHostError, match="local or private network"):
            client.get(url)
        assert respx_router.calls.call_count == 0

    def test_redirect_to_a_private_address_is_refused(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        no_robots(respx_router)
        respx_router.get(PAGE).respond(302, headers={"Location": "http://169.254.169.254/"})
        with pytest.raises(ForbiddenHostError):
            client.get(PAGE)

    def test_public_hosts(self) -> None:
        assert is_public_host("events.stanford.edu")
        assert is_public_host("8.8.8.8")
        assert is_public_host("localhost.example.edu")
        assert not is_public_host("LOCALHOST.")
        assert not is_public_host("fe80::1")
        assert not is_public_host("")

    @pytest.mark.parametrize("host", [*LOOPBACK_SPELLINGS, "10.1", "3232235777", "0", "0x0"])
    def test_other_spellings_of_private_ipv4_addresses_are_not_public(self, host: str) -> None:
        """The resolver reads these as 127.0.0.1, 10.0.0.1, 192.168.1.1 and 0.0.0.0."""
        assert not is_public_host(host)

    @pytest.mark.parametrize("host", ["1.2.3.4.5", "999.1.1.1", "example.123", "a.b.0x1f", "0x"])
    def test_a_host_that_ends_in_a_number_but_is_no_address_is_refused(self, host: str) -> None:
        assert not is_public_host(host)

    @pytest.mark.parametrize("host", ["134744072", "0x08080808", "8.8.2056", "010.010.010.010"])
    def test_other_spellings_of_public_ipv4_addresses_stay_public(self, host: str) -> None:
        assert is_public_host(host)

    @pytest.mark.parametrize("host", ["123.example.edu", "1e3.example.edu", "x1", "a.b.0x1g"])
    def test_names_with_digits_are_names(self, host: str) -> None:
        assert is_public_host(host)

    def test_full_width_characters_and_ideographic_dots_do_not_hide_an_address(self) -> None:
        """HTTP clients map these to ASCII, so they are judged in that form."""
        wide_digits = "".join(chr(0xFF10 + int(digit)) for digit in "127") + ".0.0.1"
        ideographic_dots = "127.0.0.1".replace(".", chr(0x3002))
        full_width_dots = "localhost" + chr(0xFF0E)
        for host in (wide_digits, ideographic_dots, full_width_dots):
            assert not is_public_host(host), ascii(host)
        assert canonical_host(ideographic_dots) == "127.0.0.1"
        assert canonical_host("WWW.Example.EDU.") == "www.example.edu"
        assert is_public_host("www" + chr(0x3002) + "example" + chr(0x3002) + "edu")

    def test_every_spelling_of_a_host_shares_one_queue(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        with pytest.raises(ForbiddenHostError):
            client.get("http://127" + chr(0x3002) + "0.0.1/")
        origin, host, path = split_url("https://Dept.Example.EDU./events?page=1")
        assert (origin, host, path) == (
            "https://dept.example.edu.",
            "dept.example.edu",
            "/events?page=1",
        )
        no_robots(respx_router)
        respx_router.get(url__startswith=f"{HOST}").respond(200, text="ok")
        respx_router.get(url__startswith="https://dept.example.edu.").respond(200, text="ok")
        respx_router.get("https://dept.example.edu./robots.txt").respond(404)
        client.get(PAGE)
        client.get("https://DEPT.example.edu./events")
        assert client.request_counts() == {"dept.example.edu": 2}

    def test_ipv4_mapped_ipv6_addresses_are_judged_by_their_ipv4_address(self) -> None:
        assert not is_public_host("::ffff:127.0.0.1")
        assert not is_public_host("::ffff:192.168.1.10")
        assert is_public_host("::ffff:8.8.8.8")
        assert is_public_host("2606:4700:4700::1111")

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
        assert (document.status, document.text) == (404, "gone\n")

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


NAME = "Poincar" + chr(0xE9)  # e with acute accent
QUOTED = chr(0x201C) + "flow" + chr(0x201D)  # curly double quotes
REPLACEMENT = chr(0xFFFD)
BOM = chr(0xFEFF)


class TestDecodeBody:
    def test_charset_of_the_content_type_header(self) -> None:
        assert decode_body(NAME.encode("latin-1"), "text/html; charset=ISO-8859-1") == NAME
        assert decode_body(NAME.encode("utf-8"), 'text/html; charset="utf-8"') == NAME
        assert decode_body(NAME.encode("utf-16-le"), "text/plain;charset=utf-16le") == NAME

    @pytest.mark.parametrize(
        "declaration",
        [
            '<meta charset="iso-8859-1">',
            "<META CHARSET=ISO-8859-1>",
            "<meta http-equiv='Content-Type' content='text/html; charset=windows-1252'>",
            '<meta content="text/html; charset=latin1" http-equiv="content-type" />',
        ],
    )
    def test_html_meta_charset(self, declaration: str) -> None:
        page = f"<html><head>{declaration}<title>x</title></head><body>{NAME}</body></html>"
        assert decode_body(page.encode("latin-1"), "text/html") == page
        assert decode_body(page.encode("latin-1"), None) == page

    @pytest.mark.parametrize("quote", ['"', "'"])
    def test_xml_declaration(self, quote: str) -> None:
        feed = (
            f"<?xml version={quote}1.0{quote} encoding={quote}ISO-8859-1{quote}?>\n"
            f"<rss><channel><title>{NAME}</title></channel></rss>"
        )
        assert decode_body(feed.encode("latin-1"), "application/rss+xml") == feed
        assert decode_body(b"\n  " + feed.encode("latin-1"), "text/xml") == "\n  " + feed

    def test_undeclared_text_is_utf8_when_it_is_valid_utf8(self) -> None:
        assert decode_body(NAME.encode("utf-8"), "text/html") == NAME
        assert decode_body(b"plain ascii", None) == "plain ascii"
        assert decode_body(b"", None) == ""

    def test_undeclared_text_that_is_not_utf8_is_windows_1252(self) -> None:
        text = f"{NAME} on {QUOTED}"
        assert decode_body(text.encode("cp1252"), "text/html") == text
        assert REPLACEMENT not in decode_body(NAME.encode("latin-1"), None)

    def test_latin1_labels_mean_windows_1252_as_in_browsers(self) -> None:
        body = QUOTED.encode("cp1252")
        assert decode_body(body, "text/html; charset=iso-8859-1") == QUOTED
        assert decode_body(b'<meta charset="us-ascii">' + body, None).endswith(QUOTED)

    def test_byte_order_mark_decides_and_is_consumed(self) -> None:
        assert decode_body((BOM + NAME).encode("utf-8"), "text/html") == NAME
        assert decode_body((BOM + NAME).encode("utf-8"), "text/html; charset=iso-8859-1") == NAME
        assert decode_body(NAME.encode("utf-16"), "text/html") == NAME
        assert decode_body((BOM + NAME).encode("utf-16-be"), None) == NAME
        assert decode_body(NAME.encode("utf-32"), None) == NAME

    def test_header_wins_over_a_declaration_in_the_document(self) -> None:
        page = f'<meta charset="iso-8859-1">{NAME}'
        assert decode_body(page.encode("utf-8"), "text/html; charset=utf-8") == page

    def test_only_the_start_of_the_document_is_searched(self) -> None:
        late = "<p>" + "x" * 3000 + '</p><meta charset="koi8-r">' + NAME
        assert decode_body(late.encode("utf-8"), "text/html") == late

    @pytest.mark.parametrize(
        "label", ["x-user-defined-nonsense", "base64", "idna", "undefined", "utf-7", "UTF7"]
    )
    def test_unknown_and_unusable_labels_are_ignored(self, label: str) -> None:
        page = f'<meta charset="{label}">{NAME}'
        assert decode_body(page.encode("utf-8"), f"text/html; charset={label}") == page
        assert decode_body(page.encode("utf-8"), "text/html") == page

    def test_utf7_is_never_used(self) -> None:
        page = '<meta name="description" content="charset=utf-7 +ADw-script+AD4-">'
        assert decode_body(page.encode("ascii"), "text/html") == page
        assert decode_body(page.encode("ascii"), "text/html; charset=utf-7") == page

    def test_declared_16_bit_encoding_in_ascii_readable_text_is_ignored(self) -> None:
        page = f'<meta charset="utf-16">{NAME}'
        assert decode_body(page.encode("utf-8"), "text/html") == page

    def test_invalid_bytes_become_replacement_characters(self) -> None:
        assert decode_body(b"ok \xff\xfe", "text/plain; charset=utf-8") == "ok " + REPLACEMENT * 2


class TestEncoding:
    def test_latin1_page_without_header_charset(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        page = f'<html><head><meta charset="iso-8859-1"></head><body>{NAME}</body></html>'
        no_robots(respx_router)
        respx_router.get(PAGE).respond(
            200, content=page.encode("latin-1"), headers={"Content-Type": "text/html"}
        )
        assert client.get(PAGE).text == page + "\n"

    def test_latin1_feed_with_an_xml_declaration(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        feed = f'<?xml version="1.0" encoding="ISO-8859-1"?><rss><title>{NAME}</title></rss>'
        no_robots(respx_router)
        respx_router.get(PAGE).respond(
            200, content=feed.encode("latin-1"), headers={"Content-Type": "application/rss+xml"}
        )
        document = client.get(PAGE)
        assert NAME in document.text
        assert REPLACEMENT not in document.text

    def test_old_page_without_any_declaration(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        no_robots(respx_router)
        respx_router.get(PAGE).respond(200, content=f"<p>{NAME}</p>".encode("latin-1"))
        assert client.get(PAGE).text == f"<p>{NAME}</p>\n"

    def test_cached_body_keeps_its_decoded_text(
        self,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        respx_router: respx.MockRouter,
        tmp_path: Path,
    ) -> None:
        page = f'<meta charset="iso-8859-1">{NAME}'
        no_robots(respx_router)
        respx_router.get(PAGE).mock(
            side_effect=[
                httpx.Response(200, content=page.encode("latin-1"), headers={"ETag": '"v1"'}),
                httpx.Response(304),
            ]
        )
        client = PoliteClient(
            http_settings, cache_dir=tmp_path, clock=fake_clock, sleep=fake_clock.sleep
        )
        client.get(PAGE)
        again = client.get(PAGE)
        assert (again.from_cache, again.text) == (True, page + "\n")


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
        assert client.get(PAGE).text == "ok\n"

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

    def test_robots_with_a_byte_order_mark_is_still_obeyed(
        self, client: PoliteClient, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        body = b"\xef\xbb\xbfUser-agent: *\nCrawl-delay: 30\nDisallow: /private/\n"
        respx_router.get(ROBOTS).respond(200, content=body)
        private = respx_router.get(f"{HOST}/private/x").respond(200, text="secret")
        respx_router.get(PAGE).respond(200, text="ok")
        with pytest.raises(RobotsDisallowedError):
            client.get(f"{HOST}/private/x")
        assert private.call_count == 0
        client.get(PAGE)
        assert fake_clock.sleeps == [30.0]

    def test_missing_robots_allows_everything(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(ROBOTS).respond(403)
        respx_router.get(PAGE).respond(200, text="ok")
        assert client.get(PAGE).text == "ok\n"

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

    @pytest.mark.parametrize(
        "failure",
        [httpx.Response(503), httpx.Response(429), httpx.ConnectError("boom")],
        ids=["503", "429", "network"],
    )
    def test_unreachable_robots_is_remembered_for_the_run(
        self,
        client: PoliteClient,
        respx_router: respx.MockRouter,
        fake_clock: FakeClock,
        failure: httpx.Response | Exception,
    ) -> None:
        robots = respx_router.get(ROBOTS)
        if isinstance(failure, Exception):
            robots.mock(side_effect=failure)
        else:
            robots.mock(return_value=failure)
        page = respx_router.get(url__startswith=PAGE).respond(200, text="ok")
        messages = []
        for number in range(5):
            with pytest.raises(RobotsUnavailableError) as caught:
                client.get(f"{PAGE}?page={number}")
            messages.append(str(caught.value))
        assert robots.call_count == 4
        assert page.call_count == 0
        assert len(set(messages)) == 1
        assert messages[0].endswith(ROBOTS)
        assert fake_clock.sleeps == [1.0, 2.0, 4.0]
        assert client.request_counts() == {"dept.example.edu": 0}

    def test_robots_failure_is_not_carried_over_to_the_next_run(
        self, http_settings: HttpSettings, respx_router: respx.MockRouter, fake_clock: FakeClock
    ) -> None:
        robots = respx_router.get(ROBOTS).mock(
            side_effect=[httpx.Response(503)] * 4 + [httpx.Response(404)]
        )
        respx_router.get(PAGE).respond(200, text="ok")
        first = PoliteClient(http_settings, clock=fake_clock, sleep=fake_clock.sleep)
        with pytest.raises(RobotsUnavailableError):
            first.get(PAGE)
        second = PoliteClient(http_settings, clock=fake_clock, sleep=fake_clock.sleep)
        assert second.get(PAGE).status == 200
        assert robots.call_count == 5

    def test_robots_failure_is_per_origin(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        plain = "http://dept.example.edu"
        broken = respx_router.get(ROBOTS).respond(503)
        respx_router.get(f"{plain}/robots.txt").respond(404)
        respx_router.get(f"{plain}/events").respond(200, text="ok")
        with pytest.raises(RobotsUnavailableError):
            client.get(PAGE)
        assert client.get(f"{plain}/events").status == 200
        with pytest.raises(RobotsUnavailableError):
            client.get(PAGE)
        assert broken.call_count == 4

    def test_binary_robots_blocks_the_host_and_is_remembered(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        robots = respx_router.get(ROBOTS).respond(
            200, content=b"\x00\x01", headers={"Content-Type": "application/octet-stream"}
        )
        page = respx_router.get(PAGE).respond(200, text="ok")
        for _ in range(3):
            with pytest.raises(RobotsUnavailableError, match="not a text file"):
                client.get(PAGE)
        assert robots.call_count == 1
        assert page.call_count == 0


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
        robots_text = "User-agent: *\nCrawl-delay: 30\n"
        respx_router.get(f"{OTHER}/robots.txt").mock(
            side_effect=timed(fake_clock, times, httpx.Response(200, text=robots_text))
        )
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
            http_settings.evolve(max_redirects=2), clock=fake_clock, sleep=fake_clock.sleep
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


class TestBodyLimit:
    @pytest.fixture
    def small(self, http_settings: HttpSettings, fake_clock: FakeClock) -> PoliteClient:
        settings = http_settings.evolve(max_body_bytes=1000)
        return PoliteClient(settings, clock=fake_clock, sleep=fake_clock.sleep)

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
                settings, clock=clock, sleep=clock.sleep, transport=httpx.MockTransport(handler)
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
        assert client.get(PAGE).text == "a\n"
        assert client.get(f"{other}/events").text == "b\n"
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
        assert client.get(PAGE).text == "ok\n"
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
        assert client.get(PAGE).text == "ok\n"
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
        assert client.get(PAGE).text == "ok\n"
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
        assert client.get(PAGE).text == "ok\n"
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
