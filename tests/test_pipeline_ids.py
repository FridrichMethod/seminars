"""Tests for the words of day keys: transliterated Latin names and names in other scripts."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pytest

from factories import PACIFIC, make_draft
from seminars.models import EventDraft
from seminars.pipeline.ids import draft_key, is_time_of_day_key, latin_fold, slug, uid_for_key

START = datetime(2026, 11, 17, 16, 0, tzinfo=PACIFIC)
L_STROKE = "\u0141"  # capital L with stroke, which has no decomposition
ZHANG_SAN = "\u5f20\u4e09"
LI_SI = "\u674e\u56db"
PROTEIN_DESIGN = "\u86cb\u767d\u8d28\u8bbe\u8ba1"
QUANTUM_COMPUTING = "\u91cf\u5b50\u8ba1\u7b97"


def no_secret(text: str) -> bool:
    return False


def key(draft: EventDraft, **overrides: Any) -> str:
    values: dict[str, Any] = {
        "start": START,
        "speaker": None,
        "title": None,
        "shared": frozenset(),
        "is_secret": no_secret,
    }
    return draft_key(draft, **{**values, **overrides})


class TestLatinLetters:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (f"{L_STROKE}ukasz D\u0119bowski", "lukasz-debowski"),
            ("Lukasz Debowski", "lukasz-debowski"),
            ("S\u00f8ren \u00c6rb\u00f8", "soren-aerbo"),
            ("Stra\u00dfe", "strasse"),
            ("\u0110or\u0111e \u0131\u015f\u0131k", "dorde-isik"),
            ("\u00de\u00f3r \u00d0\u00f3ra", "thor-dora"),
            ("\u0152uvre", "oeuvre"),
        ],
    )
    def test_letters_without_a_decomposition_are_spelled_in_ascii(
        self, text: str, expected: str
    ) -> None:
        assert slug(text) == expected

    def test_the_fold_used_to_compare_names_spells_them_the_same(self) -> None:
        assert latin_fold(f"{L_STROKE}ukasz D\u0119bowski") == latin_fold("LUKASZ DEBOWSKI")
        assert latin_fold("Jos\u00e9 Garc\u00eda-L\u00f3pez") == "jose garcia lopez"

    def test_a_transliterated_speaker_keeps_the_key(self) -> None:
        draft = make_draft(native_id=None, url=None)
        stroked = key(draft, speaker=f"{L_STROKE}ukasz D\u0119bowski")
        assert stroked == key(draft, speaker="Lukasz Debowski")
        assert stroked == "sites-bioengineering:day:2026-11-17:lukasz-debowski"


class TestOtherScripts:
    def test_a_name_without_latin_letters_gets_words_of_its_own(self) -> None:
        words = slug(ZHANG_SAN)
        assert re.fullmatch(r"u[0-9a-f]{10}", words)
        assert slug(ZHANG_SAN) == words
        assert slug(LI_SI) != words
        assert slug(f" {ZHANG_SAN}! ") == words

    def test_text_without_any_letter_or_digit_has_no_words(self) -> None:
        assert slug("  ") == ""
        assert slug("!?-- ...") == ""

    def test_two_talks_in_another_script_at_one_start_get_two_uids(self) -> None:
        drafts = [
            make_draft(native_id=None, url=None, title=PROTEIN_DESIGN),
            make_draft(native_id=None, url=None, title=QUANTUM_COMPUTING),
        ]
        first = key(drafts[0], speaker=ZHANG_SAN, title=PROTEIN_DESIGN)
        second = key(drafts[1], speaker=LI_SI, title=QUANTUM_COMPUTING)
        assert first != second
        assert uid_for_key(first) != uid_for_key(second)
        assert not is_time_of_day_key(first)
        assert not is_time_of_day_key(second)

    def test_a_title_in_another_script_names_the_talk_without_a_speaker(self) -> None:
        draft = make_draft(native_id=None, url=None)
        titled = key(draft, title=PROTEIN_DESIGN)
        assert titled != key(draft, title=QUANTUM_COMPUTING)
        assert titled.startswith("sites-bioengineering:day:2026-11-17:u")

    def test_without_speaker_and_title_the_time_of_day_remains(self) -> None:
        draft = make_draft(native_id=None, url=None)
        assert key(draft) == "sites-bioengineering:day:2026-11-17:t1600"
        assert is_time_of_day_key(key(draft, title="!!"))
