"""Tests for decoding response bodies: which encoding is used, and which are never used."""

from __future__ import annotations

import codecs
import time
from pathlib import Path

import httpx
import pytest
import respx

from fakes import FakeClock, StubResolver
from seminars import httptext
from seminars.config import HttpSettings
from seminars.http import PoliteClient, decode_body

HOST = "https://dept.example.edu"
PAGE = f"{HOST}/events"


def no_robots(router: respx.MockRouter, host: str = HOST) -> respx.Route:
    return router.get(f"{host}/robots.txt").respond(404)


@pytest.fixture
def client(
    http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
) -> PoliteClient:
    return PoliteClient(http_settings, clock=fake_clock, sleep=fake_clock.sleep, resolver=resolver)


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
            http_settings,
            cache_dir=tmp_path,
            clock=fake_clock,
            sleep=fake_clock.sleep,
            resolver=StubResolver(),
        )
        client.get(PAGE)
        again = client.get(PAGE)
        assert (again.from_cache, again.text) == (True, page + "\n")


CYRILLIC = "".join(chr(code) for code in (0x41F, 0x440, 0x438, 0x432, 0x435, 0x442))
KANJI = chr(0x65E5) + chr(0x672C)
HANGUL = chr(0xD55C) + chr(0xAE00)
NOT_TEXT_ENCODINGS = ["punycode", "idna", "unicode_escape", "unicode-escape", "raw_unicode_escape"]
NOT_TEXT_ENCODINGS += ["base64", "zlib", "bz2", "hex", "rot13", "quopri", "uu", "undefined", "mbcs"]
NOT_WEB_ENCODINGS = ["utf-7", "hz", "iso-2022-kr", "utf-32", "utf-32le", "utf-8-sig", "cp037"]
NOT_WEB_ENCODINGS += ["cp500", "utf_16_le.evil", "x-user-defined", "latin1.something", "..", "-"]


class TestOnlyWebEncodings:
    """A label picks one of the encodings web pages use; anything else is as if it were absent."""

    @pytest.mark.parametrize(
        ("label", "text"),
        [
            ("utf-8", "Poincar" + chr(0xE9)),
            ("UTF8", KANJI),
            ("windows-1251", CYRILLIC),
            ("koi8-r", CYRILLIC),
            ("iso-8859-5", CYRILLIC),
            ("ISO-8859-2", chr(0x141) + chr(0xF3) + "d" + chr(0x17A)),
            ("iso-8859-15", "5 " + chr(0x20AC)),
            ("macintosh", "caf" + chr(0xE9)),
            ("shift_jis", KANJI),
            ("euc-jp", KANJI),
            ("iso-2022-jp", KANJI),
            ("gbk", KANJI),
            ("gb18030", KANJI),
            ("big5", KANJI),
            ("euc-kr", HANGUL),
            ("utf-16le", KANJI),
            ("utf-16be", CYRILLIC),
        ],
    )
    def test_the_encodings_of_the_web_are_used(self, label: str, text: str) -> None:
        body = text.encode(label)
        assert decode_body(body, f"text/html; charset={label}") == text

    def test_gb2312_is_read_as_gbk_as_in_browsers(self) -> None:
        beyond_gb2312 = chr(0x9555)  # in GBK, not in GB 2312
        body = (KANJI + beyond_gb2312).encode("gbk")
        assert decode_body(body, "text/html; charset=gb2312") == KANJI + beyond_gb2312
        assert decode_body(b'<meta charset="GB2312">' + body, None).endswith(beyond_gb2312)

    @pytest.mark.parametrize("label", [*NOT_TEXT_ENCODINGS, *NOT_WEB_ENCODINGS])
    def test_any_other_label_is_skipped(self, label: str) -> None:
        page = f'<meta charset="{label}">{NAME}'
        undeclared = decode_body(page.encode("utf-8"), "text/html")
        assert undeclared == page
        assert decode_body(page.encode("utf-8"), f"text/html; charset={label}") == page
        assert decode_body(f"<?xml version='1.0' encoding='{label}'?>".encode(), None).startswith(
            "<?xml"
        )
        assert httptext._text_codec(label) is None

    def test_an_escape_codec_cannot_turn_ascii_into_other_characters(self) -> None:
        """unicode_escape would make lone surrogates, NUL and any character out of plain ASCII."""
        page = rb"<meta charset=unicode_escape>\ud83d \x00 \u202e \N{BELL}"
        text = decode_body(page, "text/html")
        assert text == page.decode("ascii")
        assert text.isascii()
        assert decode_body(page, "text/html; charset=unicode_escape") == text
        assert decode_body(page, "text/html; charset=raw_unicode_escape") == text

    def test_a_label_cannot_make_decoding_slow(self) -> None:
        """The punycode decoder is quadratic: it needed 40 s for a body of 640 kB."""
        body = b"<meta charset=punycode>-" + b"a" * 2_000_000
        started = time.perf_counter()
        assert decode_body(body, "text/html") == body.decode("ascii")
        assert decode_body(body, "text/html; charset=punycode") == body.decode("ascii")
        assert decode_body(body, "text/html; charset=idna") == body.decode("ascii")
        assert time.perf_counter() - started < 1.0

    def test_every_encoding_on_the_list_exists_and_decodes_anything(self) -> None:
        garbage = bytes(range(256)) * 4 + b"\x1b$B\x1b(B~{~}"
        for codec in sorted(httptext._WEB_CODECS):
            assert codecs.lookup(codec).name == codec
            assert isinstance(garbage.decode(codec, errors="replace"), str)
            assert httptext._text_codec(codec) == codec
        for label, codec in httptext._READ_AS.items():
            assert codecs.lookup(label).name == label
            assert codec in httptext._WEB_CODECS
        for _, codec in httptext._BOM_ENCODINGS:
            assert isinstance(garbage.decode(codec, errors="replace"), str)

    def test_a_refused_label_in_a_response_does_not_reach_the_codec(
        self, client: PoliteClient, respx_router: respx.MockRouter
    ) -> None:
        no_robots(respx_router)
        respx_router.get(PAGE).respond(
            200,
            content=rb"<p>\ud83d</p>",
            headers={"Content-Type": "text/html; charset=unicode_escape"},
        )
        assert client.get(PAGE).text == "<p>\\ud83d</p>\n"
