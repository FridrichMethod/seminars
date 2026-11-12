"""Tests for de-duplication: the pair rules, the merge rules and the properties."""

from __future__ import annotations

import itertools
import random
from datetime import datetime, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from factories import PACIFIC, REFERENCE_NOW, events, make_candidate, make_event
from seminars.config import DedupSettings
from seminars.models import Candidate, EventHints, Speaker
from seminars.pipeline.dedup import DedupResult, dedupe, separate_listings
from seminars.pipeline.text import canonical_name

SETTINGS = DedupSettings()
START = datetime(2026, 11, 17, 16, 0, tzinfo=PACIFIC)
AGGREGATORS = frozenset({"localist-stanford"})
LISTING = frozenset({"https://nlp.example.edu/seminar"})


DISTINCT_TITLES = {
    "a": "Knots in three-manifolds",
    "b": "Dark matter searches underground",
    "c": "Enzyme catalysis mechanisms revealed",
}


def candidate(uid: str, *, sources: tuple[str, ...] = ("sites-x",), **overrides: Any) -> Candidate:
    """A candidate with its own uid, key, address and no speaker unless told otherwise."""
    values: dict[str, Any] = {
        "uid": uid,
        "source_ids": [f"{sources[0]}:{uid}"],
        "url": f"https://example.edu/talks/{uid}",
        "source_urls": [f"https://example.edu/talks/{uid}"],
        "title": DISTINCT_TITLES.get(uid, f"{uid} {uid}"),
        "speakers": [],
        "start": START,
        "end": None,
        "fields": [],
        "abstract": None,
    }
    event = make_event(**{**values, **overrides})
    return Candidate(event=event, hints=EventHints(source_ids=list(sources)))


def run(*candidates: Candidate) -> DedupResult:
    return dedupe(
        list(candidates), settings=SETTINGS, aggregators=AGGREGATORS, listing_urls=LISTING
    )


def uids(result: DedupResult) -> list[str]:
    return [item.event.uid for item in result.candidates]


