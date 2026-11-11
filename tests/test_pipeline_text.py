"""Tests for the text helpers of normalization and for speaker lines."""

from __future__ import annotations

import pytest

from seminars.pipeline.speakers import ParsedSpeaker, looks_like_name, split_speakers
from seminars.pipeline.text import (
    canonical_name,
    fold,
    html_to_text,
    is_tba,
    one_line,
    paragraphs,
    scrub_private_links,
    split_status,
    strip_markup,
    tidy_name,
    truncate,
)
from seminars.textrules import decorative_characters

NBSP = "\u00a0"
ROCKET = "\U0001f680"
LIGATURE_FI = "\ufb01"


class TestMarkup:
    def test_html_becomes_one_paragraph_per_line(self) -> None:
        markup = (
            "<div><p>Hello <b>world</b>, a&lt;b &amp; c</p>\n<p>Second&nbsp;para<br>line</p>"
            "<script>alert(1)</script><style>p{}</style><!-- note --><ul><li>one</li>"
            "<li>two</li></ul></div>"
        )
        assert paragraphs(markup, limit=500, is_html=True) == (
            "Hello world, a<b & c\nSecond para\nline\none\ntwo"
        )

    def test_inline_tags_do_not_break_a_sentence(self) -> None:
        assert html_to_text("A <em>very</em> <a href='x'>good</a> talk").split() == [
            "A",
            "very",
            "good",
            "talk",
        ]

    def test_empty_html(self) -> None:
        assert html_to_text("") == ""
        assert paragraphs("<p> </p>", limit=10, is_html=True) is None

    def test_stray_tags_and_entities_in_plain_text(self) -> None:
        assert one_line("  A &amp; B <em>talk</em>\n on <br/> knots ", limit=100) == (
            "A & B talk on knots"
        )
        assert one_line("CO<sub>2</sub> reduction", limit=100) == "CO2 reduction"

    def test_mathematics_is_not_mistaken_for_markup(self) -> None:
        text = "Bounds for a < b and pairs <x, y> with x > 0"
        assert strip_markup(text) == text
        assert one_line(text, limit=100) == text

    def test_script_blocks_and_comments_are_dropped_from_plain_text(self) -> None:
        text = "Title<script>steal()</script> here<!-- hidden -->"
        assert one_line(text, limit=100) == "Title here"

    def test_plain_paragraphs_keep_their_lines(self) -> None:
        text = "First  line \r\n\r\n  second\tline\n\n\nthird"
        assert paragraphs(text, limit=100) == "First line\nsecond line\nthird"

    def test_nothing_gives_none(self) -> None:
        assert one_line(None, limit=10) is None
        assert one_line(" \n\t ", limit=10) is None
        assert paragraphs(None, limit=10) is None


class TestUnicode:
    def test_text_is_cleaned_for_data(self) -> None:
        raw = f"Poincar\u00e9{NBSP}duality {ROCKET} and ef{LIGATURE_FI}ciency\u200b"
        cleaned = one_line(raw, limit=100)
        assert cleaned == "Poincar\u00e9 duality and efficiency"
        assert decorative_characters(cleaned or "") == []

    def test_names_keep_their_accents_and_symbols_stay(self) -> None:
        text = "Universit\u00e9 de Gen\u00e8ve: \u211d\u00b2"
        assert one_line(text, limit=50) == text


class TestTruncate:
    def test_short_text_is_kept(self) -> None:
        assert truncate("abc", 3) == "abc"

    def test_a_cut_ends_in_three_dots_and_respects_the_limit(self) -> None:
        cut = truncate("word " * 100, 40)
        assert len(cut) <= 40
        assert cut.endswith("...")
        assert not cut.endswith(" ...")

    def test_tiny_limits(self) -> None:
        assert truncate("abcdef", 2) == "ab"

    def test_fields_are_bounded(self) -> None:
        assert len(one_line("x" * 1000, limit=50) or "") == 50
        assert len(paragraphs("y" * 9000, limit=4000) or "") == 4000


class TestPrivateLinks:
    def test_links_with_a_password_and_passcodes_are_removed(self) -> None:
        text = (
            "Join https://stanford.zoom.us/j/123?pwd=abc. Passcode: 123456 or see "
            "https://example.edu/a?b=1, password is: hunter2"
        )
        assert scrub_private_links(text) == (
            "Join [link removed]. Passcode: [removed] or see "
            "https://example.edu/a?b=1, password is: [removed]"
        )

    def test_ordinary_text_is_untouched(self) -> None:
        text = "A talk about password hashing at https://example.edu/talk"
        assert scrub_private_links(text) == text


