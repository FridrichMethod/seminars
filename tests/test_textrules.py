"""Tests for the shared character tables, and that they match the charset gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from seminars import textrules
from seminars.textrules import (
    DECORATIVE_RANGES,
    EMOJI_RANGES,
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
INVISIBLE = ["\u061c", "\u180e", "\u2028", "\u2029", "\u2060", "\ufe0e"]


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
        for char in (ROCKET, CHECK_MARK_BUTTON, VARIATION_SELECTOR, NBSP, LIGATURE_FI):
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