class TestPairs:
    def test_unrelated_talks_at_the_same_time_stay_apart(self) -> None:
        result = run(candidate("a"), candidate("b"))
        assert uids(result) == ["a", "b"]
        assert result.absorbed == {}

    def test_similar_titles_within_the_time_limit_are_one_talk(self) -> None:
        first = candidate("a", title="Protein design with diffusion models")
        second = candidate(
            "b", title="PROTEIN DESIGN with Diffusion Models!", start=START + timedelta(minutes=15)
        )
        assert uids(run(first, second)) == ["a"]

    def test_the_time_limit_is_inclusive_and_strict_beyond(self) -> None:
        first = candidate("a", title="Protein design with diffusion models")
        late = candidate(
            "b", title="Protein design with diffusion models", start=START + timedelta(minutes=16)
        )
        assert uids(run(first, late)) == ["a", "b"]

    def test_a_shared_speaker_is_enough(self) -> None:
        first = candidate("a", speakers=[Speaker(name="Jos\u00e9 Garc\u00eda")])
        second = candidate("b", speakers=[Speaker(name="Prof. Jose GARCIA, PhD")], title="TBA")
        assert uids(run(first, second)) == ["a"]

    def test_a_speaker_named_in_the_title_of_the_other_is_enough(self) -> None:
        # The campus calendar names the speaker only in its title; the host lists them.
        calendar = candidate(
            "a", sources=("localist-stanford",), title="MRC Public Lecture: Tadashi Tokieda"
        )
        host = candidate(
            "b",
            sources=("sites-mrc",),
            title="Tadashi Tokieda: Surprises from rubbing the wrong way",
            speakers=[Speaker(name="Prof. Tadashi Tokieda")],
            start=START + timedelta(minutes=10),
        )
        for order in ((calendar, host), (host, calendar)):
            assert uids(run(*order)) == ["a"]

    def test_a_name_of_one_word_in_the_other_title_is_not_enough(self) -> None:
        first = candidate("a", title="Li-ion batteries at the nanoscale")
        second = candidate("b", sources=("sites-y",), speakers=[Speaker(name="Li")])
        assert uids(run(first, second)) == ["a", "b"]

    def test_a_speaker_named_in_a_title_of_one_source_does_not_join_two_talks(self) -> None:
        # One source lists two colloquia at the same hour; the calendar's title names one.
        first = candidate("a", speakers=[Speaker(name="Jane Doe")])
        second = candidate("b", speakers=[Speaker(name="John Roe")])
        calendar = candidate("c", sources=("sites-y",), title="Colloquium: Jane Doe")
        result = run(first, second, calendar)
        assert uids(result) == ["a", "b"]
        assert result.absorbed == {"c": "a"}

    def test_the_same_detail_page_is_enough(self) -> None:
        first = candidate("a", url="https://example.edu/talks/7/")
        second = candidate("b", sources=("sites-y",), url="HTTPS://EXAMPLE.edu/talks/7#abstract")
        assert uids(run(first, second)) == ["a"]

    def test_a_page_that_two_talks_of_one_source_point_at_is_not_a_detail_page(self) -> None:
        series = "https://example.edu/colloquium-series"
        tba = candidate("a", title="TBA", url=series)
        talk = candidate("b", url=series, speakers=[Speaker(name="Bea Jones")])
        assert uids(run(tba, talk)) == ["a", "b"]
        calendar = candidate("c", sources=("sites-y",), title="TBA", url=series)
        assert uids(run(tba, talk, calendar)) == ["a", "b", "c"]
        assert uids(run(tba, calendar)) == ["a"]

    def test_a_page_shared_a_week_apart_still_tells_the_talk(self) -> None:
        series = "https://events.example.edu/event/weekly-colloquium"
        weekly = [
            candidate(uid, sources=("localist-stanford",), title="TBA", url=series, start=start)
            for uid, start in (("a", START), ("b", START + timedelta(days=7)))
        ]
        copy = candidate("c", title="Enzymes in motion", url=series, start=START)
        result = run(*weekly, copy)
        assert uids(result) == ["a", "b"]
        assert result.absorbed == {"c": "a"}

    def test_one_listing_seen_twice_still_has_its_page(self) -> None:
        page = "https://example.edu/talks/7"
        first = candidate("a", title="TBA", url=page)
        again = candidate("a", title="TBA", url=page, abstract="The same listing again.")
        calendar = candidate("c", sources=("sites-y",), title="TBA", url=page)
        assert uids(run(first, again, calendar)) == ["a"]

    def test_a_listing_page_or_a_home_page_is_not_a_detail_page(self) -> None:
        listed = [candidate(uid, url="https://nlp.example.edu/seminar") for uid in "ab"]
        assert uids(run(*listed)) == ["a", "b"]
        home = [candidate(uid, url="https://example.edu/") for uid in "ab"]
        assert uids(run(*home)) == ["a", "b"]

    def test_tba_is_not_similar_to_tba(self) -> None:
        assert uids(run(candidate("a", title="TBA"), candidate("b", title="TBA"))) == ["a", "b"]

    def test_a_one_word_title_is_not_compared(self) -> None:
        generic = candidate("a", title="Colloquium")
        math = candidate("b", title="Mathematics Colloquium: knots")
        physics = candidate("c", title="Physics Colloquium: dark matter")
        assert uids(run(generic, math, physics)) == ["a", "b", "c"]

    def test_the_same_uid_is_one_talk_whatever_else_differs(self) -> None:
        first = candidate("a")
        moved = candidate("a", start=START + timedelta(days=3), title="Something else entirely")
        assert uids(run(first, moved)) == ["a"]

    def test_a_shared_source_key_is_one_talk(self) -> None:
        first = candidate("a", source_ids=["sites-x:1", "localist-stanford:9"])
        second = candidate("b", source_ids=["localist-stanford:9"], start=START + timedelta(days=1))
        result = run(first, second)
        assert uids(result) == ["a"]
        assert result.absorbed == {"b": "a"}

    def test_pairs_chain_into_one_group(self) -> None:
        title = "Protein design with diffusion models"
        chain = [
            candidate(uid, title=title, start=START + timedelta(minutes=offset))
            for uid, offset in (("a", 0), ("b", 12), ("c", 24))
        ]
        assert uids(run(*chain)) == ["a"]

    def test_merging_repeats_until_nothing_matches(self) -> None:
        """a and b share a speaker; only their merged event (title of b, time of a) matches c."""
        title = "Protein design with diffusion models"
        jane = [Speaker(name="Jane Doe")]
        first = candidate("a", title="TBA", speakers=jane)
        second = candidate("b", title=title, speakers=jane, start=START + timedelta(minutes=14))
        third = candidate("c", title=title, start=START - timedelta(minutes=12))
        assert uids(run(first, third)) == ["a", "c"]
        assert uids(run(second, third)) == ["b", "c"]
        result = run(first, second, third)
        assert uids(result) == ["a"]
        assert result.absorbed == {"b": "a", "c": "a"}
        assert run(*result.candidates).candidates == result.candidates


