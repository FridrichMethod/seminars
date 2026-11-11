"""Tests for de-duplication: the pair rules, the merge rules and the properties."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from factories import PACIFIC, REFERENCE_NOW, events, make_candidate, make_event
from seminars.config import DedupSettings
from seminars.models import Candidate, EventHints, Speaker
from seminars.pipeline.dedup import DedupResult, dedupe

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

    def test_the_same_detail_page_is_enough(self) -> None:
        first = candidate("a", url="https://example.edu/talks/7/")
        second = candidate("b", url="HTTPS://EXAMPLE.edu/talks/7#abstract")
        assert uids(run(first, second)) == ["a"]

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
