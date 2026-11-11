"""Tests for text of a site in messages and on the terminal: printable ASCII, on one line.

A site controls the headers it sends and, through links and redirects, the
URLs that are asked for. An escape sequence in such text can repaint the
terminal of whoever reads it, and a newline can forge a line of a log or a
workflow command of a CI system. Error messages and the output of the
fixtures command line therefore escape everything that is not printable
ASCII.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from fakes import FakeClock, FakeHttpClient, StubResolver
from seminars.config import HttpSettings
from seminars.fixtures import FixtureNotFoundError, main
from seminars.http import (
    CrawlDelayTooLongError,
    DeadlineExceededError,
    ForbiddenHostError,
    HostBudgetExceededError,
    HttpError,
    HttpRequestError,
    HttpStatusError,
    PoliteClient,
    ResponseTooLargeError,
    RobotsDisallowedError,
    RobotsUnavailableError,
    TooManyRedirectsError,
    UnsupportedContentEncodingError,
    UnsupportedContentTypeError,
    UnsupportedSchemeError,
    media_type,
)
from seminars.textrules import ascii_printable, ascii_safe

ESC = "\x1b"
HOST = "https://www.example.edu"
PAGE = f"{HOST}/page"
IMAGE = f"{HOST}/img"
HOSTILE_TYPE = (
    f"text/html{ESC}[2K\rrecorded ok/all-good.html (HTTP 200, 1 bytes)\x08; charset=utf-8"
)
HOSTILE_IMAGE_TYPE = f"image/x{ESC}[2K{ESC}[1A{ESC}]0;pwned\x07"
HOSTILE_CODING = f"gzip{ESC}[31m\x08\x7f"
HOSTILE_URLS = [
    f"{HOST}/x\n::error file=app.py::forged annotation",
    f"{HOST}/\r{ESC}[1A{ESC}[2Kok",
    "x" * 8300 + "\n::warning::y",
    f"{HOST}/" + chr(0xD83D) + "\n::notice::z",
    f"ftp://files.example.edu/{ESC}c\x00\x7f",
    f"http://localhost/\n{ESC}[2J",
]


def printable(text: str) -> bool:
    """True when ``text`` consists of printable ASCII characters only."""
    return all(" " <= char <= "~" for char in text)


def lines_of(output: str) -> list[str]:
    """The lines of captured output, each checked to be printable; the last one must be ended."""
    assert output == "" or output.endswith("\n")
    lines = output.split("\n")[:-1]
    assert all(printable(line) for line in lines), output
    return lines


class TestAsciiPrintable:
    @pytest.mark.parametrize(
        ("text", "escaped"),
        [
            ("plain: text", "plain: text"),
            ("two\nlines", "two\\nlines"),
            ("carriage\rreturn", "carriage\\rreturn"),
            ("a\tb", "a\\tb"),
            (f"{ESC}[31mred{ESC}[0m", "\\x1b[31mred\\x1b[0m"),
            ("nul\x00bell\x07back\x08del\x7f", "nul\\x00bell\\x07back\\x08del\\x7f"),
            ("caf" + chr(0xE9), "caf\\xe9"),
            ("next line" + chr(0x85), "next line\\x85"),
            ("sun " + chr(0x2609), "sun \\u2609"),
            ("half " + chr(0xD83D), "half \\ud83d"),
            ("line" + chr(0x2028) + "separator", "line\\u2028separator"),
            ("a backslash \\ stays", "a backslash \\ stays"),
            ("", ""),
        ],
    )
    def test_examples(self, text: str, escaped: str) -> None:
        assert ascii_printable(text) == escaped

    @pytest.mark.parametrize("code", [*range(0x20), 0x7F])
    def test_every_control_character_is_escaped(self, code: int) -> None:
        escaped = ascii_printable(f"a{chr(code)}b")
        assert printable(escaped)
        assert escaped.startswith("a\\")
        assert escaped.endswith("b")

    @given(st.text())
    def test_the_result_is_printable_ascii_and_stays_as_it_is(self, text: str) -> None:
        escaped = ascii_printable(text)
        assert printable(escaped)
        assert ascii_printable(escaped) == escaped

    @given(st.text(alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E)))
    def test_printable_ascii_is_not_changed(self, text: str) -> None:
        assert ascii_printable(text) == text

    def test_it_escapes_what_ascii_safe_leaves_alone(self) -> None:
        assert ascii_safe("a\nb") == "a\nb"
        assert ascii_printable("a\nb") == "a\\nb"


class TestMediaType:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("text/html", "text/html"),
            ("Text/HTML; charset=utf-8", "text/html"),
            ("  application/ld+json  ", "application/ld+json"),
            ("application/vnd.api+json;profile=x", "application/vnd.api+json"),
            ("text/calendar ; charset=utf-8", "text/calendar"),
            ("application/x-www-form-urlencoded", "application/x-www-form-urlencoded"),
            ("image/svg+xml", "image/svg+xml"),
            ("\ttext/html\r\n", "text/html"),  # white space around the value is no part of it
            ("x!#$&^_.+-/y!#$&^_.+-", "x!#$&^_.+-/y!#$&^_.+-"),
        ],
    )
    def test_a_type_and_a_subtype_come_back_in_lower_case(self, header: str, expected: str) -> None:
        assert media_type(header) == expected

    @pytest.mark.parametrize(
        "header",
        [
            None,
            "",
            "   ",
            ";charset=utf-8",
            "text",
            "text/",
            "/html",
            "text/html/extra",
            "text html",
            "text/ html",
            "text/html, text/html",
            "text/html\ntext/plain",
            "*/*",
            'text/"html"',
            "text/<b>",
            "text/caf" + chr(0xE9),
            HOSTILE_TYPE,
            HOSTILE_IMAGE_TYPE,
            f"text/h{ESC}[31mtml\x08\x08",
            "text/html\x00",
            "text/html\x7f",
        ],
    )
    def test_anything_else_is_no_media_type(self, header: str | None) -> None:
        assert media_type(header) is None

    @given(st.text())
    def test_the_result_is_none_or_two_plain_tokens(self, header: str) -> None:
        result = media_type(header)
        if result is not None:
            assert printable(result)
            assert result.count("/") == 1
            assert result == result.lower()
            assert not set(result) & set(' ;,"<>()[]{}\\@:?=')


def errors_for(url: str) -> list[HttpError]:
    """One error of every kind for ``url``, with hostile text wherever a kind takes text."""
    return [
        HttpError(url, f"something{ESC}[2J\nwent wrong"),
        UnsupportedSchemeError(url, "invalid URL"),
        ForbiddenHostError(url, "refusing a local or private network address"),
        RobotsDisallowedError(url, "disallowed by robots.txt"),
        RobotsUnavailableError(url, "robots.txt is unreachable; host skipped"),
        CrawlDelayTooLongError(url, "robots.txt asks for a crawl delay"),
        HostBudgetExceededError(url, "request budget is exhausted"),
        HttpStatusError(url, 500),
        HttpRequestError(url, f"request failed after 4 attempt(s) (Odd{ESC}Error)"),
        DeadlineExceededError(url, "gave up after 600 s"),
        TooManyRedirectsError(url, "more than 5 redirects"),
        UnsupportedContentTypeError(url, HOSTILE_IMAGE_TYPE),
        UnsupportedContentTypeError(url, None),
        UnsupportedContentEncodingError(url, HOSTILE_CODING),
        ResponseTooLargeError(url, 5_000_000),
        FixtureNotFoundError(url, "offline mode: no fixture recorded"),
    ]


class TestErrorMessages:
    @pytest.mark.parametrize("url", [PAGE, *HOSTILE_URLS])
    def test_every_kind_of_error_has_a_message_of_one_printable_line(self, url: str) -> None:
        for error in errors_for(url):
            assert printable(str(error)), type(error).__name__
            assert error.url == url  # the attribute keeps the URL as it was given

    def test_what_was_escaped_can_still_be_read(self) -> None:
        url = HOSTILE_URLS[0]
        assert str(UnsupportedSchemeError(url, "invalid URL")) == (
            "invalid URL: https://www.example.edu/x\\n::error file=app.py::forged annotation"
        )
        assert str(UnsupportedContentEncodingError(PAGE, HOSTILE_CODING)) == (
            f"refusing content encoding gzip\\x1b[31m\\x08\\x7f: {PAGE}"
        )
        assert str(HttpStatusError(f"{HOST}/caf" + chr(0xE9), 404)) == (
            "HTTP 404: https://www.example.edu/caf\\xe9"
        )

    @given(st.text(), st.text())
    def test_no_text_gets_through_unescaped(self, url: str, message: str) -> None:
        assert printable(str(HttpError(url, message)))


Handler = Callable[[httpx.Request], httpx.Response]


def hostile_site(request: httpx.Request) -> httpx.Response:
    """A site whose every header value is an attack on whoever prints it."""
    if request.url.path == "/robots.txt":
        return httpx.Response(404)
    if request.url.path == "/img":
        return httpx.Response(200, content=b"x", headers={"content-type": HOSTILE_IMAGE_TYPE})
    if request.url.path == "/packed":
        return httpx.Response(200, content=b"x", headers={"content-encoding": HOSTILE_CODING})
    return httpx.Response(200, content=b"<html>x</html>", headers={"content-type": HOSTILE_TYPE})


@pytest.fixture
def client(http_settings: HttpSettings, fake_clock: FakeClock) -> PoliteClient:
    return PoliteClient(
        http_settings,
        clock=fake_clock,
        sleep=fake_clock.sleep,
        resolver=StubResolver(),
        transport=httpx.MockTransport(hostile_site),
    )


class TestTheLiveClient:
    @pytest.mark.parametrize("url", [PAGE, IMAGE])
    def test_a_content_type_that_is_no_media_type_is_not_handed_on(
        self, client: PoliteClient, url: str
    ) -> None:
        document = client.get(url)
        assert document.content_type is None

    def test_a_hostile_content_encoding_is_escaped_in_the_message(
        self, client: PoliteClient
    ) -> None:
        with pytest.raises(UnsupportedContentEncodingError) as caught:
            client.get(f"{HOST}/packed")
        assert printable(str(caught.value))
        assert "gzip\\x1b[31m" in str(caught.value)

    @pytest.mark.parametrize("url", HOSTILE_URLS)
    def test_a_refused_url_is_escaped_in_the_message(self, client: PoliteClient, url: str) -> None:
        with pytest.raises(HttpError) as caught:
            client.get(url)
        assert printable(str(caught.value))
        assert client.request_counts() == {}


def run(
    capsys: pytest.CaptureFixture[str], root: Path, http: object, *arguments: str
) -> tuple[int, list[str], list[str]]:
    """Run the fixtures command line; its exit status and the lines of stdout and stderr."""
    code = main(["--root", str(root), *arguments], http=http)  # type: ignore[arg-type]
    captured = capsys.readouterr()
    return code, lines_of(captured.out), lines_of(captured.err)


class TestCommandLine:
    def test_fetch_prints_nothing_a_site_or_a_url_could_use_against_the_terminal(
        self, client: PoliteClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(capsys, tmp_path, client, "fetch", PAGE, IMAGE, *HOSTILE_URLS)
        assert code == 1
        assert out == [
            f"fetched {PAGE}",
            "  status: 200",
            f"  final URL: {PAGE}",
            "  content type: unknown",
            "  bytes: 15",
            f"fetched {IMAGE}",
            "  status: 200",
            f"  final URL: {IMAGE}",
            "  content type: unknown",
            "  bytes: 2",
        ]
        assert len(err) == len(HOSTILE_URLS)  # one line per URL, however many a URL tries to be
        assert all(line.startswith("error: ") for line in err)
        assert err[0] == (
            "error: invalid URL: control characters: "
            "https://www.example.edu/x\\n::error file=app.py::forged annotation"
        )

    def test_fetch_escapes_what_any_client_hands_it(
        self, fake_http: FakeHttpClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        url = f"{HOST}/a{ESC}[2Jb"
        fake_http.add(url, "x", content_type=HOSTILE_TYPE, final_url=f"{HOST}/moved\rhere")
        code, out, err = run(capsys, tmp_path, fake_http, "fetch", url, f"{HOST}/missing\x07")
        assert code == 1
        assert out == [
            "fetched https://www.example.edu/a\\x1b[2Jb",
            "  status: 200",
            "  final URL: https://www.example.edu/moved\\rhere",
            "  content type: text/html\\x1b[2K\\rrecorded ok/all-good.html "
            "(HTTP 200, 1 bytes)\\x08; charset=utf-8",
            "  bytes: 2",
        ]
        assert len(err) == 1
        assert err[0].endswith("https://www.example.edu/missing\\x07")

    def test_record_prints_one_printable_line_per_url(
        self, client: PoliteClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        arguments = ["record", "--source-id", "probe", PAGE, IMAGE, *HOSTILE_URLS]
        code, out, err = run(capsys, tmp_path, client, *arguments)
        assert code == 1
        assert len(out) == 2
        assert all(line.startswith("recorded probe/") for line in out)
        assert len(err) == len(HOSTILE_URLS)
        manifest = (tmp_path / "tests" / "fixtures" / "probe" / "manifest.json").read_text(
            encoding="utf-8"
        )
        assert printable(manifest.replace("\n", ""))
        assert '"content_type": null' in manifest

    def test_record_escapes_the_reason_a_document_cannot_be_stored(
        self, fake_http: FakeHttpClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        url = f"{HOST}/binary\nforged line"
        fake_http.add(url, "\x00\x01", content_type="text/html")
        code, out, err = run(capsys, tmp_path, fake_http, "record", "--source-id", "probe", url)
        assert (code, out) == (1, [])
        assert err == [
            "error: refusing to store binary content of "
            "https://www.example.edu/binary\\nforged line"
        ]

    @pytest.mark.parametrize("command", [["record", "--source-id", "broken", PAGE], ["check"]])
    def test_a_broken_manifest_is_reported_on_one_line(
        self,
        fake_http: FakeHttpClient,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        command: list[str],
    ) -> None:
        """The reason comes from the validation of the manifest and has several lines."""
        broken = tmp_path / "tests" / "fixtures" / "broken"
        broken.mkdir(parents=True)
        (broken / "manifest.json").write_text(
            '{"schema_version": 1, "source_id": "broken", "entries": "\\u001b[2J\\nforged"}\n',
            encoding="utf-8",
        )
        code, out, err = run(capsys, tmp_path, fake_http, *command)
        assert code == 1
        problems = err if command[0] == "record" else out[:-1]
        assert len(problems) == 1
        assert "invalid fixture manifest" in problems[0]
        assert fake_http.requests == []

    def test_check_escapes_file_names_and_manifest_content(
        self, fake_http: FakeHttpClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_http.add(PAGE, "x")
        assert run(capsys, tmp_path, fake_http, "record", "--source-id", "probe", PAGE)[0] == 0
        directory = tmp_path / "tests" / "fixtures" / "probe"
        (directory / f"stray{ESC}[2K\nfixtures OK.txt").write_text("x\n", encoding="utf-8")
        broken = tmp_path / "tests" / "fixtures" / "broken"
        broken.mkdir()
        (broken / "manifest.json").write_text(
            '{"schema_version": 1, "source_id": "broken", "entries": "\\u001b[2J\\nfixtures OK"}\n',
            encoding="utf-8",
        )
        code, out, err = run(capsys, tmp_path, fake_http, "check")
        assert (code, err) == (1, [])
        assert len(out) == 3
        assert out[0].startswith("broken: ")
        assert out[1] == "probe: stray\\x1b[2K\\nfixtures OK.txt is not listed in the manifest"
        assert out[2] == "2 problem(s) found"