class TestOneSourceTwoSpeakers:
    """Two listings of one source with different speakers are two talks."""

    COLLOQUIUM = "Astrophysics Colloquium"
    PAGE = "https://kipac.example.edu/colloquium"

    def talk(
        self, uid: str, name: str | None, source: str = "bayarea-kipac", **overrides: Any
    ) -> Candidate:
        values: dict[str, Any] = {
            "title": self.COLLOQUIUM,
            "speakers": [Speaker(name=name)] if name else [],
            "url": self.PAGE,
            "source_urls": [self.PAGE],
        }
        return candidate(uid, sources=(source,), **{**values, **overrides})

    def test_the_same_title_and_page_do_not_merge_them(self) -> None:
        result = run(self.talk("x", "Ann Smith"), self.talk("y", "Bea Jones"))
        assert uids(result) == ["x", "y"]

    def test_a_listing_of_another_source_does_not_join_them(self) -> None:
        both = self.talk(
            "agg",
            None,
            "localist-stanford",
            speakers=[Speaker(name="Ann Smith"), Speaker(name="Bea Jones")],
            url="https://events.example.edu/event/9",
            source_urls=["https://events.example.edu/event/9"],
        )
        talks = [self.talk("x", "Ann Smith"), self.talk("y", "Bea Jones"), both]
        results = [run(*order) for order in itertools.permutations(talks)]
        assert all(result == results[0] for result in results)
        keys = [set(item.event.source_ids) for item in results[0].candidates]
        assert len(keys) == 2
        assert not any({"bayarea-kipac:x", "bayarea-kipac:y"} <= group for group in keys)
        assert run(*results[0].candidates).candidates == results[0].candidates

    def test_a_listing_without_a_speaker_still_joins_one_of_them(self) -> None:
        assert uids(run(self.talk("x", "Ann Smith"), self.talk("y", None))) == ["x"]

    def test_the_same_speaker_in_other_spellings_is_one_talk(self) -> None:
        result = run(self.talk("x", "Ann Smith"), self.talk("y", "Dr. Ann SMITH, PhD"))
        assert uids(result) == ["x"]

    def test_two_listings_with_the_same_two_speakers_stay_apart(self) -> None:
        pair = [Speaker(name="Ann Smith"), Speaker(name="Bea Jones")]
        result = run(self.talk("x", None, speakers=pair), self.talk("y", None, speakers=pair))
        assert uids(result) == ["x", "y"]

    def test_listings_of_different_sources_with_different_speakers_can_merge(self) -> None:
        other = self.talk("y", "A. Smith", "sites-x")
        assert uids(run(self.talk("x", "Ann Smith"), other)) == ["x"]


class TestSeparation:
    """Listings of one source that the uid map holds under one uid but name other speakers."""

    def listing(self, key: str, name: str | None, uid: str = "sites-x.1001") -> Candidate:
        source = key.partition(":")[0]
        values: dict[str, Any] = {
            "title": "Astrophysics Colloquium",
            "speakers": [Speaker(name=name)] if name else [],
            "source_ids": [key],
        }
        return candidate(uid, sources=(source,), **values)

    def test_two_talks_under_one_uid_get_a_uid_each(self) -> None:
        listings = [self.listing("sites-x:1001", "Ann Smith"), self.listing("sites-x:2002", "Bea")]
        assert uids_of(separate_listings(listings)) == ["sites-x.1001", "sites-x.2002"]
        result = run(*separate_listings(listings))
        assert uids(result) == ["sites-x.1001", "sites-x.2002"]
        assert result.aliases == {"sites-x:1001": "sites-x.1001", "sites-x:2002": "sites-x.2002"}

    def test_the_key_the_uid_came_from_keeps_it_else_the_smallest_key(self) -> None:
        derived = [self.listing("sites-x:2002", "Ann Smith", "sites-x.2002")]
        derived.append(self.listing("sites-x:1001", "Bea Jones", "sites-x.2002"))
        assert uids_of(separate_listings(derived)) == ["sites-x.2002", "sites-x.1001"]
        foreign = [self.listing("sites-x:2002", "Ann Smith", "sites-b.3003")]
        foreign.append(self.listing("sites-x:1001", "Bea Jones", "sites-b.3003"))
        assert uids_of(separate_listings(foreign)) == ["sites-x.2002", "sites-b.3003"]

    def test_no_speaker_or_the_same_speakers_keep_the_uid(self) -> None:
        listings = [
            self.listing("sites-x:1001", "Ann Smith"),
            self.listing("sites-x:2002", None),
            self.listing("sites-x:3003", "Dr. Ann SMITH"),
        ]
        assert separate_listings(listings) == listings

    def test_listings_of_other_sources_keep_the_uid(self) -> None:
        listings = [
            self.listing("sites-x:1001", "Ann Smith"),
            self.listing("localist-stanford:9", "Bea Jones"),
        ]
        assert separate_listings(listings) == listings

    def test_one_listing_seen_twice_moves_as_one(self) -> None:
        twice = [self.listing("sites-x:2002", "Bea Jones"), self.listing("sites-x:2002", "Bea")]
        listings = [self.listing("sites-x:1001", "Ann Smith"), *twice]
        assert uids_of(separate_listings(listings)) == ["sites-x.1001", *["sites-x.2002"] * 2]

    def test_a_listing_that_moves_to_a_held_uid_moves_the_holder_on(self) -> None:
        listings = [
            self.listing("sites-x:1001", "Ann Smith"),
            self.listing("sites-x:2002", "Bea Jones"),
            self.listing("sites-x:3003", "Carl Diaz", "sites-x.2002"),
        ]
        moved = separate_listings(listings)
        assert uids_of(moved) == ["sites-x.1001", "sites-x.2002", "sites-x.3003"]
        assert separate_listings(moved) == moved

    def test_the_result_does_not_depend_on_the_order(self) -> None:
        listings = [
            self.listing("sites-x:3003", "Ann Smith"),
            self.listing("sites-x:2002", "Bea Jones"),
            self.listing("sites-x:4004", None),
            self.listing("sites-x:5005", "Bea Jones"),
        ]
        expected = {
            "sites-x:2002": "sites-x.1001",
            "sites-x:3003": "sites-x.3003",
            "sites-x:4004": "sites-x.1001",
            "sites-x:5005": "sites-x.1001",
        }
        for order in itertools.permutations(listings):
            moved = separate_listings(list(order))
            assert {item.event.source_ids[0]: item.event.uid for item in moved} == expected