class TestTitles:
    @pytest.mark.parametrize(
        "title",
        [
            *["TBA", "tbd", "Title: TBA", "T.B.A.", "Title to come", "", None, "Talk title TBD"],
            *["To be announced!", "(TBA)", "Title", "Untitled", "Seminar title is forthcoming"],
        ],
    )
    def test_ways_of_saying_there_is_no_title(self, title: str | None) -> None:
        assert is_tba(title)

    @pytest.mark.parametrize("title", ["TBA - Jane Doe", "Protein design", "Title IX and science"])
    def test_real_titles(self, title: str) -> None:
        assert not is_tba(title)

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("CANCELLED: Knots", ("Knots", "cancelled")),
            ("Canceled - Knots", ("Knots", "cancelled")),
            ("Knots - cancelled", ("Knots", "cancelled")),
            ("[Postponed] Knots", ("Knots", "cancelled")),
            ("Knots (Canceled)", ("Knots", "cancelled")),
            ("POSTPONED \u2013 Knots", ("Knots", "cancelled")),
            ("Knots: tentative", ("Knots", "tentative")),
            ("Cancelled", ("TBA", "cancelled")),
            ("This event has been cancelled", ("TBA", "cancelled")),
            ("Cancelled flights and how to avoid them", (None, None)),
            ("Self-cancelled noise", (None, None)),
            ("Noise that is self-cancelled", (None, None)),
            ("Knots", (None, None)),
        ],
    )
    def test_status_words(self, title: str, expected: tuple[str | None, str | None]) -> None:
        text, status = expected
        assert split_status(title) == (text or title, status)


class TestNames:
    @pytest.mark.parametrize(
        ("raw", "name"),
        [
            ("Prof. Dr. Jane Doe", "Jane Doe"),
            ("Jane Doe, PhD", "Jane Doe"),
            ("Jane Doe Ph.D.", "Jane Doe"),
            ("Dr Jane Doe, M.D., PhD", "Jane Doe"),
            ("Jos\u00e9 Garc\u00eda", "Jos\u00e9 Garc\u00eda"),
            ("Professor", ""),
        ],
    )
    def test_tidy_name(self, raw: str, name: str) -> None:
        assert tidy_name(raw) == name

    def test_spellings_of_one_person_compare_equal(self) -> None:
        assert canonical_name("Prof. Jos\u00e9 GARC\u00cdA-L\u00f3pez, PhD") == "jose garcia lopez"
        assert canonical_name("Jose Garcia Lopez") == "jose garcia lopez"
        assert fold("  A_b--C  ") == "a b c"


class TestSpeakerLines:
    @pytest.mark.parametrize(
        ("line", "people"),
        [
            ("Speaker: Jane Doe (MIT)", [("Jane Doe", "MIT")]),
            ("Jane Doe, Stanford University", [("Jane Doe", "Stanford University")]),
            (
                "Prof. Jane Doe, PhD, Department of Chemistry, MIT",
                [("Jane Doe", "Department of Chemistry, MIT")],
            ),
            (
                "Jane Doe (MIT) and John Roe (Stanford University)",
                [("Jane Doe", "MIT"), ("John Roe", "Stanford University")],
            ),
            (
                "Jane Doe, John Roe, and Mary Major",
                [("Jane Doe", None), ("John Roe", None), ("Mary Major", None)],
            ),
            ("Jane Doe, John Roe", [("Jane Doe", None), ("John Roe", None)]),
            (
                "Jane Doe, Department of Chemistry and Chemical Biology, Harvard University",
                [("Jane Doe", "Department of Chemistry and Chemical Biology, Harvard University")],
            ),
            (
                "Jane Doe, Stanford University and John Roe, MIT",
                [("Jane Doe", "Stanford University"), ("John Roe", "MIT")],
            ),
            ("Jane Doe; John Roe (UCSF)", [("Jane Doe", None), ("John Roe", "UCSF")]),
            ("Jane Doe\nJohn Roe", [("Jane Doe", None), ("John Roe", None)]),
            ("Jane Doe, Google DeepMind", [("Jane Doe", "Google DeepMind")]),
            (
                "Presented by: J. K. Rowling & Stephen King",
                [("J. K. Rowling", None), ("Stephen King", None)],
            ),
            (
                "Jean-Pierre de la Fontaine (CNRS, Paris)",
                [("Jean-Pierre de la Fontaine", "CNRS, Paris")],
            ),
            ("Dr. Jos\u00e9 Garc\u00eda-L\u00f3pez", [("Jos\u00e9 Garc\u00eda-L\u00f3pez", None)]),
            ("", []),
            ("Speaker:", []),
        ],
    )
    def test_split(self, line: str, people: list[tuple[str, str | None]]) -> None:
        assert split_speakers(line) == [ParsedSpeaker(name, place) for name, place in people]

    @pytest.mark.parametrize(
        "text", ["Jane Doe", "J. K. Rowling", "Ludwig van Beethoven", "\u5f20\u4f1f", "Mary O'Neil"]
    )
    def test_names(self, text: str) -> None:
        assert looks_like_name(text)

    @pytest.mark.parametrize(
        "text",
        [
            *["Stanford University", "MIT", "Room 104", "jane@example.edu", "google deepmind"],
            *["", "Department of Chemistry", "x" * 90, "A B C D E F G"],
        ],
    )
    def test_not_names(self, text: str) -> None:
        assert not looks_like_name(text)
