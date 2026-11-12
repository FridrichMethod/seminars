"""Tests for the hand-over of stored uids when a key changes while the talk stays.

A talk that a source knows by its day or by the address of its page gets a
new key when it moves to another day, when its speaker's name is spelled
anew, when a generic title gives way to the real one or when its TBA page
is renamed. The candidate under the new key takes the uid of the stored
event (``seminars.pipeline.dedup.adopt_stored_uids``).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from typing import Any

import pytest

from factories import PACIFIC, REFERENCE_NOW, make_event
from seminars.config import DedupSettings
from seminars.models import Candidate, Event, EventHints
from seminars.pipeline.dedup import adopt_stored_uids
from seminars.pipeline.ids import MAX_MOVE

SETTINGS = DedupSettings()
START = datetime(2026, 11, 17, 16, 0, tzinfo=PACIFIC)
WEEK = timedelta(days=7)
LISTING = "https://example.edu/events"
L_STROKE = "\u0141"


def day_key(start: datetime, words: str, source: str = "sites-x") -> str:
    return f"{source}:day:{start.date().isoformat()}:{words}"


def stored(uid: str, *, start: datetime = START, **overrides: Any) -> Event:
    """A stored talk that its source knows by its day (no native id, no page of its own)."""
    values: dict[str, Any] = {
        "uid": uid,
        "title": "Quantum codes",
        "speakers": [{"name": "Ann Smith"}],
        "start": start,
        "end": None,
        "url": LISTING,
        "source_urls": [LISTING],
        "source_ids": [day_key(start, uid)],
    }
    return make_event(**{**values, **overrides})


def fresh(uid: str, **overrides: Any) -> Candidate:
    """A candidate of this run; normalization stamps the time of the run on it."""
    event = stored(uid, **overrides).evolve(first_seen=REFERENCE_NOW, last_changed=REFERENCE_NOW)
    return Candidate(event=event, hints=EventHints(source_ids=["sites-x"]))


def people(*names: str) -> list[dict[str, str]]:
    return [{"name": name} for name in names]


def adopt(candidates: list[Candidate], events: list[Event]) -> list[str]:
    result = adopt_stored_uids(candidates, events, settings=SETTINGS, listing_urls={LISTING})
    return [candidate.event.uid for candidate in result]


class TestSpellings:
    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("Emily B. Fox", "Emily Fox"),
            ("Vincent Cohen-Adad", "Vincent Cohen-Addad"),
            (f"{L_STROKE}ukasz D\u0119bowski", "Lukasz Debowski"),
            ("Michael C. Frank", "Michael Frank"),
        ],
    )
    def test_another_spelling_of_the_speaker_is_the_same_talk(self, old: str, new: str) -> None:
        orphan = stored("old", title="TBA", speakers=people(old))
        candidate = fresh("new", title="TBA", speakers=people(new))
        assert adopt([candidate], [orphan]) == ["old"]

    @pytest.mark.parametrize(
        ("old", "new"), [("Jia Li", "Jian Li"), ("Ann Smith", "Bob Jones"), ("Ann Smith", "Ann")]
    )
    def test_another_name_is_another_talk(self, old: str, new: str) -> None:
        orphan = stored("old", title="TBA", speakers=people(old))
        candidate = fresh("new", title="TBA", speakers=people(new))
        assert adopt([candidate], [orphan]) == ["new"]


class TestMovedToAnotherDay:
    def test_the_same_talk_a_week_later_keeps_the_uid(self) -> None:
        assert adopt([fresh("new", start=START + WEEK)], [stored("old")]) == ["old"]
        earlier = fresh("new", start=START - timedelta(days=2))
        assert adopt([earlier], [stored("old")]) == ["old"]

    def test_a_tba_title_on_either_side_does_not_stand_in_the_way(self) -> None:
        tba = stored("old", title="TBA")
        assert adopt([fresh("new", start=START + WEEK)], [tba]) == ["old"]
        assert adopt([fresh("new", title="TBA", start=START + WEEK)], [stored("old")]) == ["old"]

    @pytest.mark.parametrize(
        "title",
        [
            "RESCHEDULED: Quantum codes",
            "Quantum codes (new date)",
            "Quantum codes [moved to Nov 24]",
        ],
    )
    def test_a_title_that_gains_words_about_the_move_is_the_same_talk(self, title: str) -> None:
        moved = fresh("new", title=title, start=START + WEEK)
        assert adopt([moved], [stored("old")]) == ["old"]

    def test_another_title_of_the_same_speaker_is_another_talk(self) -> None:
        other = fresh("new", title="Dark matter searches", start=START + WEEK)
        assert adopt([other], [stored("old")]) == ["new"]

    def test_another_speaker_is_another_talk(self) -> None:
        other = fresh("new", speakers=people("Bob Jones"), start=START + WEEK)
        assert adopt([other], [stored("old")]) == ["new"]

    def test_a_talk_that_is_over_did_not_move(self) -> None:
        past = REFERENCE_NOW - timedelta(days=1)
        assert adopt([fresh("new", start=START)], [stored("old", start=past)]) == ["new"]
        assert adopt([fresh("new", start=past)], [stored("old")]) == ["new"]

    def test_a_move_beyond_the_limit_is_another_talk(self) -> None:
        far = fresh("new", start=START + MAX_MOVE + timedelta(minutes=1))
        assert adopt([far], [stored("old")]) == ["new"]
        assert adopt([fresh("new", start=START + MAX_MOVE)], [stored("old")]) == ["old"]

    def test_a_guess_between_two_talks_is_not_made(self) -> None:
        twice = [fresh("new", start=START + WEEK), fresh("next", start=START + 2 * WEEK)]
        assert adopt(twice, [stored("old")]) == ["new", "next"]
        orphans = [stored("old"), stored("other", start=START + 2 * WEEK)]
        assert adopt([fresh("new", start=START + WEEK)], orphans) == ["new"]

    def test_two_speakers_who_swap_their_days_keep_their_uids(self) -> None:
        ann, bob = people("Ann Smith"), people("Bob Jones")
        orphans = [
            stored("ann", title="TBA", speakers=ann),
            stored("bob", title="TBA", speakers=bob, start=START + WEEK),
        ]
        swapped = [
            fresh("bob-early", title="TBA", speakers=bob),
            fresh("ann-late", title="TBA", speakers=ann, start=START + WEEK),
        ]
        for order in itertools.permutations(swapped):
            result = adopt(list(order), orphans)
            taken = dict(zip((c.event.uid for c in order), result, strict=True))
            assert taken == {"bob-early": "bob", "ann-late": "ann"}

    @pytest.mark.parametrize(
        "keys",
        [
            ["sites-x:1001"],
            ["sites-x:url:https://example.edu/talks/1001"],
            ["sites-y:day:2026-11-17:old"],
        ],
    )
    def test_only_a_day_key_of_the_same_source_moves_with_the_talk(self, keys: list[str]) -> None:
        orphan = stored("old", source_ids=keys)
        assert adopt([fresh("new", start=START + WEEK)], [orphan]) == ["new"]


class TestMovedWithoutSpeakers:
    def test_the_same_title_a_week_later_keeps_the_uid(self) -> None:
        orphan = stored("old", title="Locally testable codes", speakers=[])
        moved = fresh("new", title="Locally testable codes", speakers=[], start=START + WEEK)
        assert adopt([moved], [orphan]) == ["old"]

    def test_a_tba_slot_an_hour_later_keeps_the_uid(self) -> None:
        orphan = stored("old", title="TBA", speakers=[])
        moved = fresh("new", title="TBA", speakers=[], start=START + timedelta(hours=1))
        assert adopt([moved], [orphan]) == ["old"]

    @pytest.mark.parametrize(
        ("old", "new"),
        [("Locally testable codes", "Knots and braids"), ("TBA", "Knots"), ("Knots", "TBA")],
    )
    def test_another_title_is_another_talk(self, old: str, new: str) -> None:
        orphan = stored("old", title=old, speakers=[])
        other = fresh("new", title=new, speakers=[], start=START + WEEK)
        assert adopt([other], [orphan]) == ["new"]

    def test_a_speaker_on_one_side_only_is_another_talk(self) -> None:
        orphan = stored("old", title="TBA", speakers=[])
        assert adopt([fresh("new", title="TBA", start=START + WEEK)], [orphan]) == ["new"]

    def test_a_talk_that_is_over_did_not_move(self) -> None:
        past = REFERENCE_NOW - timedelta(days=1)
        orphan = stored("old", title="TBA", speakers=[], start=past)
        assert adopt([fresh("new", title="TBA", speakers=[])], [orphan]) == ["new"]

    def test_a_slot_that_gains_its_talk_keeps_its_uid_next_to_a_new_placeholder(self) -> None:
        orphan = stored("old", title="TBA", speakers=[])
        talk = fresh("talk", title="Quantum codes")
        placeholder = fresh("next", title="TBA", speakers=[], start=START + WEEK)
        for order in itertools.permutations([talk, placeholder]):
            taken = dict(
                zip((c.event.uid for c in order), adopt(list(order), [orphan]), strict=True)
            )
            assert taken == {"talk": "old", "next": "next"}

    def test_a_guess_between_two_placeholders_is_not_made(self) -> None:
        orphan = stored("old", title="TBA", speakers=[])
        twice = [
            fresh("new", title="TBA", speakers=[], start=START + WEEK),
            fresh("next", title="TBA", speakers=[], start=START + 2 * WEEK),
        ]
        assert adopt(twice, [orphan]) == ["new", "next"]


class TestTwoTalksOfOneDayKey:
    """Two talks of one day key: the earlier one holds the key with its time of day."""

    KEY = day_key(START, "ann-smith")
    FIRST = "Lecture I: Random matrices"
    SECOND = "Lecture II: Free probability"
    EARLY = START - timedelta(hours=4)

    def lectures(self) -> list[Event]:
        return [
            stored("one", title=self.FIRST, start=self.EARLY, source_ids=[f"{self.KEY}:t1200"]),
            stored("two", title=self.SECOND, source_ids=[self.KEY]),
        ]

    @pytest.mark.parametrize("title", [FIRST, "TBA"])
    def test_the_earlier_talk_alone_gets_its_own_uid_back(self, title: str) -> None:
        alone = fresh("two", title=title, start=self.EARLY, source_ids=[self.KEY])
        assert adopt([alone], self.lectures()) == ["one"]

    def test_the_choice_does_not_depend_on_the_order(self) -> None:
        alone = fresh("two", title=self.FIRST, start=self.EARLY, source_ids=[self.KEY])
        other = fresh("other", title="Knots", speakers=people("Bob Jones"))
        assert adopt([alone, other], self.lectures()) == ["one", "other"]
        assert adopt([other, alone], self.lectures()) == ["other", "one"]

    def test_a_talk_under_the_key_with_its_time_gets_the_uid_that_key_is_stored_with(
        self,
    ) -> None:
        timed = [f"{self.KEY}:t1200"]
        hosted = stored("hosted", title="Spectra of graphs", start=self.EARLY, source_ids=timed)
        derived = fresh("sites-x._derived", title=self.FIRST, start=self.EARLY, source_ids=timed)
        assert adopt([derived], [hosted]) == ["hosted"]

    def test_the_later_talk_alone_keeps_its_uid(self) -> None:
        alone = fresh("two", title=self.SECOND, source_ids=[self.KEY])
        assert adopt([alone], self.lectures()) == ["two"]

    def test_the_later_talk_at_the_time_of_the_earlier_one_keeps_its_uid(self) -> None:
        moved = fresh("two", title=self.SECOND, start=self.EARLY, source_ids=[self.KEY])
        assert adopt([moved], self.lectures()) == ["two"]

    def test_a_uid_another_listing_holds_is_not_taken(self) -> None:
        alone = fresh("two", title=self.FIRST, start=self.EARLY, source_ids=[self.KEY])
        holder = fresh("one", title=self.FIRST, source_ids=["sites-y:1"])
        assert adopt([alone, holder], self.lectures()) == ["two", "one"]


class TestSlots:
    def test_a_generic_title_gives_way_to_the_title_of_the_talk(self) -> None:
        lunch = stored("theory-lunch", title="Theory Lunch", speakers=[])
        talk = fresh("locally-testable", title="Locally testable quantum codes", speakers=[])
        assert adopt([talk], [lunch]) == ["theory-lunch"]

    def test_a_slot_is_taken_only_at_the_same_moment(self) -> None:
        lunch = stored("theory-lunch", title="Theory Lunch", speakers=[])
        later = fresh("talk", title="Knots", speakers=[], start=START + timedelta(minutes=5))
        assert adopt([later], [lunch]) == ["talk"]


class TestRenamedPages:
    def page(self, uid: str, path: str, title: str, **overrides: Any) -> dict[str, Any]:
        url = f"https://example.edu{path}"
        values: dict[str, Any] = {
            "title": title,
            "speakers": [],
            "url": url,
            "source_urls": [url],
            "source_ids": [f"sites-x:url:{url}"],
        }
        return {**values, **overrides}

    def test_a_tba_page_with_a_new_address_keeps_the_uid(self) -> None:
        orphan = stored("old", **self.page("old", "/events/coming-soon-3", "TBA"))
        renamed = fresh("new", **self.page("new", "/events/knots-and-braids", "Knots"))
        assert adopt([renamed], [orphan]) == ["old"]

    @pytest.mark.parametrize(
        ("path", "overrides"),
        [
            ("/talks/knots-and-braids", {}),
            ("/events/knots?id=2", {}),
            ("/events/knots-and-braids", {"start": START + timedelta(minutes=5)}),
            ("/events/knots-and-braids", {"speakers": people("Ann Smith")}),
        ],
    )
    def test_another_directory_a_query_a_start_or_a_speaker_is_another_talk(
        self, path: str, overrides: dict[str, Any]
    ) -> None:
        orphan = stored("old", **self.page("old", "/events/coming-soon-3", "TBA"))
        other = fresh("new", **self.page("new", path, "Knots and braids", **overrides))
        assert adopt([other], [orphan]) == ["new"]

    def test_a_page_with_a_title_of_its_own_is_not_renamed(self) -> None:
        orphan = stored("old", **self.page("old", "/events/dark-matter", "Dark matter"))
        other = fresh("new", **self.page("new", "/events/knots-and-braids", "Knots and braids"))
        assert adopt([other], [orphan]) == ["new"]

    def test_two_new_pages_at_the_start_of_one_tba_page_are_a_guess(self) -> None:
        orphan = stored("old", **self.page("old", "/events/coming-soon-3", "TBA"))
        pages = [
            fresh("one", **self.page("one", "/events/knots", "Knots")),
            fresh("two", **self.page("two", "/events/braids", "Braids")),
        ]
        assert adopt(pages, [orphan]) == ["one", "two"]
