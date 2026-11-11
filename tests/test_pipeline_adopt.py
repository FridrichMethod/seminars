"""Tests for the hand-over of stored uids to candidates whose key changed."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from typing import Any

from factories import PACIFIC, REFERENCE_NOW, make_event
from seminars.config import DedupSettings
from seminars.models import Candidate, Event, EventHints
from seminars.pipeline.dedup import adopt_stored_uids

SETTINGS = DedupSettings()
START = datetime(2026, 11, 17, 16, 0, tzinfo=PACIFIC)
TITLE = "Protein design with diffusion models"


def stored(uid: str, *, first_seen: datetime = REFERENCE_NOW, **overrides: Any) -> Event:
    values: dict[str, Any] = {
        "uid": uid,
        "title": TITLE,
        "speakers": [],
        "start": START,
        "end": None,
        "url": f"https://example.edu/talks/{uid}",
        "source_urls": [f"https://example.edu/talks/{uid}"],
        "source_ids": [f"sites-x:{uid}"],
        "first_seen": first_seen,
    }
    return make_event(**{**values, **overrides})


def fresh(uid: str, **overrides: Any) -> Candidate:
    event = stored(uid, **overrides)
    return Candidate(event=event, hints=EventHints(source_ids=["sites-x"]))


def uids(candidates: list[Candidate]) -> list[str]:
    return [candidate.event.uid for candidate in candidates]


class TestAdoption:
    def test_a_new_key_of_a_stored_talk_keeps_the_stored_uid(self) -> None:
        result = adopt_stored_uids([fresh("new")], [stored("old")], settings=SETTINGS)
        assert uids(result) == ["old"]
        assert result[0].event.source_ids == ["sites-x:new"]

    def test_nothing_is_adopted_from_a_different_talk(self) -> None:
        other = stored("old", title="Dark matter searches underground")
        result = adopt_stored_uids([fresh("new")], [other], settings=SETTINGS)
        assert uids(result) == ["new"]

    def test_nothing_is_adopted_beyond_the_time_limit(self) -> None:
        late = stored("old", start=START + timedelta(minutes=16))
        assert uids(adopt_stored_uids([fresh("new")], [late], settings=SETTINGS)) == ["new"]

    def test_a_stored_event_that_is_delivered_again_is_not_taken(self) -> None:
        candidates = [fresh("old"), fresh("new", start=START + timedelta(minutes=5))]
        result = adopt_stored_uids(candidates, [stored("old")], settings=SETTINGS)
        assert uids(result) == ["old", "new"]

    def test_a_known_uid_never_changes(self) -> None:
        events = [stored("old"), stored("other", title="Protein design with diffusion")]
        result = adopt_stored_uids([fresh("other")], events, settings=SETTINGS)
        assert uids(result) == ["other"]

    def test_the_event_seen_first_is_taken_and_each_only_once(self) -> None:
        early = stored("early", first_seen=REFERENCE_NOW - timedelta(days=3))
        late = stored("late", first_seen=REFERENCE_NOW - timedelta(days=1))
        result = adopt_stored_uids([fresh("one")], [late, early], settings=SETTINGS)
        assert uids(result) == ["early"]
        both = adopt_stored_uids([fresh("one"), fresh("two")], [late, early], settings=SETTINGS)
        assert sorted(uids(both)) == ["early", "late"]

    def test_the_choice_does_not_depend_on_the_order(self) -> None:
        events = [
            stored("early", first_seen=REFERENCE_NOW - timedelta(days=3)),
            stored("late", first_seen=REFERENCE_NOW - timedelta(days=1)),
        ]
        candidates = [
            fresh("one"),
            fresh("two", abstract="Another listing of the same talk."),
            fresh("three", title="Dark matter searches underground"),
        ]
        expected: dict[str, str] | None = None
        for order in itertools.permutations(candidates):
            for stored_order in itertools.permutations(events):
                result = adopt_stored_uids(list(order), list(stored_order), settings=SETTINGS)
                taken = {
                    before.event.source_ids[0]: after.event.uid
                    for before, after in zip(order, result, strict=True)
                }
                expected = expected or taken
                assert taken == expected
        assert expected is not None
        assert expected["sites-x:three"] == "three"

    def test_listing_pages_do_not_count_as_the_same_detail_page(self) -> None:
        listing = "https://example.edu/events"
        old = stored("old", title="TBA", url=listing, source_ids=["sites-y:old"])
        new = fresh("new", title="TBA", url=listing)
        result = adopt_stored_uids([new], [old], settings=SETTINGS, listing_urls={listing})
        assert uids(result) == ["new"]

    def test_no_stored_events_changes_nothing(self) -> None:
        candidates = [fresh("a"), fresh("b")]
        assert adopt_stored_uids(candidates, [], settings=SETTINGS) == candidates


def placeholder(uid: str, **overrides: Any) -> Event:
    """A stored TBA talk without a speaker or a page of its own, as day keys give them."""
    values: dict[str, Any] = {
        "title": "TBA",
        "url": "https://example.edu/events",
        "source_urls": ["https://example.edu/events"],
        "source_ids": [f"sites-x:day:2026-11-17:{uid}"],
    }
    return stored(uid, **{**values, **overrides})


def announced(uid: str, **overrides: Any) -> Candidate:
    values: dict[str, Any] = {
        "url": "https://example.edu/events",
        "source_urls": ["https://example.edu/events"],
        "source_ids": [f"sites-x:day:2026-11-17:{uid}"],
    }
    return fresh(uid, **{**values, **overrides})


LISTING = {"https://example.edu/events"}


class TestPlaceholders:
    def test_a_tba_slot_is_taken_by_the_talk_announced_for_it(self) -> None:
        result = adopt_stored_uids(
            [announced("protein-design")], [placeholder("t1600")], settings=SETTINGS
        )
        assert uids(result) == ["t1600"]

    def test_a_speaker_added_to_a_tba_slot_keeps_it(self) -> None:
        speaker = {"name": "Ada Lovelace"}
        candidate = announced("ada-lovelace", title="TBA", speakers=[speaker])
        result = adopt_stored_uids(
            [candidate], [placeholder("t1600")], settings=SETTINGS, listing_urls=LISTING
        )
        assert uids(result) == ["t1600"]

    def test_a_slot_of_another_source_is_not_taken(self) -> None:
        other = placeholder("t1600", source_ids=["sites-y:day:2026-11-17:t1600"])
        result = adopt_stored_uids(
            [announced("protein-design")], [other], settings=SETTINGS, listing_urls=LISTING
        )
        assert uids(result) == ["protein-design"]

    def test_the_start_must_be_the_same_moment(self) -> None:
        later = placeholder("t1605", start=START + timedelta(minutes=5))
        result = adopt_stored_uids(
            [announced("protein-design")], [later], settings=SETTINGS, listing_urls=LISTING
        )
        assert uids(result) == ["protein-design"]

    def test_two_slots_of_one_source_and_start_are_a_guess_and_left_alone(self) -> None:
        slots = [placeholder("t1600"), placeholder("room-b", source_ids=["sites-x:room-b"])]
        result = adopt_stored_uids(
            [announced("protein-design")], slots, settings=SETTINGS, listing_urls=LISTING
        )
        assert uids(result) == ["protein-design"]

    def test_a_tba_talk_with_a_speaker_is_no_placeholder(self) -> None:
        named = placeholder("ada-lovelace", speakers=[{"name": "Ada Lovelace"}])
        candidate = announced("grace-hopper", speakers=[{"name": "Grace Hopper"}])
        result = adopt_stored_uids([candidate], [named], settings=SETTINGS, listing_urls=LISTING)
        assert uids(result) == ["grace-hopper"]

    def test_a_talk_that_matches_by_the_rules_leaves_the_slot_to_another(self) -> None:
        events = [
            stored("old", first_seen=REFERENCE_NOW - timedelta(days=5)),
            placeholder("t1600"),
        ]
        candidates = [
            fresh("new"),
            announced("dark-matter", title="Dark matter searches underground"),
        ]
        for order in itertools.permutations(candidates):
            result = adopt_stored_uids(list(order), events, settings=SETTINGS, listing_urls=LISTING)
            taken = {
                before.event.uid: after.event.uid
                for before, after in zip(order, result, strict=True)
            }
            assert taken == {"new": "old", "dark-matter": "t1600"}

    def test_one_slot_goes_to_one_talk_whatever_the_order(self) -> None:
        candidates = [
            announced("protein-design"),
            announced("dark-matter", title="Dark matter searches underground"),
        ]
        results = set()
        for order in itertools.permutations(candidates):
            result = adopt_stored_uids(
                list(order), [placeholder("t1600")], settings=SETTINGS, listing_urls=LISTING
            )
            taken = {
                before.event.uid: after.event.uid
                for before, after in zip(order, result, strict=True)
            }
            assert sorted(taken.values()).count("t1600") == 1
            results.add(tuple(sorted(taken.items())))
        assert len(results) == 1
