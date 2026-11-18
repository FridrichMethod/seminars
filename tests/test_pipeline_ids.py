"""Tests for the words of day keys: transliterated Latin names and names in other scripts."""

from __future__ import annotations

import itertools
import re
from datetime import datetime, timedelta
from typing import Any

import pytest

from factories import PACIFIC, make_draft, make_event
from seminars.models import Candidate, EventDraft, EventHints
from seminars.pipeline.ids import (
    draft_key,
    is_day_key,
    is_site_root,
    is_time_of_day_key,
    latin_fold,
    renamed_page,
    same_people,
    slug,
    tell_apart_day_keys,
    uid_for_key,
)

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

    def test_a_name_in_another_script_keeps_its_key_through_a_move_on_the_same_day(self) -> None:
        draft = make_draft(native_id=None, url=None)
        later = START.replace(hour=16, minute=30)
        assert key(draft, speaker=ZHANG_SAN) == key(draft, speaker=ZHANG_SAN, start=later)

    def test_a_title_in_another_script_names_the_talk_without_a_speaker(self) -> None:
        draft = make_draft(native_id=None, url=None)
        titled = key(draft, title=PROTEIN_DESIGN)
        assert titled != key(draft, title=QUANTUM_COMPUTING)
        assert titled.startswith("sites-bioengineering:day:2026-11-17:u")

    def test_without_speaker_and_title_the_time_of_day_remains(self) -> None:
        draft = make_draft(native_id=None, url=None)
        assert key(draft) == "sites-bioengineering:day:2026-11-17:t1600"
        assert is_time_of_day_key(key(draft, title="!!"))


DAY_KEY = "sites-x:day:2026-11-17:ann-smith"


def listing(hour: int, key: str = DAY_KEY, **overrides: Any) -> Candidate:
    values: dict[str, Any] = {
        "uid": uid_for_key(key),
        "title": f"Lecture at {hour}",
        "start": START.replace(hour=hour),
        "end": None,
        "source_ids": [key],
    }
    event = make_event(**{**values, **overrides})
    return Candidate(event=event, hints=EventHints(source_ids=["sites-x"]))


class TestOneDayKeyForTwoTalks:
    def test_the_last_talk_keeps_the_key_and_the_earlier_ones_get_their_time(self) -> None:
        talks = [listing(10), listing(14), listing(12)]
        result = tell_apart_day_keys(talks)
        assert [c.event.source_ids for c in result] == [
            [f"{DAY_KEY}:t1000"],
            [DAY_KEY],
            [f"{DAY_KEY}:t1200"],
        ]
        assert [c.event.uid for c in result] == [
            uid_for_key(f"{DAY_KEY}:t1000"),
            talks[1].event.uid,
            uid_for_key(f"{DAY_KEY}:t1200"),
        ]
        assert not any(is_time_of_day_key(key) for c in result for key in c.event.source_ids)
        assert all(is_day_key(key) for c in result for key in c.event.source_ids)

    def test_the_result_does_not_depend_on_the_order_and_is_stable(self) -> None:
        talks = [listing(10), listing(14), listing(12)]
        expected = {c.event.title: c.event.source_ids for c in tell_apart_day_keys(talks)}
        for order in itertools.permutations(talks):
            result = tell_apart_day_keys(list(order))
            assert {c.event.title: c.event.source_ids for c in result} == expected
            assert tell_apart_day_keys(result) == result

    def test_listings_with_one_start_stay_one_talk(self) -> None:
        talks = [listing(10), listing(10, abstract="Another listing of the same talk.")]
        assert tell_apart_day_keys(talks) == talks

    def test_only_day_keys_are_told_apart(self) -> None:
        native = [listing(10, "sites-x:1001"), listing(14, "sites-x:1001")]
        assert tell_apart_day_keys(native) == native
        slots = [listing(10, "sites-x:day:2026-11-17:t1000")]
        assert tell_apart_day_keys(slots) == slots

    def test_a_merged_listing_moves_only_the_shared_day_key(self) -> None:
        merged = listing(10, source_ids=[DAY_KEY, "localist-stanford:9"])
        later = listing(14)
        result = tell_apart_day_keys([merged, later])
        assert sorted(result[0].event.source_ids) == ["localist-stanford:9", f"{DAY_KEY}:t1000"]
        assert result[1] == later
        assert timedelta(0) < later.event.start - merged.event.start