def uids_of(candidates: list[Candidate]) -> list[str]:
    return [item.event.uid for item in candidates]


class TestMerge:
    def site(self, **overrides: Any) -> Candidate:
        values: dict[str, Any] = {
            "title": "Protein design with diffusion models",
            "host_org": "Stanford Bioengineering",
            "first_seen": REFERENCE_NOW,
        }
        return candidate("site-1", sources=("sites-bioengineering",), **{**values, **overrides})

    def aggregator(self, **overrides: Any) -> Candidate:
        values: dict[str, Any] = {
            "title": "Protein design with diffusion models",
            "host_org": "Stanford University",
            "url": "https://events.example.edu/event/1",
            "source_urls": ["https://events.example.edu/event/1"],
            "first_seen": REFERENCE_NOW - timedelta(days=4),
        }
        return candidate("agg-1", sources=("localist-stanford",), **{**values, **overrides})

    def merged(self, *members: Candidate) -> Candidate:
        (result,) = run(*members).candidates
        return result

    def test_the_uid_seen_first_survives_and_the_other_is_reported(self) -> None:
        result = run(self.site(), self.aggregator())
        assert uids(result) == ["agg-1"]
        assert result.absorbed == {"site-1": "agg-1"}
        assert result.aliases == {
            "localist-stanford:agg-1": "agg-1",
            "sites-bioengineering:site-1": "agg-1",
        }
        assert result.candidates[0].event.first_seen == REFERENCE_NOW - timedelta(days=4)

    def test_equal_first_seen_falls_back_to_the_uid(self) -> None:
        both = run(self.site(), self.aggregator(first_seen=REFERENCE_NOW))
        assert uids(both) == ["agg-1"]

    def test_the_lead_listing_does_not_change_once_both_share_the_uid(self) -> None:
        # The uid map gives both listings the uid of the merged event in the next run.
        title = "Protein design with diffusion models"
        first = candidate("sites-a.1", sources=("sites-a",), title=title, host_org="Zeta")
        second = candidate("sites-b.1", sources=("sites-b",), title=title, host_org="Alpha")
        first = first.model_copy(update={"event": first.event.evolve(location="Z")})
        second = second.model_copy(update={"event": second.event.evolve(location="A")})
        merged = run(first, second).candidates[0].event
        assert (merged.uid, merged.host_org, merged.location) == ("sites-a.1", "Zeta", "Z")
        again = [
            item.model_copy(update={"event": item.event.evolve(uid=merged.uid)})
            for item in (first, second)
        ]
        for order in (again, again[::-1]):
            (result,) = run(*order).candidates
            assert result.event == merged

    def test_the_host_listing_supplies_page_times_place_and_host(self) -> None:
        site = self.site(
            location="Shriram 104", start=START + timedelta(minutes=5), event_type="colloquium"
        )
        aggregator = self.aggregator(location="Campus", end=START + timedelta(hours=1))
        event = self.merged(site, aggregator).event
        assert str(event.url) == "https://example.edu/talks/site-1"
        assert event.host_org == "Stanford Bioengineering"
        assert event.start == START + timedelta(minutes=5)
        assert event.end is None
        assert (event.location, event.event_type) == ("Shriram 104", "colloquium")

    def test_missing_values_are_filled_from_the_other_listing(self) -> None:
        site = self.site(location=None, series=None)
        aggregator = self.aggregator(
            location="Campus",
            series="Bioengineering Seminar",
            end=START + timedelta(hours=1),
            stream_url="https://example.edu/live",
            is_online=True,
        )
        event = self.merged(site, aggregator).event
        assert (event.location, event.series) == ("Campus", "Bioengineering Seminar")
        assert event.end == START + timedelta(hours=1)
        assert (event.is_online, str(event.stream_url)) == (True, "https://example.edu/live")

    def test_a_real_title_beats_tba_and_the_longest_abstract_wins(self) -> None:
        speaker = [Speaker(name="Jane Doe")]
        site = self.site(title="TBA", abstract="Short.", speakers=speaker)
        aggregator = self.aggregator(abstract="A much longer abstract.", speakers=speaker)
        event = self.merged(site, aggregator).event
        assert event.title == "Protein design with diffusion models"
        assert event.abstract == "A much longer abstract."

    def test_the_title_of_the_talk_beats_an_announcement_that_names_the_speaker(self) -> None:
        # Two hosts list one talk: one by its announcement, one by the title of the talk.
        speaker = [Speaker(name="David Garfield")]
        announcement = candidate(
            "csb-1",
            sources=("custom-csb-events",),
            title="CSB Special Seminar & Open Discussion, David Garfield, PhD",
            speakers=speaker,
            url="https://example.edu/shared",
            source_urls=["https://example.edu/shared"],
        )
        talk = candidate(
            "zz-chemh-1",
            sources=("sites-chemh",),
            title="Science with Claude: language models in the lab",
            speakers=speaker,
            url="https://example.edu/shared",
            source_urls=["https://example.edu/shared"],
        )
        for order in ((announcement, talk), (talk, announcement)):
            event = self.merged(*order).event
            assert event.title == "Science with Claude: language models in the lab"

    def test_of_titles_without_a_speaker_the_one_that_says_most_wins(self) -> None:
        site = self.site(title="Diffusion models", series="Bioengineering Seminar")
        aggregator = self.aggregator(
            title="Bioengineering Seminar: Diffusion models for protein design",
            series="Bioengineering Seminar",
        )
        event = self.merged(site, aggregator).event
        assert event.title == "Bioengineering Seminar: Diffusion models for protein design"

    def test_the_words_of_the_series_say_nothing_and_the_host_title_stays(self) -> None:
        site = self.site(title="Diffusion models", series="Bioengineering Seminar")
        aggregator = self.aggregator(
            title="Bioengineering Seminar: Diffusion models", series="Bioengineering Seminar"
        )
        assert self.merged(site, aggregator).event.title == "Diffusion models"

    @pytest.mark.parametrize(
        ("talk", "speaker", "generic"),
        [
            (
                "Jane Doe: Protein design with diffusion models",
                "Jane Doe",
                "Bioengineering Seminar",
            ),
            (
                "Protein design with diffusion models - Jane Doe (MIT)",
                "Jane Doe",
                "Applied Physics Colloquium",
            ),
            ("Li-ion batteries at the nanoscale", "Li", "Energy Seminar"),
        ],
    )
    def test_a_talk_title_that_names_its_speaker_beats_the_name_of_the_series(
        self, talk: str, speaker: str, generic: str
    ) -> None:
        speakers = [Speaker(name=speaker)]
        for named_by_host in (True, False):
            site = self.site(
                title=talk if named_by_host else generic, series=generic, speakers=speakers
            )
            aggregator = self.aggregator(
                title=generic if named_by_host else talk, series=generic, speakers=speakers
            )
            assert self.merged(site, aggregator).event.title == talk, named_by_host

    def test_a_title_that_names_the_speaker_gives_way_to_one_that_says_as_much(self) -> None:
        speakers = [Speaker(name="Jane Doe")]
        for named_by_host in (True, False):
            site = self.site(
                title="Jane Doe on protein design" if named_by_host else "Protein design",
                speakers=speakers,
            )
            aggregator = self.aggregator(
                title="Protein design" if named_by_host else "Jane Doe on protein design",
                speakers=speakers,
            )
            assert self.merged(site, aggregator).event.title == "Protein design", named_by_host

    @pytest.mark.parametrize(
        "wrapper",
        [
            "Jane Doe, PhD: Protein design",
            "Prof. Jane Doe - Protein design",
            "Jane Doe (MIT): Protein design",
            'Bioengineering Seminar Series - Jane Doe, MD "Protein design "',
        ],
    )
    def test_a_title_that_wraps_the_talk_title_with_its_speaker_gives_way_to_it(
        self, wrapper: str
    ) -> None:
        speakers = [Speaker(name="Jane Doe")]
        for wrapped_by_host in (True, False):
            site = self.site(
                title=wrapper if wrapped_by_host else "Protein design",
                series=None,
                speakers=speakers,
            )
            aggregator = self.aggregator(
                title="Protein design" if wrapped_by_host else wrapper,
                series=None,
                speakers=speakers,
            )
            for order in ((site, aggregator), (aggregator, site)):
                title = self.merged(*order).event.title
                assert title == "Protein design", (wrapper, wrapped_by_host)

    def test_a_calendar_wrapper_with_series_speaker_degree_and_quotes_gives_way(self) -> None:
        # The campus calendar wraps the department's title; "biology" is not in the series.
        clean = "Ion channels and transporters regulating lymphocyte function"
        wrapper = f'Biology Seminar Series - Stefan Feske, MD "{clean} "'
        speakers = [Speaker(name="Stefan Feske")]
        site = self.site(title=clean, series=None, speakers=speakers)
        aggregator = self.aggregator(title=wrapper, series="Department Seminars", speakers=speakers)
        for order in ((site, aggregator), (aggregator, site)):
            assert self.merged(*order).event.title == clean

    @pytest.mark.parametrize(
        ("talk", "generic"),
        [
            ("Jane Doe: CRISPR screens", "Genetics Department Seminar Series"),
            ("Jane Doe (MIT): Diffusion", "Stanford Bioengineering Department Seminar"),
        ],
    )
    def test_the_host_talk_title_beats_a_generic_title_of_the_aggregator(
        self, talk: str, generic: str
    ) -> None:
        speakers = [Speaker(name="Jane Doe")]
        site = self.site(title=talk, series=None, speakers=speakers)
        aggregator = self.aggregator(title=generic, series=None, speakers=speakers)
        for order in ((site, aggregator), (aggregator, site)):
            assert self.merged(*order).event.title == talk

    @pytest.mark.parametrize(
        ("generic", "talk"),
        [
            ("Physics Colloquium", "Physics Colloquium: Jane Doe, Quantum materials"),
            ("Biology Seminar", "Biology Seminar - Jane Doe: Protein design"),
            ("Genetics Seminar", "Genetics Seminar: Jane Doe on CRISPR screens"),
        ],
    )
    def test_a_talk_title_that_begins_with_a_generic_title_does_not_give_way_to_it(
        self, generic: str, talk: str
    ) -> None:
        # The talk title adds more words to the generic one than the two share: no wrapper.
        speakers = [Speaker(name="Jane Doe")]
        for talk_by_host in (True, False):
            site = self.site(
                title=talk if talk_by_host else generic, series=None, speakers=speakers
            )
            aggregator = self.aggregator(
                title=generic if talk_by_host else talk, series=None, speakers=speakers
            )
            for order in ((site, aggregator), (aggregator, site)):
                assert self.merged(*order).event.title == talk, talk_by_host

    @pytest.mark.parametrize(
        ("plain", "naming", "kept"),
        [
            # (2) A generic title with more words than the talk title wins as an announcement.
            ("Genetics and Genomics Seminar Series", "Jane Doe: CRISPR", "plain"),
            # (3) A generic title that holds more words than the talk title adds wins.
            (
                "Applied Physics Colloquium",
                "Applied Physics Colloquium: Jane Doe, Materials",
                "plain",
            ),
            # A wrapper that adds as many words as it holds stays.
            ("Diffusion", "Jane Doe (MIT): Diffusion", "naming"),
        ],
    )
    def test_the_known_limits_of_counting_words(self, plain: str, naming: str, kept: str) -> None:
        # Pinned so that a change of the rules is deliberate (see the module docstring).
        speakers = [Speaker(name="Jane Doe")]
        expected = plain if kept == "plain" else naming
        for plain_by_host in (True, False):
            site = self.site(
                title=plain if plain_by_host else naming, series=None, speakers=speakers
            )
            aggregator = self.aggregator(
                title=naming if plain_by_host else plain, series=None, speakers=speakers
            )
            for order in ((site, aggregator), (aggregator, site)):
                assert self.merged(*order).event.title == expected, plain_by_host

    def test_without_a_series_a_generic_host_title_still_beats_the_aggregator(self) -> None:
        # Known limit: no rule tells "Applied Physics Colloquium" from a talk title, so the
        # titles are incomparable and the host listing decides.
        talk = "Protein design with diffusion models - Jane Doe (MIT)"
        speakers = [Speaker(name="Jane Doe")]
        site = self.site(title="Applied Physics Colloquium", series=None, speakers=speakers)
        aggregator = self.aggregator(title=talk, series=None, speakers=speakers)
        for order in ((site, aggregator), (aggregator, site)):
            assert self.merged(*order).event.title == "Applied Physics Colloquium"

    def test_of_titles_that_name_the_speaker_the_one_that_says_more_wins(self) -> None:
        speakers = [Speaker(name="Tadashi Tokieda")]
        talk = "Tadashi Tokieda: Surprises from rubbing the wrong way"
        calendar = self.aggregator(
            title="MRC Public Lecture: Tadashi Tokieda", series=None, speakers=[]
        )
        calendar = calendar.model_copy(
            update={"hints": EventHints(source_ids=["localist-stanford", "sites-mathematics"])}
        )
        host = self.site(title=talk, series="Public Lectures", speakers=speakers)
        for order in ((calendar, host), (host, calendar)):
            assert self.merged(*order).event.title == talk

    def test_when_every_title_names_the_speaker_the_host_title_stays(self) -> None:
        speaker = [Speaker(name="Jane Doe")]
        site = self.site(title="Jane Doe: Protein design", speakers=speaker)
        aggregator = self.aggregator(
            title="Seminar with Jane Doe on protein design", speakers=speaker
        )
        assert self.merged(site, aggregator).event.title == "Jane Doe: Protein design"

    def test_speakers_are_united(self) -> None:
        site = self.site(speakers=[Speaker(name="Jane Doe"), Speaker(name="John Roe")])
        aggregator = self.aggregator(
            speakers=[
                Speaker.model_validate(
                    {
                        "name": "Dr. Jane Doe",
                        "affiliation": "MIT",
                        "url": "https://example.edu/~doe",
                    }
                ),
                Speaker(name="Mary Major"),
            ]
        )
        speakers = self.merged(site, aggregator).event.speakers
        assert [(s.name, s.affiliation) for s in speakers] == [
            ("Jane Doe", "MIT"),
            ("John Roe", None),
            ("Mary Major", None),
        ]
        assert str(speakers[0].url) == "https://example.edu/~doe"

    def test_every_source_key_and_address_is_kept(self) -> None:
        event = self.merged(self.site(), self.aggregator()).event
        assert event.source_ids == ["localist-stanford:agg-1", "sites-bioengineering:site-1"]
        assert [str(url) for url in event.source_urls] == [
            "https://events.example.edu/event/1",
            "https://example.edu/talks/site-1",
        ]

    def test_a_cancellation_from_any_listing_is_kept(self) -> None:
        event = self.merged(self.site(), self.aggregator(status="cancelled")).event
        assert event.status == "cancelled"
        tentative = self.merged(self.site(status="tentative"), self.aggregator()).event
        assert tentative.status == "confirmed"
        both = self.merged(self.site(status="tentative"), self.aggregator(status="tentative"))
        assert both.event.status == "tentative"

    def test_hints_are_united(self) -> None:
        site = self.site()
        site = site.model_copy(
            update={
                "hints": EventHints(source_ids=["sites-bioengineering"], default_fields=["bio"])
            }
        )
        aggregator = self.aggregator()
        aggregator = aggregator.model_copy(
            update={
                "hints": EventHints(
                    source_ids=["localist-stanford"], field_hints=["chem"], categories=["Lecture"]
                )
            }
        )
        hints = self.merged(site, aggregator).hints
        assert hints.source_ids == ["localist-stanford", "sites-bioengineering"]
        assert hints.suggested_fields == ["chem", "bio"]
        assert hints.categories == ["Lecture"]

    def test_bookkeeping_of_stored_events_is_kept(self) -> None:
        site = self.site(sequence=2, missing_runs=1, last_changed=REFERENCE_NOW)
        aggregator = self.aggregator(
            sequence=5, missing_runs=0, last_changed=REFERENCE_NOW - timedelta(days=1)
        )
        event = self.merged(site, aggregator).event
        assert (event.sequence, event.missing_runs, event.last_changed) == (5, 0, REFERENCE_NOW)

    def test_one_candidate_comes_back_unchanged(self) -> None:
        only = make_candidate()
        assert run(only).candidates == (only,)

    def test_nothing_in_nothing_out(self) -> None:
        assert run() == DedupResult()


