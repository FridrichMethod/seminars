"""Tests for the hand-over of stored uids to candidates whose key changed."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from typing import Any

import pytest

from factories import PACIFIC, REFERENCE_NOW, make_event
from seminars.config import DedupSettings
from seminars.models import Candidate, Event, EventHints
from seminars.pipeline.dedup import adopt_stored_uids
from seminars.pipeline.ids import is_time_of_day_key

SETTINGS = DedupSettings()
START = datetime(2026, 11, 17, 16, 0, tzinfo=PACIFIC)
TITLE = "Protein design with diffusion models"


def stored(uid: str, *, first_seen: datetime = REFERENCE_NOW, **overrides: Any) -> Event:
    """A stored talk that its source knows by the address of its page."""
    values: dict[str, Any] = {
        "uid": uid,
        "title": TITLE,
        "speakers": [],
        "start": START,
        "end": None,
        "url": f"https://example.edu/talks/{uid}",
        "source_urls": [f"https://example.edu/talks/{uid}"],
        "source_ids": [f"sites-x:url:https://example.edu/talks/{uid}"],
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
        assert result[0].event.source_ids == ["sites-x:url:https://example.edu/talks/new"]

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
        assert expected["sites-x:url:https://example.edu/talks/three"] == "three"

    def test_a_talk_of_the_same_source_with_another_speaker_is_not_taken(self) -> None:
        old = stored("old", speakers=[{"name": "Ann Smith"}])
        new = fresh("new", speakers=[{"name": "Bea Jones"}])
        assert uids(adopt_stored_uids([new], [old], settings=SETTINGS)) == ["new"]

    @pytest.mark.parametrize(
        ("old_title", "new_title"),
        [
            ("Astrophysics Colloquium", "Astrophysics Colloquium"),
            ("Applied Physics Colloquium", "Applied Physics Colloquium: Knots and braids"),
        ],
    )
    def test_a_talk_known_by_its_native_id_is_not_taken_by_a_similar_title(
        self, old_title: str, new_title: str
    ) -> None:
        old = stored("old", title=old_title, source_ids=["sites-x:1001"])
        new = fresh("new", title=new_title, source_ids=["sites-x:2002"])
        assert uids(adopt_stored_uids([new], [old], settings=SETTINGS)) == ["new"]
        named = new.evolve(event=new.event.evolve(speakers=[{"name": "Bea Jones"}]))
        assert uids(adopt_stored_uids([named], [old], settings=SETTINGS)) == ["new"]

    def test_a_talk_known_by_its_native_id_is_taken_by_the_same_speakers(self) -> None:
        ann = [{"name": "Ann Smith"}]
        old = stored("old", title="TBA", speakers=ann, source_ids=["sites-x:1001"])
        new = fresh("new", title="Enzymes in motion", speakers=ann, source_ids=["sites-x:2002"])
        assert uids(adopt_stored_uids([new], [old], settings=SETTINGS)) == ["old"]

    def test_a_native_id_of_another_source_leaves_the_title_rule(self) -> None:
        old = stored("old", source_ids=["localist-stanford:9"])
        new = fresh("new", source_ids=["sites-x:2002"])
        assert uids(adopt_stored_uids([new], [old], settings=SETTINGS)) == ["old"]

    def test_a_renamed_page_of_the_same_speaker_keeps_the_uid(self) -> None:
        jane = [{"name": "Jane Doe"}]
        old = stored(
            "old",
            title="TBA",
            speakers=jane,
            url="https://example.edu/events/tba-jane-doe",
            source_ids=["sites-x:url:https://example.edu/events/tba-jane-doe"],
        )
        other = stored(
            "other",
            title="TBA",
            speakers=[{"name": "Bea Jones"}],
            url="https://example.edu/events/tba-bea-jones",
            source_ids=["sites-x:url:https://example.edu/events/tba-bea-jones"],
            first_seen=REFERENCE_NOW - timedelta(days=2),
        )
        renamed = fresh(
            "new",
            title="Folding proteins in the cell",
            speakers=jane,
            url="https://example.edu/events/folding-proteins-in-the-cell",
            source_ids=["sites-x:url:https://example.edu/events/folding-proteins-in-the-cell"],
        )
        result = adopt_stored_uids([renamed], [other, old], settings=SETTINGS)
        assert uids(result) == ["old"]

    def test_a_tba_talk_on_a_series_page_is_not_taken_by_another_talk_there(self) -> None:
        series = "https://example.edu/colloquium-series"
        tba = stored("old", title="TBA", url=series, source_urls=[series])
        other = fresh(
            "new", title="Enzymes in motion", url=series, speakers=[{"name": "Bea Jones"}]
        )
        assert uids(adopt_stored_uids([other], [tba], settings=SETTINGS)) == ["new"]
        page_key = stored("old", title="TBA", url=series, source_ids=[f"sites-x:url:{series}"])
        assert uids(adopt_stored_uids([other], [page_key], settings=SETTINGS)) == ["new"]

    def test_the_same_page_is_enough_for_a_listing_of_another_source(self) -> None:
        page = "https://example.edu/talks/9"
        calendar = stored(
            "old", title="TBA", url=page, source_urls=[page], source_ids=["localist-stanford:9"]
        )
        department = fresh("new", title="Enzymes in motion", url=page)
        assert uids(adopt_stored_uids([department], [calendar], settings=SETTINGS)) == ["old"]

    def test_a_page_that_two_talks_of_one_source_point_at_is_no_reason(self) -> None:
        series = "https://example.edu/colloquium-series"
        calendar = stored("old", title="TBA", url=series, source_ids=["localist-stanford:9"])
        later = stored(
            "later",
            title="Knots and braids",
            url=series,
            source_ids=["localist-stanford:10"],
            start=START + timedelta(minutes=10),
        )
        talk = fresh("new", title="Enzymes in motion", url=series)
        result = adopt_stored_uids([talk], [calendar, later], settings=SETTINGS)
        assert uids(result) == ["new"]
        twice = [talk, fresh("next", title="Knots and braids", url=series)]
        assert uids(adopt_stored_uids(twice, [calendar], settings=SETTINGS)) == ["new", "next"]

    def test_a_series_page_tells_the_talk_of_the_week_of_another_source(self) -> None:
        series = "https://example.edu/colloquium-series"
        weekly = [
            stored(
                f"week{week}",
                title="TBA",
                url=series,
                source_ids=[f"localist-stanford:{week}"],
                start=START + timedelta(days=7 * week),
            )
            for week in (0, 1)
        ]
        talk = fresh("new", title="Enzymes in motion", url=series)
        assert uids(adopt_stored_uids([talk], weekly, settings=SETTINGS)) == ["week0"]

    def test_the_same_two_speakers_of_one_source_are_the_same_talk(self) -> None:
        pair = [{"name": "Ann Smith"}, {"name": "Bea Jones"}]
        old = stored("old", title="TBA", speakers=pair)
        new = fresh("new", title="Enzymes in motion", speakers=list(reversed(pair)))
        assert uids(adopt_stored_uids([new], [old], settings=SETTINGS)) == ["old"]
        one = fresh("one", title="Enzymes in motion", speakers=pair[:1])
        assert uids(adopt_stored_uids([one], [old], settings=SETTINGS)) == ["one"]

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

    def test_a_tba_talk_with_a_native_id_is_no_placeholder(self) -> None:
        native = placeholder("t1600", source_ids=["sites-x:1001"])
        result = adopt_stored_uids(
            [announced("protein-design")], [native], settings=SETTINGS, listing_urls=LISTING
        )
        assert uids(result) == ["protein-design"]

    def test_a_tba_talk_with_a_page_of_its_own_is_no_placeholder(self) -> None:
        page = "https://example.edu/talks/1001"
        own = placeholder("t1600", url=page, source_urls=[page], source_ids=[f"sites-x:url:{page}"])
        result = adopt_stored_uids(
            [announced("protein-design")], [own], settings=SETTINGS, listing_urls=LISTING
        )
        assert uids(result) == ["protein-design"]

    def test_a_stable_key_next_to_a_slot_key_is_no_placeholder(self) -> None:
        both = placeholder("t1600", source_ids=["sites-x:1001", "sites-x:day:2026-11-17:t1600"])
        result = adopt_stored_uids(
            [announced("protein-design")], [both], settings=SETTINGS, listing_urls=LISTING
        )
        assert uids(result) == ["protein-design"]

    def test_the_slot_key_of_another_source_does_not_count(self) -> None:
        merged = placeholder(
            "t1600", source_ids=["sites-x:1001", "localist-stanford:day:2026-11-17:t1600"]
        )
        result = adopt_stored_uids(
            [announced("protein-design")], [merged], settings=SETTINGS, listing_urls=LISTING
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


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("sites-x:day:2026-11-17:t1600", True),
        ("sites-x:day:2026-11-17:ada-lovelace", False),
        ("sites-x:day:2026-11-17:t1600-knots", False),
        ("sites-x:1001", False),
        ("sites-x:url:https://example.edu/talks/1", False),
        ("sites-x:t1600", False),
    ],
)
def test_time_of_day_keys(key: str, expected: bool) -> None:
    assert is_time_of_day_key(key) is expected
