"""The rule-based event filter (seminars.classify.filters)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

import pytest

from factories import make_candidate, make_event
from seminars.classify.filters import (
    DEFENSE_PHRASES,
    OUT_OF_SCOPE_DETAIL,
    TALK_MARKERS,
    RuleEventFilter,
    build_event_filter,
)
from seminars.config import ConfigError, EventSettings, Settings, Taxonomy, load_settings
from seminars.contracts import EventFilter, EventFilterFactory, load_implementation
from seminars.models import Candidate, ExcludedEvent, RuleExclusionReason

REPO_ROOT = Path(__file__).resolve().parents[1]

PHRASES: dict[str, list[str]] = {
    "course": ["problem session", "review session"],
    "office_hours": ["office hours"],
    "admissions": ["info session"],
    "career": ["career development", "career talk"],
    "social": ["reception", "holiday party"],
    "fitness": ["yoga"],
    "religious": ["bible study"],
    "administrative": ["faculty meeting"],
    "academic_calendar": ["tuition due"],
}
TAXONOMY = Taxonomy.model_validate({"exclusions": PHRASES})


def settings(*, include_defenses: bool = True) -> Settings:
    base = load_settings(REPO_ROOT / "config" / "settings.yaml")
    return base.evolve(events=EventSettings(include_defenses=include_defenses))


def candidate(
    title: str,
    *,
    uid: str = "e1",
    series: str | None = None,
    abstract: str | None = None,
    event_type: str = "seminar",
    **hints: Any,
) -> Candidate:
    event = make_event(
        uid=uid,
        title=title,
        series=series,
        abstract=abstract,
        event_type=event_type,
        fields=[],
        field_tag_method="source",
    )
    return make_candidate(event, **hints)


@pytest.fixture
def event_filter() -> RuleEventFilter:
    return RuleEventFilter(TAXONOMY, settings())


def outcome(event_filter: EventFilter, item: Candidate) -> tuple[str, str | None]:
    """("kept" | "uncertain" | the exclusion reason, the detail)."""
    result = event_filter.filter([item])
    if result.excluded:
        (excluded,) = result.excluded
        return excluded.reason, excluded.detail
    assert [kept.event.uid for kept in result.kept] == [item.event.uid]
    return ("uncertain" if result.uncertain else "kept"), None


class TestFactory:
    def test_the_factory_is_registered_and_builds_an_event_filter(self) -> None:
        build: EventFilterFactory = load_implementation("event_filter")
        assert build is build_event_filter
        assert isinstance(build(TAXONOMY, settings()), EventFilter)

    def test_every_rule_reason_has_phrases_in_this_test(self) -> None:
        assert set(PHRASES) == set(get_args(RuleExclusionReason))

    def test_a_bad_phrase_is_reported_with_its_place(self) -> None:
        taxonomy = Taxonomy.model_validate({"exclusions": {"office_hours": ["office hours", "!"]}})
        with pytest.raises(ConfigError, match=r"taxonomy exclusions\.office_hours"):
            build_event_filter(taxonomy, settings())

    def test_an_empty_taxonomy_keeps_everything(self) -> None:
        empty = build_event_filter(Taxonomy(), settings())
        assert outcome(empty, candidate("Office hours")) == ("kept", None)


class TestRuleReasons:
    @pytest.mark.parametrize(
        ("title", "reason", "phrase"),
        [
            ("CS 229 Problem Session", "course", "problem session"),
            ("Office Hours with the Chair", "office_hours", "office hours"),
            ("PhD Program Info Session", "admissions", "info session"),
            ("Career Development for Postdocs", "career", "career development"),
            ("Welcome Reception", "social", "reception"),
            ("Lunchtime Yoga", "fitness", "yoga"),
            ("Graduate Bible Study", "religious", "bible study"),
            ("Faculty Meeting (closed)", "administrative", "faculty meeting"),
            ("Winter Quarter Tuition Due", "academic_calendar", "tuition due"),
        ],
    )
    def test_each_reason_excludes_by_title(
        self, event_filter: RuleEventFilter, title: str, reason: str, phrase: str
    ) -> None:
        assert outcome(event_filter, candidate(title)) == (reason, f'title matches "{phrase}"')

    def test_the_series_counts_like_the_title(self, event_filter: RuleEventFilter) -> None:
        item = candidate("Week 3", series="Department Holiday Party")
        assert outcome(event_filter, item) == ("social", 'series matches "holiday party"')

    def test_the_abstract_never_excludes(self, event_filter: RuleEventFilter) -> None:
        item = candidate("Enzyme design", abstract="Office hours and a reception follow.")
        assert outcome(event_filter, item) == ("kept", None)

    def test_the_earliest_reason_wins(self, event_filter: RuleEventFilter) -> None:
        item = candidate("Holiday party and info session")
        assert outcome(event_filter, item) == ("admissions", 'title matches "info session"')

    def test_within_a_reason_the_first_listed_phrase_is_reported(
        self, event_filter: RuleEventFilter
    ) -> None:
        item = candidate("Holiday party reception")
        assert outcome(event_filter, item) == ("social", 'title matches "reception"')

    @pytest.mark.parametrize(
        "title",
        [
            "Receptor tyrosine kinases",
            "Officer training in the navy",
            "Problems in session types",
            "Hours of the office",
        ],
    )
    def test_near_misses_are_kept(self, event_filter: RuleEventFilter, title: str) -> None:
        assert outcome(event_filter, candidate(title)) == ("kept", None)


class TestUncertain:
    @pytest.mark.parametrize(
        "title",
        [
            "Career Development Seminar: Jane Doe",
            "Physics Colloquium and Reception",
            "Lecture: the neuroscience of yoga",
            "Keynote at the faculty meeting",
        ],
    )
    def test_a_talk_marker_outside_the_phrase_makes_a_hit_uncertain(
        self, event_filter: RuleEventFilter, title: str
    ) -> None:
        assert outcome(event_filter, candidate(title)) == ("uncertain", None)

    def test_a_talk_marker_in_the_series_counts(self, event_filter: RuleEventFilter) -> None:
        item = candidate("Reception", series="Chemistry Seminar")
        assert outcome(event_filter, item) == ("uncertain", None)

    def test_a_talk_marker_inside_the_phrase_does_not_count(
        self, event_filter: RuleEventFilter
    ) -> None:
        assert outcome(event_filter, candidate("Career talk with alumni")) == (
            "career",
            'title matches "career talk"',
        )

    def test_a_phrase_in_a_category_only_is_uncertain(self, event_filter: RuleEventFilter) -> None:
        item = candidate("Dark matter at SLAC", categories=["Social Event / Reception"])
        assert outcome(event_filter, item) == ("uncertain", None)

    def test_categories_without_a_phrase_change_nothing(
        self, event_filter: RuleEventFilter
    ) -> None:
        item = candidate("Dark matter at SLAC", categories=["Lecture / Presentation"])
        assert outcome(event_filter, item) == ("kept", None)

    def test_the_markers_are_the_documented_ones(self) -> None:
        assert {"seminar", "colloquium", "lecture", "talk", "symposium"} <= set(TALK_MARKERS)
        assert "workshop" not in TALK_MARKERS
        assert "webinar" not in TALK_MARKERS


class TestDefenseSwitch:
    def test_defenses_are_kept_when_the_switch_is_on(self, event_filter: RuleEventFilter) -> None:
        assert outcome(event_filter, candidate("Robust control", event_type="defense")) == (
            "kept",
            None,
        )
        assert outcome(event_filter, candidate("PhD Thesis Defense: Robust control")) == (
            "kept",
            None,
        )

    def test_defense_events_are_excluded_when_the_switch_is_off(self) -> None:
        off = build_event_filter(TAXONOMY, settings(include_defenses=False))
        assert outcome(off, candidate("Robust control", event_type="defense")) == (
            "defense",
            "event type defense",
        )

    @pytest.mark.parametrize(
        ("title", "phrase"),
        [
            ("PhD Thesis Defense: Robust control", "thesis defense"),
            ("Dissertation Defence - Jane Doe", "dissertation defence"),
            ("University Oral Examination: Jane Doe", "university oral examination"),
            ("PhD Oral Defense", "oral defense"),
        ],
    )
    def test_defense_titles_are_excluded_when_the_switch_is_off(
        self, title: str, phrase: str
    ) -> None:
        off = build_event_filter(TAXONOMY, settings(include_defenses=False))
        assert outcome(off, candidate(title)) == ("defense", f'title matches "{phrase}"')

    def test_other_defenses_are_not_phd_defenses(self) -> None:
        off = build_event_filter(TAXONOMY, settings(include_defenses=False))
        assert outcome(off, candidate("Plant defense against pathogens")) == ("kept", None)
        assert outcome(off, candidate("Seminar on national defense")) == ("kept", None)

    def test_the_switch_comes_before_the_rule_reasons(self) -> None:
        off = build_event_filter(TAXONOMY, settings(include_defenses=False))
        item = candidate("Thesis defense and reception")
        assert outcome(off, item)[0] == "defense"

    def test_defense_phrases_cover_the_usual_titles(self) -> None:
        assert {"thesis defense", "dissertation defense", "PhD defense"} <= set(DEFENSE_PHRASES)


class TestResult:
    def test_order_and_partition(self, event_filter: RuleEventFilter) -> None:
        items = [
            candidate("Reception", uid="c"),
            candidate("Enzyme design", uid="b"),
            candidate("Office hours", uid="a"),
            candidate("Reception after the seminar", uid="z"),
            candidate("Topology", uid="y"),
        ]
        result = event_filter.filter(items)
        assert [item.event.uid for item in result.kept] == ["b", "z", "y"]
        assert [(item.source_ids, item.reason) for item in result.excluded] == [
            (["sites-bioengineering:1234"], "social"),
            (["sites-bioengineering:1234"], "office_hours"),
        ]
        assert [item.title for item in result.excluded] == ["Reception", "Office hours"]
        assert result.uncertain == ["z"]
        assert result.kept[0] is items[1]

    def test_excluded_events_describe_the_event(self, event_filter: RuleEventFilter) -> None:
        item = candidate("Office hours")
        (excluded,) = event_filter.filter([item]).excluded
        assert excluded == ExcludedEvent.from_event(
            item.event, "office_hours", 'title matches "office hours"'
        )

    def test_no_candidates(self, event_filter: RuleEventFilter) -> None:
        result = event_filter.filter([])
        assert (result.kept, result.excluded, result.uncertain) == ([], [], [])

    def test_order_of_the_input_does_not_change_any_decision(
        self, event_filter: RuleEventFilter
    ) -> None:
        titles = ["Reception", "Enzymes", "Office hours", "Seminar and reception", "Yoga"]
        items = [candidate(title, uid=f"u{n}") for n, title in enumerate(titles)]
        forward, backward = event_filter.filter(items), event_filter.filter(items[::-1])
        assert [c.event.uid for c in forward.kept] == [c.event.uid for c in backward.kept][::-1]
        assert forward.excluded == backward.excluded[::-1]
        assert forward.uncertain == backward.uncertain
        assert event_filter.filter(items) == forward


class TestOutOfScope:
    def test_events_without_fields_are_out_of_scope(self, event_filter: RuleEventFilter) -> None:
        events = [
            make_event(uid="a", fields=["bio"]),
            make_event(uid="b", title="Poetry and empire", fields=[]),
            make_event(uid="c", fields=["math", "physics"]),
            make_event(uid="d", title="Opera night", fields=[]),
        ]
        result = event_filter.drop_out_of_scope(events)
        assert [event.uid for event in result.kept] == ["a", "c"]
        assert result.excluded == [
            ExcludedEvent.from_event(events[1], "out_of_scope", OUT_OF_SCOPE_DETAIL),
            ExcludedEvent.from_event(events[3], "out_of_scope", OUT_OF_SCOPE_DETAIL),
        ]

    def test_nothing_to_check(self, event_filter: RuleEventFilter) -> None:
        result = event_filter.drop_out_of_scope([])
        assert (result.kept, result.excluded) == ([], [])

    def test_out_of_scope_ignores_the_exclusion_phrases(
        self, event_filter: RuleEventFilter
    ) -> None:
        event = make_event(title="Office hours", fields=["cs"])
        assert event_filter.drop_out_of_scope([event]).kept == [event]