# --- properties ----------------------------------------------------------------------------

TITLES = [
    "Protein design with diffusion models",
    "Protein Design With Diffusion Models (joint seminar)",
    "Knots, links and three-manifolds",
    "Knots and links",
    "TBA",
    "Seminar",
    "Dark matter searches underground",
]
NAMES = ["Jane Doe", "Dr. Jane Doe", "John Roe", "Jos\u00e9 Garc\u00eda"]
PAGES = [
    "https://example.edu/talks/1",
    "https://example.edu/talks/2",
    "https://nlp.example.edu/seminar",
    "https://events.example.edu/event/3",
]
SOURCES = ["localist-stanford", "sites-a", "sites-b"]
MINUTES = [0, 5, 14, 15, 16, 29, 31, 44, 120]


@st.composite
def similar_candidates(draw: st.DrawFn) -> Candidate:
    """Candidates drawn from small pools, so that duplicates of every kind are common."""
    source = draw(st.sampled_from(SOURCES))
    uid = draw(st.sampled_from(["u1", "u2", "u3", "u4", "u5", "u6", "u7", "u8"]))
    keys = draw(st.lists(st.sampled_from(["k1", "k2", "k3", "k4", "k5"]), min_size=1, max_size=2))
    speakers = draw(st.lists(st.sampled_from(NAMES), max_size=2, unique=True))
    start = START + timedelta(minutes=draw(st.sampled_from(MINUTES)))
    event = make_event(
        uid=uid,
        title=draw(st.sampled_from(TITLES)),
        speakers=[Speaker(name=name) for name in speakers],
        start=start,
        end=draw(st.none() | st.just(start + timedelta(hours=1))),
        url=draw(st.sampled_from(PAGES)),
        source_urls=draw(st.lists(st.sampled_from(PAGES), min_size=1, max_size=2)),
        source_ids=[f"{source}:{key}" for key in keys],
        abstract=draw(st.none() | st.sampled_from(["Short.", "A longer abstract.", "Other text"])),
        location=draw(st.none() | st.sampled_from(["Room 1", "Hall B"])),
        status=draw(st.sampled_from(["confirmed", "tentative", "cancelled"])),
        fields=[],
        first_seen=REFERENCE_NOW - timedelta(days=draw(st.integers(min_value=0, max_value=3))),
        sequence=draw(st.integers(min_value=0, max_value=3)),
    )
    return Candidate(event=event, hints=EventHints(source_ids=[source]))


