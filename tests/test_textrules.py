"""Tests for the shared character tables, the text cleaning and the run-time charset check."""

from __future__ import annotations

import importlib.util
import unicodedata
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from seminars import textrules
from seminars.textrules import (
    DECORATIVE_RANGES,
    EMOJI_RANGES,
    ascii_safe,
    clean_text,
    data_charset_problems,
    data_tree_problems,
    decorative_characters,
    is_decorative,
    is_emoji,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

ROCKET = "\U0001f680"
CHECK_MARK_BUTTON = "\u2705"
VARIATION_SELECTOR = "\ufe0f"
NBSP = "\u00a0"
LIGATURE_FI = "\ufb01"
ZERO_WIDTH_SPACE = "\u200b"
E_ACUTE = "\u00e9"
DOUBLE_STRUCK_R = "\u211d"
RIGHT_ARROW = "\u2192"
CHINESE = "\u4e2d"
SUN = "\u2609"
FEMALE, MALE = "\u2640", "\u2642"
JUPITER = "\u2643"
FLAT, NATURAL, SHARP = "\u266d", "\u266e", "\u266f"
SCIENCE_SIGNS = [SUN, "\u263f", FEMALE, "\u2641", MALE, JUPITER, "\u2647", FLAT, NATURAL, SHARP]
"""Characters of the symbol blocks that are notation, not decoration."""
_STILL_EMOJI = (0x2600, 0x2605, 0x2608, 0x260E, 0x263A, 0x2648, 0x2665, 0x266C, 0x2670)
SYMBOL_BLOCK_EMOJI = [chr(code) for code in (*_STILL_EMOJI, 0x26A0, 0x2705, 0x2713, 0x2764, 0x27BF)]
"""Stars, faces, zodiac, hearts, notes, warning signs, check marks: decoration."""
TYPOGRAPHIC_SPACES = ["\u2000", "\u2002", "\u2003", "\u2009", "\u200a", "\u205f"]
FORMAT_MARKS = ["\u061c", "\u180e", "\u2060", "\ufe0e"]
INVISIBLE = [*FORMAT_MARKS, "\u2028", "\u2029"]
FILLERS = ["\u115f", "\u1160", "\u3164", "\uffa0"]
"""Hangul fillers: letters by category, blank on screen."""
ANNOTATION_MARKS = ["\ufff9", "\ufffa", "\ufffb", "\ufffc"]
"""Interlinear annotation controls and the object replacement character."""
WORD_BULLET = "\uf0b7"  # the bullet of the Symbol font, pasted from Word
ICON = chr(0xF0045)  # a glyph of an icon font that uses plane 15
PRIVATE_USE = ["\ue000", WORD_BULLET, "\uf8ff"]
PRIVATE_USE += [chr(0xF0000), ICON, chr(0xFFFFD), chr(0x100000), chr(0x10ABCD), chr(0x10FFFD)]
"""Private use characters: they have no agreed meaning and show up as boxes."""
PRIVATE_USE_PLANES = [(0xF0000, 0xFFFFD), (0x100000, 0x10FFFD)]
"""The supplementary private use areas, planes 15 and 16 without their last two code points."""
UNPRINTABLE = [*FILLERS, *ANNOTATION_MARKS, *PRIVATE_USE]
REPLACEMENT_CHARACTER = "\ufffd"


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_ascii_for_textrules", REPO_ROOT / "scripts" / "check_ascii.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


class TestTablesMatchTheGate:
    def test_emoji_ranges_are_identical(self) -> None:
        assert gate.EMOJI_RANGES == EMOJI_RANGES

    def test_decorative_ranges_are_identical(self) -> None:
        assert gate.DECORATIVE_RANGES == DECORATIVE_RANGES

    @pytest.mark.parametrize("table", ["EMOJI_RANGES", "DECORATIVE_RANGES"])
    def test_predicates_agree_at_every_range_boundary(self, table: str) -> None:
        for low, high in getattr(textrules, table):
            assert low <= high
            for code_point in (low - 1, low, high, high + 1):
                char = chr(code_point)
                assert is_emoji(char) == gate.is_emoji(char), hex(code_point)
                assert is_decorative(char) == gate.is_decorative(char), hex(code_point)

    def test_what_the_gate_rejects_in_data_is_what_the_predicates_flag(self) -> None:
        for char in (
            ROCKET,
            CHECK_MARK_BUTTON,
            VARIATION_SELECTOR,
            NBSP,
            LIGATURE_FI,
            *UNPRINTABLE,
        ):
            line = '{"title": "x' + char + '"}\n'
            assert gate.check_bytes("data/events.json", line.encode("utf-8")) != []
            assert decorative_characters(line) == [char]
        for char in (E_ACUTE, DOUBLE_STRUCK_R, RIGHT_ARROW, CHINESE, *SCIENCE_SIGNS):
            line = '{"title": "x' + char + '"}\n'
            assert gate.check_bytes("data/events.json", line.encode("utf-8")) == []
            assert decorative_characters(line) == []


class TestPredicates:
    @pytest.mark.parametrize("char", [ROCKET, CHECK_MARK_BUTTON, VARIATION_SELECTOR, "\u2b50"])
    def test_emoji(self, char: str) -> None:
        assert is_emoji(char)
        assert not is_decorative(char)

    @pytest.mark.parametrize("char", [NBSP, LIGATURE_FI, ZERO_WIDTH_SPACE, "\u202f", "\ufeff"])
    def test_decorative(self, char: str) -> None:
        assert is_decorative(char)
        assert not is_emoji(char)

    @pytest.mark.parametrize(
        "char", ["a", " ", "\n", E_ACUTE, DOUBLE_STRUCK_R, RIGHT_ARROW, CHINESE, "\u00b2", "\u2264"]
    )
    def test_ordinary_text_is_neither(self, char: str) -> None:
        assert not is_emoji(char)
        assert not is_decorative(char)

    @pytest.mark.parametrize("char", SCIENCE_SIGNS)
    def test_scientific_signs_in_the_symbol_blocks_are_ordinary_text(self, char: str) -> None:
        """Solar and planetary masses, female and male, flat and sharp must survive cleaning."""
        assert not is_emoji(char)
        assert not is_decorative(char)

    @pytest.mark.parametrize("char", SYMBOL_BLOCK_EMOJI)
    def test_the_rest_of_the_symbol_blocks_is_still_emoji(self, char: str) -> None:
        assert is_emoji(char)

    def test_exactly_the_scientific_signs_are_exempt_in_the_symbol_blocks(self) -> None:
        exempt = [chr(code) for code in range(0x2600, 0x27C0) if not is_emoji(chr(code))]
        assert exempt == [SUN, *map(chr, range(0x263F, 0x2648)), FLAT, NATURAL, SHARP]

    @pytest.mark.parametrize("char", [*TYPOGRAPHIC_SPACES, *INVISIBLE])
    def test_invisible_characters_and_typographic_spaces_are_decorative(self, char: str) -> None:
        assert is_decorative(char)
        assert not is_emoji(char)

    @pytest.mark.parametrize("char", ["\u1fff", "\u2010", "\u2027", "\u2030", "\u205e", "\u3000"])
    def test_neighbours_of_the_decorative_ranges_are_ordinary_text(self, char: str) -> None:
        assert not is_decorative(char)

    @pytest.mark.parametrize("char", UNPRINTABLE)
    def test_fillers_annotation_marks_and_private_use_are_decorative(self, char: str) -> None:
        assert is_decorative(char)
        assert not is_emoji(char)

    def test_the_whole_private_use_area_is_decorative(self) -> None:
        assert all(is_decorative(chr(code)) for code in range(0xE000, 0xF900))
        assert all(unicodedata.category(chr(code)) == "Co" for code in range(0xE000, 0xF900))

    @pytest.mark.parametrize(("first", "last"), PRIVATE_USE_PLANES)
    def test_the_supplementary_private_use_planes_are_decorative(
        self, first: int, last: int
    ) -> None:
        assert (first, last) in DECORATIVE_RANGES
        assert all(is_decorative(chr(code)) for code in range(first, last + 1))
        assert all(unicodedata.category(chr(code)) == "Co" for code in range(first, last + 1))
        assert not any(is_emoji(chr(code)) for code in (first, last))

    @pytest.mark.parametrize("code", [0xEFFFF, 0xFFFFE, 0xFFFFF, 0x10FFFE, 0x10FFFF])
    def test_the_code_points_around_those_planes_are_not_in_the_table(self, code: int) -> None:
        """The last two code points of a plane are no characters at all, private or not."""
        assert unicodedata.category(chr(code)) == "Cn"
        assert not is_decorative(chr(code))

    def test_clean_text_removes_icon_font_characters_of_the_upper_planes(self) -> None:
        text = "Colloquium " + ICON + " Room 101 " + chr(0x100000) + chr(0x10FFFD) + "\n"
        assert clean_text(text) == "Colloquium  Room 101 \n"
        assert decorative_characters(text) == [ICON, chr(0x100000), chr(0x10FFFD)]
        assert data_charset_problems("ab" + ICON + "\n") == ["1:3 U+F0045"]
        assert data_charset_problems(clean_text(text)) == []

    @pytest.mark.parametrize(
        "char",
        [
            "\u115e",  # a Hangul initial consonant
            "\u1161",  # the Hangul vowel a
            "\u3163",  # a Hangul compatibility letter
            "\u3165",
            "\uf900",  # a CJK compatibility ideograph, right after the private use area
            "\uffa1",  # a halfwidth Hangul letter
            REPLACEMENT_CHARACTER,  # what decoding puts in place of an invalid byte
        ],
    )
    def test_neighbours_of_the_new_ranges_are_ordinary_text(self, char: str) -> None:
        assert not is_decorative(char)
        assert not is_emoji(char)

    def test_tables_do_not_overlap(self) -> None:
        for low, high in DECORATIVE_RANGES:
            assert not is_emoji(chr(low))
            assert not is_emoji(chr(high))


class TestDecorativeCharacters:
    def test_clean_text_has_none(self) -> None:
        text = "Poincar" + E_ACUTE + ": maps " + DOUBLE_STRUCK_R + " " + RIGHT_ARROW + " x\n"
        assert decorative_characters(text) == []
        assert decorative_characters("") == []

    def test_reports_each_character_once_in_order_of_appearance(self) -> None:
        text = "of" + LIGATURE_FI + "ce" + NBSP + "hours " + ROCKET + NBSP + LIGATURE_FI + ROCKET
        assert decorative_characters(text) == [LIGATURE_FI, NBSP, ROCKET]

    def test_emoji_sequences_are_reported_by_code_point(self) -> None:
        keycap = "1" + VARIATION_SELECTOR + "\u20e3"
        assert decorative_characters(keycap) == [VARIATION_SELECTOR, "\u20e3"]


ZWJ = "\u200d"
SOFT_HYPHEN = "\u00ad"
COMBINING_ACUTE = "\u0301"
WOMAN_RAISING_HAND = "\U0001f64b" + ZWJ + FEMALE + VARIATION_SELECTOR
MAN_SHRUGGING = "\U0001f937" + ZWJ + MALE + VARIATION_SELECTOR

LONE_SURROGATES = ["\ud800", "\ud83d", "\udbff", "\udc00", "\ude80", "\udfff"]
"""Halves of UTF-16 pairs: a string can hold one, a UTF-8 file cannot."""

every_character = st.characters(exclude_categories=[])  # the default leaves surrogates out
any_text = st.text(alphabet=st.one_of(every_character, st.sampled_from(LONE_SURROGATES)))
"""Arbitrary strings, lone surrogates included (``json.loads`` produces them)."""

_CLEANING_TARGETS = [NBSP, SOFT_HYPHEN, ZERO_WIDTH_SPACE, ZWJ, LIGATURE_FI, ROCKET, FEMALE, MALE]
_CLEANING_TARGETS += [VARIATION_SELECTOR, COMBINING_ACUTE, E_ACUTE, "e", " ", "\n", "\r", "\t"]
_CLEANING_TARGETS += [*UNPRINTABLE, "\u1112", "\u1161"]
hostile_text = st.text(
    alphabet=st.one_of(
        st.sampled_from([*_CLEANING_TARGETS, "\u2028", "\x85", *LONE_SURROGATES]),
        every_character,
    )
)
"""Text that is dense in the characters cleaning is about."""


class TestCleanText:
    def test_ordinary_text_is_unchanged(self) -> None:
        text = "Poincar" + E_ACUTE + " at Universit" + E_ACUTE + ": maps " + DOUBLE_STRUCK_R
        text += "\u00b2 " + RIGHT_ARROW + " " + DOUBLE_STRUCK_R + ", " + CHINESE + "\nsecond line"
        assert clean_text(text) == text
        assert clean_text("") == ""

    @pytest.mark.parametrize(
        "name", ["Erd\u0151s", "\u0141ukasiewicz", "Universit\u00e9", "\u674e"]
    )
    def test_names_are_not_transliterated(self, name: str) -> None:
        assert clean_text(name) == name

    @pytest.mark.parametrize(
        "notation", ["x\u00b2", "H\u2082O", "\u2102\u207f", "\u00bd", "\u2460"]
    )
    def test_notation_is_not_flattened_the_way_nfkc_would(self, notation: str) -> None:
        assert clean_text(notation) == notation
        assert unicodedata.normalize("NFKC", notation) != notation

    def test_text_is_composed_to_nfc(self) -> None:
        assert clean_text("Poincare" + COMBINING_ACUTE) == "Poincar" + E_ACUTE
        assert clean_text("\u212b") == "\u00c5"  # angstrom sign -> A with ring, a canonical pair

    @pytest.mark.parametrize(
        ("ligature", "letters"),
        [
            ("\ufb00", "ff"),
            ("\ufb01", "fi"),
            ("\ufb02", "fl"),
            ("\ufb03", "ffi"),
            ("\ufb04", "ffl"),
            ("\ufb05", "st"),
            ("\ufb06", "st"),
        ],
    )
    def test_ligatures_become_letters(self, ligature: str, letters: str) -> None:
        assert clean_text("x" + ligature + "y") == "x" + letters + "y"
        assert unicodedata.normalize("NFKC", ligature) == letters

    @pytest.mark.parametrize("space", [NBSP, "\u2007", "\u202f", "\t", *TYPOGRAPHIC_SPACES])
    def test_special_spaces_become_a_plain_space(self, space: str) -> None:
        assert clean_text("Jane" + space + "Doe") == "Jane Doe"

    @pytest.mark.parametrize(
        "line_break", ["\r\n", "\r", "\x0b", "\x0c", "\x85", "\u2028", "\u2029"]
    )
    def test_every_line_break_becomes_a_newline(self, line_break: str) -> None:
        assert clean_text("first" + line_break + "second") == "first\nsecond"

    @pytest.mark.parametrize(
        "char",
        [
            ZERO_WIDTH_SPACE,
            ZWJ,
            SOFT_HYPHEN,
            "\ufeff",
            "\x00",
            "\x07",
            "\x7f",
            "\x9f",
            *FORMAT_MARKS,
            *LONE_SURROGATES,
            *UNPRINTABLE,
        ],
    )
    def test_invisible_and_control_characters_are_removed(self, char: str) -> None:
        assert clean_text("semi" + char + "nar") == "seminar"

    def test_a_bullet_pasted_from_word_is_removed(self) -> None:
        assert clean_text(WORD_BULLET + " first point\n" + WORD_BULLET + " second") == (
            " first point\n second"
        )

    def test_hangul_text_survives_and_composes_after_a_filler_is_removed(self) -> None:
        syllable, initial, vowel = "\ud55c", "\u1112", "\u1161"
        assert clean_text(syllable) == syllable
        assert clean_text(initial + "\u1160") == initial
        assert clean_text(initial + "\u115f" + vowel) == "\ud558"  # the two letters compose

    def test_the_replacement_character_is_kept(self) -> None:
        assert clean_text("caf" + REPLACEMENT_CHARACTER) == "caf" + REPLACEMENT_CHARACTER

    def test_emoji_are_removed_with_their_modifiers(self) -> None:
        assert clean_text("Launch " + ROCKET + " day") == "Launch  day"
        assert clean_text("done " + CHECK_MARK_BUTTON + VARIATION_SELECTOR) == "done "
        assert clean_text("step 1" + VARIATION_SELECTOR + "\u20e3") == "step 1"
        assert clean_text("\U0001f1fa\U0001f1f8 talk") == " talk"  # regional indicators
        assert clean_text("\U0001f44d\U0001f3fd") == ""  # thumbs up with a skin tone

    def test_scientific_signs_survive_but_not_as_part_of_an_emoji(self) -> None:
        assert clean_text("a 10 M" + SUN + " star") == "a 10 M" + SUN + " star"
        assert clean_text(FEMALE + " and " + MALE + " mice") == FEMALE + " and " + MALE + " mice"
        assert (
            clean_text("the " + FLAT + " and " + SHARP + " maps")
            == "the " + FLAT + " and " + SHARP + " maps"
        )
        assert clean_text("hi " + WOMAN_RAISING_HAND + MAN_SHRUGGING + "!") == "hi !"
        assert clean_text(FEMALE + VARIATION_SELECTOR) == FEMALE

    def test_a_removal_can_complete_a_letter(self) -> None:
        assert clean_text("e" + ZERO_WIDTH_SPACE + COMBINING_ACUTE) == E_ACUTE

    def test_whitespace_is_left_for_the_caller(self) -> None:
        assert clean_text("  a" + NBSP + NBSP + "b \n\n") == "  a  b \n\n"

    def test_every_code_point_is_handled(self) -> None:
        everything = "".join(chr(code) for code in range(0x110000))
        cleaned = clean_text(everything)
        assert decorative_characters(cleaned) == []
        assert data_charset_problems(cleaned + "\n") == []
        assert clean_text(cleaned) == cleaned
        assert cleaned.encode("utf-8").decode("utf-8") == cleaned

    @given(hostile_text)
    def test_result_passes_the_data_rule(self, text: str) -> None:
        cleaned = clean_text(text)
        assert decorative_characters(cleaned) == []
        assert data_charset_problems(cleaned + "\n") == []
        assert gate.check_bytes("data/events.json", (cleaned + "\n").encode("utf-8")) == []

    @given(any_text)
    def test_result_can_always_be_encoded(self, text: str) -> None:
        """Every writer encodes to UTF-8, so a lone surrogate must not survive cleaning."""
        cleaned = clean_text(text)
        assert cleaned.encode("utf-8").decode("utf-8") == cleaned
        assert not any(0xD800 <= ord(char) <= 0xDFFF for char in cleaned)

    @given(hostile_text)
    def test_is_idempotent(self, text: str) -> None:
        cleaned = clean_text(text)
        assert clean_text(cleaned) == cleaned
        assert unicodedata.is_normalized("NFC", cleaned)

    @given(any_text)
    @example("\u2011")
    def test_never_adds_characters_beyond_letters_spaces_newlines_and_a_hyphen(
        self, text: str
    ) -> None:
        # The letters of the ligatures, the space and the newline, and "-" for the
        # non-breaking hyphen U+2011.
        kept = set(unicodedata.normalize("NFC", text)) | set("filst \n-")
        composed = {char for char in clean_text(text) if char not in kept}
        assert all(unicodedata.decomposition(char) for char in composed)


class TestAsciiSafe:
    def test_non_ascii_becomes_backslash_escapes(self) -> None:
        assert ascii_safe("caf" + E_ACUTE) == "caf\\xe9"
        assert ascii_safe("M" + SUN) == "M\\u2609"
        assert ascii_safe(ROCKET) == "\\U0001f680"

    @given(any_text)
    def test_result_is_ascii_and_stable(self, text: str) -> None:
        escaped = ascii_safe(text)
        assert escaped.isascii()
        assert ascii_safe(escaped) == escaped
        assert ascii_safe(text) == escaped

    def test_ascii_is_unchanged(self) -> None:
        assert ascii_safe("plain: text\n") == "plain: text\n"


class TestDataCharsetProblems:
    def test_clean_text_has_no_problems(self) -> None:
        assert data_charset_problems("") == []
        assert data_charset_problems('{"speaker": "Poincar' + E_ACUTE + '"}\n') == []

    def test_reports_line_column_and_code_point(self) -> None:
        text = '{"a": 1,\n "title": "x' + NBSP + "y" + ROCKET + '"}\n'
        assert data_charset_problems(text) == ["2:13 U+00A0", "2:15 U+1F680"]

    def test_control_characters_and_line_endings(self) -> None:
        assert data_charset_problems("a\tb\n") == ["1:2 U+0009"]
        assert data_charset_problems("a\r\nb\r\n") == ["1:2 U+000D", "2:2 U+000D"]
        assert data_charset_problems("\ufeff{}\n") == ["1:1 U+FEFF"]
        assert data_charset_problems("{}") == ["missing final newline"]

    def test_problems_never_quote_the_text(self) -> None:
        problems = data_charset_problems("secret" + NBSP + "title")
        assert problems == ["1:7 U+00A0", "missing final newline"]
        assert all(problem.isascii() and "secret" not in problem for problem in problems)

    @given(any_text.filter(lambda text: "\x00" not in text))
    def test_agrees_with_the_gate_on_what_is_acceptable(self, text: str) -> None:
        """NUL is left out: the gate skips files with one as binary, this check rejects them.

        A lone surrogate has no UTF-8 form. The bytes a careless writer could
        produce for one ("surrogatepass") are not valid UTF-8 to the gate.
        """
        on_disk = text.encode("utf-8", "surrogatepass")
        accepted_by_gate = gate.check_bytes("data/events.json", on_disk) == []
        assert (data_charset_problems(text) == []) == accepted_by_gate

    def test_lone_surrogates_are_reported(self) -> None:
        assert data_charset_problems("a\ud83db\n") == ["1:2 U+D83D"]
        assert data_charset_problems("\n\udfff\n") == ["2:1 U+DFFF"]

    def test_rejects_a_nul_byte_like_the_gate(self) -> None:
        assert gate.check_bytes("data/events.json", b"a\x00b\n") == [
            "data/events.json: NUL byte at byte offset 1: not UTF-8 text"
        ]
        assert data_charset_problems("a\x00b\n") == ["1:2 U+0000"]


class TestDataTreeProblems:
    def test_clean_tree_and_missing_directory(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        assert data_tree_problems(data) == []
        (data / "archive").mkdir(parents=True)
        (data / "events.json").write_text('{"n": "' + E_ACUTE + '"}\n', encoding="utf-8")
        (data / "archive" / "2026.jsonl").write_text("{}\n", encoding="utf-8")
        assert data_tree_problems(data) == []

    def test_problems_carry_the_path_like_the_gate(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        (data / "llm_cache" / "ab").mkdir(parents=True)
        (data / "events.json").write_text('{"t": "a' + NBSP + 'b"}\n', encoding="utf-8")
        (data / "llm_cache" / "ab" / "cdef.json").write_text("{}", encoding="utf-8")
        (data / "run_report.json").write_bytes(b"caf\xe9\n")
        assert data_tree_problems(data) == [
            "data/events.json:1:9 U+00A0",
            "data/llm_cache/ab/cdef.json:missing final newline",
            "data/run_report.json: not valid UTF-8",
        ]
        line = '{"t": "a' + NBSP + 'b"}\n'
        assert gate.check_bytes("data/events.json", line.encode("utf-8")) == [
            "data/events.json:1:9 U+00A0 NO-BREAK SPACE"
        ]
