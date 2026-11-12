"""Tests for text-default emoji and invisible fillers in the shared character tables.

Each character here is checked against both copies of the tables: the one
of ``seminars.textrules`` that cleans text at run time and the one of the
charset gate, ``scripts/check_ascii.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from seminars.textrules import clean_text, decorative_characters, is_decorative, is_emoji

REPO_ROOT = Path(__file__).resolve().parents[1]
VS16 = "\ufe0f"

TEXT_DEFAULT_EMOJI = ["\u203c", "\u2049", "\u2139", "\u24c2"]
"""Double and question exclamation marks, information source, circled M."""
TEXT_SYMBOLS = ["\u2194", "\u00a9", "\u00ae", "\u2122"]
"""Left right arrow, copyright, registered and trade mark signs: ordinary text."""
INVISIBLE_RANGES = [
    (0x2800, 0x2800),  # Braille pattern blank
    (0x034F, 0x034F),  # combining grapheme joiner
    (0x17B4, 0x17B5),  # Khmer inherent vowels
    (0x180B, 0x180F),  # Mongolian free variation selectors and vowel separator
    (0xFE00, 0xFE0D),  # variation selectors 1 to 14
    (0xE0001, 0xE0001),  # language tag
    (0xE0100, 0xE01EF),  # variation selectors supplement
]
INVISIBLE = [chr(code) for low, high in INVISIBLE_RANGES for code in range(low, high + 1)]
NEIGHBOURS = [
    "\u2801",  # Braille pattern dots-1
    "\u034e",
    "\u0350",  # combining marks around the grapheme joiner
    "\u17b3",
    "\u17b6",  # Khmer letters and vowel signs
    "\u180a",  # Mongolian nirugu
    "\u1810",  # Mongolian digit zero
    chr(0xE0002),
    chr(0xE00FF),
    chr(0xE01F0),
]
NON_BREAKING_HYPHEN = "\u2011"


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_ascii_for_invisible", REPO_ROOT / "scripts" / "check_ascii.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _rejected_in_data(char: str) -> bool:
    line = '{"title": "x' + char + '"}\n'
    return gate.check_bytes("data/events.json", line.encode("utf-8")) != []


class TestTextDefaultEmoji:
    @pytest.mark.parametrize("char", TEXT_DEFAULT_EMOJI)
    def test_they_are_emoji_in_both_tables(self, char: str) -> None:
        assert is_emoji(char)
        assert gate.is_emoji(char)
        assert not is_decorative(char)
        assert _rejected_in_data(char)

    @pytest.mark.parametrize("char", TEXT_DEFAULT_EMOJI)
    def test_clean_text_removes_them_with_or_without_the_emoji_selector(self, char: str) -> None:
        assert clean_text(f"A{char} B{char}{VS16} C") == "A B C"

    @pytest.mark.parametrize("char", TEXT_SYMBOLS)
    def test_arrows_and_legal_signs_stay_text(self, char: str) -> None:
        assert not is_emoji(char)
        assert not is_decorative(char)
        assert not _rejected_in_data(char)
        assert clean_text(f"A{char}{VS16} B{char}") == f"A{char} B{char}"


class TestInvisibleFillers:
    @pytest.mark.parametrize("char", INVISIBLE, ids=lambda char: f"U+{ord(char):04X}")
    def test_they_are_decorative_in_both_tables(self, char: str) -> None:
        assert is_decorative(char)
        assert gate.is_decorative(char)
        assert not is_emoji(char)

    @pytest.mark.parametrize(("low", "high"), INVISIBLE_RANGES)
    def test_the_gate_rejects_them_in_data(self, low: int, high: int) -> None:
        assert _rejected_in_data(chr(low))
        assert _rejected_in_data(chr(high))

    def test_clean_text_removes_them(self) -> None:
        text = "".join(
            f"{chr(ord('A') + index % 26)}{char}" for index, char in enumerate(INVISIBLE)
        )
        cleaned = clean_text(text)
        assert all(char not in cleaned for char in INVISIBLE)
        assert decorative_characters(cleaned) == []

    @pytest.mark.parametrize("char", NEIGHBOURS, ids=lambda char: f"U+{ord(char):04X}")
    def test_their_neighbours_are_not_in_the_tables(self, char: str) -> None:
        assert not is_decorative(char)
        assert not is_emoji(char)

    def test_the_review_sample(self) -> None:
        text = (
            "A\u203c B\u2049 C\u2139 D\u24c2 E\u2194 F\u2b05 G\u25b6 H\u2122 I\u00a9"
            " L\u2800M N\u034fO P\u17b4Q R\ufe00S T" + chr(0xE0001) + "U V" + chr(0xE0100) + "W"
            " X\u180bY"
        )
        assert clean_text(text) == "A B C D E\u2194 F G H\u2122 I\u00a9 LM NO PQ RS TU VW XY"


class TestNonBreakingHyphen:
    def test_it_becomes_a_hyphen(self) -> None:
        assert clean_text(f"Jean{NON_BREAKING_HYPHEN}Pierre") == "Jean-Pierre"

    def test_other_dashes_are_data(self) -> None:
        text = "1990\u20132000 \u2014 Jean\u2010Pierre"
        assert clean_text(text) == text