candidate_lists = st.lists(similar_candidates(), max_size=9)


def arbitrary_candidates() -> st.SearchStrategy[list[Candidate]]:
    """Unrelated events with odd text, to check that nothing breaks on unusual values."""
    return st.lists(events().map(lambda event: Candidate(event=event)), max_size=6)


def all_urls(candidates: tuple[Candidate, ...] | list[Candidate]) -> set[str]:
    return {str(url) for item in candidates for url in item.event.source_urls}


def all_keys(candidates: tuple[Candidate, ...] | list[Candidate]) -> set[str]:
    return {key for item in candidates for key in item.event.source_ids}


class TestProperties:
    @given(candidate_lists)
    @settings(max_examples=300, deadline=None)
    def test_idempotent(self, candidates: list[Candidate]) -> None:
        once = run(*candidates)
        twice = run(*once.candidates)
        assert twice.candidates == once.candidates
        assert twice.absorbed == {}

    @given(candidate_lists, st.randoms(use_true_random=False))
    @settings(max_examples=300, deadline=None)
    def test_independent_of_the_input_order(
        self, candidates: list[Candidate], shuffler: random.Random
    ) -> None:
        shuffled = list(candidates)
        shuffler.shuffle(shuffled)
        assert run(*shuffled) == run(*candidates)
        assert run(*reversed(candidates)) == run(*candidates)

    @given(candidate_lists)
    @settings(max_examples=300, deadline=None)
    def test_no_source_address_and_no_source_key_is_lost(self, candidates: list[Candidate]) -> None:
        result = run(*candidates)
        assert all_urls(result.candidates) == all_urls(candidates)
        assert all_keys(result.candidates) == all_keys(candidates)
        by_uid = {item.event.uid: item.event for item in result.candidates}
        for item in candidates:
            survivor = by_uid[result.absorbed.get(item.event.uid, item.event.uid)]
            assert set(item.event.source_urls) <= set(survivor.source_urls)
            assert set(item.event.source_ids) <= set(survivor.source_ids)
            assert set(item.hints.source_ids) <= set(
                next(c for c in result.candidates if c.event.uid == survivor.uid).hints.source_ids
            )

    @given(candidate_lists)
    @settings(max_examples=200, deadline=None)
    def test_the_result_is_sorted_and_free_of_duplicates(self, candidates: list[Candidate]) -> None:
        result = run(*candidates)
        found = uids(result)
        assert found == sorted(set(found))
        keys = [key for item in result.candidates for key in item.event.source_ids]
        assert len(keys) == len(set(keys))
        assert set(result.absorbed.values()) <= set(found)
        assert not set(result.absorbed) & set(found)
        assert len(result.candidates) <= len(candidates)

    @given(candidate_lists)
    @settings(max_examples=200, deadline=None)
    def test_the_earliest_seen_uid_of_a_group_survives(self, candidates: list[Candidate]) -> None:
        result = run(*candidates)
        for item in result.candidates:
            members = [
                c.event
                for c in candidates
                if result.absorbed.get(c.event.uid, c.event.uid) == item.event.uid
            ]
            assert item.event.first_seen == min(member.first_seen for member in members)

    @given(candidate_lists)
    @settings(max_examples=300, deadline=None)
    def test_one_source_never_merges_listings_with_other_speakers(
        self, candidates: list[Candidate]
    ) -> None:
        own = [
            item.evolve(event=item.event.evolve(uid=f"u{index}"))
            for index, item in enumerate(own_keys(candidates))
        ]
        assert_no_other_speakers_merged(own, run(*own))

    @given(candidate_lists, st.randoms(use_true_random=False))
    @settings(max_examples=300, deadline=None)
    def test_separated_listings_that_shared_a_uid_never_merge_other_speakers(
        self, candidates: list[Candidate], shuffler: random.Random
    ) -> None:
        own = own_keys(candidates)
        separated = separate_listings(own)
        assert separate_listings(separated) == separated
        shuffled = list(own)
        shuffler.shuffle(shuffled)
        assert uid_by_key(separate_listings(shuffled)) == uid_by_key(separated)
        assert_no_other_speakers_merged(separated, run(*separated))

    @given(arbitrary_candidates(), st.randoms(use_true_random=False))
    @settings(max_examples=150, deadline=None)
    def test_arbitrary_events_keep_the_properties(
        self, candidates: list[Candidate], shuffler: random.Random
    ) -> None:
        once = run(*candidates)
        assert run(*once.candidates).candidates == once.candidates
        shuffled = list(candidates)
        shuffler.shuffle(shuffled)
        assert run(*shuffled) == once
        assert all_urls(once.candidates) == all_urls(candidates)


def own_keys(candidates: list[Candidate]) -> list[Candidate]:
    """The candidates with a key each (their uids stay as drawn, so many share one)."""
    return [
        item.evolve(event=item.event.evolve(source_ids=[f"{item.hints.source_ids[0]}:k{index}"]))
        for index, item in enumerate(candidates)
    ]


def uid_by_key(candidates: list[Candidate]) -> dict[str, str]:
    return {key: item.event.uid for item in candidates for key in item.event.source_ids}


def assert_no_other_speakers_merged(listings: list[Candidate], result: DedupResult) -> None:
    """No event of ``result`` holds two listings of one source that name other speakers."""
    groups: dict[str, list[Candidate]] = {}
    for item in listings:
        groups.setdefault(result.absorbed.get(item.event.uid, item.event.uid), []).append(item)
    for members in groups.values():
        for first, second in itertools.combinations(members, 2):
            names = [
                {canonical_name(speaker.name) for speaker in item.event.speakers}
                for item in (first, second)
            ]
            same_source = first.hints.source_ids == second.hints.source_ids
            assert not (same_source and all(names) and names[0] != names[1])