class TestSamePeople:
    @pytest.mark.parametrize(
        ("first", "second", "expected"),
        [
            ({"emily b fox"}, {"emily fox"}, True),
            ({"vincent cohen adad"}, {"vincent cohen addad"}, True),
            ({"\u0142ukasz debowski"}, {"lukasz debowski"}, True),
            ({"ann smith", "bea jones"}, {"bea jones", "ann smith"}, True),
            ({"ann smith", "bea jones"}, {"ann smith"}, False),
            ({"jia li"}, {"jian li"}, False),
            ({"ann smith"}, {"bob jones"}, False),
            (set(), set(), False),
        ],
    )
    def test_spellings_of_one_person_are_one_person(
        self, first: set[str], second: set[str], expected: bool
    ) -> None:
        assert same_people(first, second) is expected
        assert same_people(second, first) is expected


class TestRenamedPages:
    @pytest.mark.parametrize(
        ("new", "expected"),
        [
            ("sites-x:url:https://example.edu/events/knots", True),
            ("sites-x:url:https://example.edu/talks/knots", False),
            ("sites-x:url:https://other.edu/events/knots", False),
            ("sites-x:url:https://example.edu/events/knots?id=2", False),
            ("sites-x:url:https://example.edu/events/coming-soon", False),
            ("sites-y:url:https://example.edu/events/knots", False),
        ],
    )
    def test_two_pages_of_one_directory(self, new: str, expected: bool) -> None:
        assert renamed_page("sites-x:url:https://example.edu/events/coming-soon", new) is expected


class TestSiteRoots:
    @pytest.mark.parametrize(
        ("url", "root"),
        [
            ("https://iclr.cc/", True),
            ("https://iclr.cc", True),
            ("https://iclr.cc//", True),
            ("https://iclr.cc/Conferences/2027", False),
            ("https://www.proteinsociety.org/annualsymposium/", False),
        ],
    )
    def test_the_front_page_of_a_site(self, url: str, root: bool) -> None:
        assert is_site_root(url) is root

    @pytest.mark.parametrize("url", ["https://iclr.cc/", "https://iclr.cc"])
    def test_a_front_page_is_never_a_key(self, url: str) -> None:
        # One edition of a conference must not hand its uid on to the next one.
        draft = make_draft(native_id=None, url=url, title="ICLR 2027")
        assert key(draft, title="ICLR 2027") == "sites-bioengineering:day:2026-11-17:iclr-2027"

    def test_a_deeper_page_is(self) -> None:
        draft = make_draft(native_id=None, url="https://iclr.cc/Conferences/2027/")
        assert key(draft) == "sites-bioengineering:url:https://iclr.cc/Conferences/2027"


class TestMeetingPages:
    MEETING_START = datetime(2027, 5, 17, 9, 0, tzinfo=PACIFIC)

    @pytest.mark.parametrize(
        "url",
        [
            "https://aimi.stanford.edu/events/aimi-symposium-summit-series-2027",
            "https://iclr.cc/Conferences/2027/",
            "https://example.org/events?year=2027",
            "https://example.org/2027/summer-school",
        ],
    )
    def test_the_page_of_one_edition_is_the_key_of_a_meeting(self, url: str) -> None:
        draft = make_draft(native_id=None, url=url)
        found = key(draft, start=self.MEETING_START, title="AIMI Symposium", meeting=True)
        assert found.startswith("sites-bioengineering:url:")

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.proteinsociety.org/annualsymposium",
            "https://example.org/symposium-2026",
            "https://example.org/events/20270517-symposium",
            "https://2027.example.org/symposium",
        ],
    )
    def test_the_page_of_a_series_is_no_key_of_a_meeting(self, url: str) -> None:
        # The series page serves one edition after the other; a page that names another
        # year, or no year of its own, is no page of this edition either.
        draft = make_draft(native_id=None, url=url)
        found = key(
            draft,
            start=self.MEETING_START,
            speaker="Jane Doe",
            title="Protein Society Symposium 2027",
            meeting=True,
        )
        assert found == "sites-bioengineering:day:2027-05-17:protein-society-symposium-2027"
        # A talk keeps its page, and its day key names its speaker first.
        talk = key(draft, start=self.MEETING_START, speaker="Jane Doe", title="Folding")
        assert talk == f"sites-bioengineering:url:{url}"
        assert key(make_draft(native_id=None, url=None), speaker="Jane Doe", title="Folding") == (
            "sites-bioengineering:day:2026-11-17:jane-doe"
        )

    def test_a_meeting_without_a_title_is_named_by_its_speaker(self) -> None:
        draft = make_draft(native_id=None, url=None)
        found = key(draft, speaker="Jane Doe", meeting=True)
        assert found == "sites-bioengineering:day:2026-11-17:jane-doe"

    def test_a_native_id_stays_the_key_of_a_meeting(self) -> None:
        draft = make_draft(native_id="777", url="https://www.proteinsociety.org/annualsymposium")
        assert key(draft, meeting=True) == "sites-bioengineering:777"
