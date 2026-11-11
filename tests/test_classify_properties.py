"""Properties of the shipped rules on generated input: determinism, order and independence.

The texts are built from the configured terms themselves, mixed with
arbitrary text and punctuation, so that the rules fire often.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from factories import events
from seminars.classify.filters import (
    DEFENSE_PHRASES,
    OUT_OF_SCOPE_DETAIL,
    TALK_MARKERS,
    build_event_filter,
)
from seminars.classify.picks import build_picks_scorer
from seminars.classify.rules import build_field_tagger
from seminars.config import (
    Interests,
    Settings,
    Taxonomy,
    load_interests,
    load_settings,
    load_taxonomy,
)
from seminars.contracts import EventFilter
from seminars.models import FIELD_IDS, Candidate, Event, EventHints, ExcludedEvent

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
TAXONOMY: Final = load_taxonomy(REPO_ROOT / "config" / "taxonomy.yaml")
INTERESTS: Final = load_interests(REPO_ROOT / "config" / "interests.yaml")
SETTINGS: Final = load_settings(REPO_ROOT / "config" / "settings.yaml")
EXAMPLES: Final = settings(max_examples=60, deadline=None)


def _vocabulary(taxonomy: Taxonomy, interests: Interests) -> list[str]:
    terms: set[str] = {*DEFENSE_PHRASES, *TALK_MARKERS, *taxonomy.departments}
    for rules in taxonomy.fields.values():
        terms.update(rules.keywords, rules.series, rules.exclude_keywords)
    for phrases in taxonomy.exclusions.values():
        terms.update(phrases)
    terms.update(interests.keywords, interests.series_allowlist, interests.exclusions)
    return sorted(term.rstrip("*") for term in terms)


VOCABULARY: Final = _vocabulary(TAXONOMY, INTERESTS)
_PIECES: Final = st.one_of(
    st.sampled_from(VOCABULARY),
    st.sampled_from([". ", ", ", ": ", " (", ") ", "-", "/", "\n", "s", "E. "]),
    st.text(max_size=8),
)
_TEXTS: Final = st.lists(_PIECES, max_size=6).map(" ".join)


@st.composite
def candidates(draw: st.DrawFn) -> Candidate:
    """Events with texts made of configured terms, and hints of any source."""
    title = draw(_TEXTS)
    event = draw(events()).evolve(
        title=title if title.strip() else "TBA",
        series=draw(st.none() | _TEXTS),
        abstract=draw(st.none() | _TEXTS),
        fields=[],
    )
    hints = EventHints(
        source_ids=draw(st.lists(st.from_regex(r"[a-z][a-z0-9-]{0,15}", fullmatch=True))),
        default_fields=draw(st.lists(st.sampled_from(FIELD_IDS), max_size=2)),
        field_hints=draw(st.lists(st.sampled_from(FIELD_IDS), max_size=1)),
        categories=draw(st.lists(st.sampled_from(VOCABULARY) | st.text(max_size=10), max_size=3)),
    )
    return Candidate(event=event, hints=hints)


def candidate_lists() -> st.SearchStrategy[list[Candidate]]:
    return st.lists(candidates(), max_size=6, unique_by=lambda item: item.event.uid)


def with_defenses(include: bool) -> Settings:
    return SETTINGS.evolve(events=SETTINGS.events.evolve(include_defenses=include))


TAGGER: Final = build_field_tagger(TAXONOMY)
SCORER: Final = build_picks_scorer(INTERESTS)
FILTERS: Final[dict[bool, EventFilter]] = {
    include: build_event_filter(TAXONOMY, with_defenses(include)) for include in (False, True)
}
"""Built once: the properties compare them with fresh instances where that matters."""


def _is_plain_ascii(text: str) -> bool:
    return all(" " <= char <= "~" for char in text)


# --- field tagging ---------------------------------------------------------------------------


class TestTagger:
    @EXAMPLES
    @given(candidates())
    def test_the_same_candidate_gets_the_same_decision(self, item: Candidate) -> None:
        assert TAGGER.tag(item) == TAGGER.tag(item) == build_field_tagger(TAXONOMY).tag(item)

    @EXAMPLES
    @given(candidates())
    def test_a_decision_is_consistent(self, item: Candidate) -> None:
        decision = TAGGER.tag(item)
        suggested = item.hints.suggested_fields
        assert set(suggested) <= set(decision.fields)
        assert set(decision.fields) <= set(FIELD_IDS)
        if not decision.fields:
            assert decision.needs_llm
            assert decision.method == "rules"
        if suggested:
            assert not decision.needs_llm
        if decision.method == "source":
            assert decision.fields
        assert all(_is_plain_ascii(reason) for reason in decision.reasons)


# --- filtering ---------------------------------------------------------------------------------


def _excluded_by_uid(
    items: Sequence[Candidate], include_defenses: bool
) -> dict[str, ExcludedEvent]:
    event_filter = FILTERS[include_defenses]
    found: dict[str, ExcludedEvent] = {}
    for item in items:
        result = event_filter.filter([item])
        if result.excluded:
            found[item.event.uid] = result.excluded[0]
    return found


def _uncertain_alone(items: Sequence[Candidate], include_defenses: bool) -> list[str]:
    event_filter = FILTERS[include_defenses]
    return sorted(uid for item in items for uid in event_filter.filter([item]).uncertain)


class TestFilter:
    @EXAMPLES
    @given(candidate_lists(), st.booleans())
    def test_the_result_partitions_the_input_in_order(
        self, items: list[Candidate], include_defenses: bool
    ) -> None:
        result = build_event_filter(TAXONOMY, with_defenses(include_defenses)).filter(items)
        assert result == FILTERS[include_defenses].filter(items)
        excluded = _excluded_by_uid(items, include_defenses)
        assert result.kept == [item for item in items if item.event.uid not in excluded]
        assert result.excluded == [excluded[i.event.uid] for i in items if i.event.uid in excluded]
        assert result.uncertain == _uncertain_alone(items, include_defenses)
        assert set(result.uncertain) <= {item.event.uid for item in result.kept}

    @EXAMPLES
    @given(candidate_lists(), st.booleans())
    def test_order_does_not_change_any_decision(
        self, items: list[Candidate], include_defenses: bool
    ) -> None:
        event_filter = FILTERS[include_defenses]
        forward = event_filter.filter(items)
        backward = event_filter.filter(items[::-1])
        assert backward.kept == forward.kept[::-1]
        assert backward.excluded == forward.excluded[::-1]
        assert backward.uncertain == forward.uncertain

    @EXAMPLES
    @given(candidate_lists())
    def test_the_defense_switch_only_moves_defenses(self, items: list[Candidate]) -> None:
        kept_in = _excluded_by_uid(items, include_defenses=True)
        left_out = _excluded_by_uid(items, include_defenses=False)
        assert all(excluded.reason != "defense" for excluded in kept_in.values())
        assert set(kept_in) <= set(left_out)
        moved = {uid for uid in left_out if uid not in kept_in}
        typed = {item.event.uid for item in items if item.event.event_type == "defense"}
        assert {left_out[uid].reason for uid in moved | typed} <= {"defense"}
        assert typed <= set(left_out)

    @EXAMPLES
    @given(st.lists(events(), max_size=6, unique_by=lambda event: event.uid))
    def test_out_of_scope_is_exactly_the_events_without_a_field(self, items: list[Event]) -> None:
        result = FILTERS[SETTINGS.events.include_defenses].drop_out_of_scope(items)
        assert result.kept == [event for event in items if event.fields]
        assert result.excluded == [
            ExcludedEvent.from_event(event, "out_of_scope", OUT_OF_SCOPE_DETAIL)
            for event in items
            if not event.fields
        ]


# --- picks -----------------------------------------------------------------------------------

_POINTS: Final = re.compile(r"\(([+-][0-9.]+)\)$")


class TestPicks:
    @EXAMPLES
    @given(candidates())
    def test_the_same_event_gets_the_same_score(self, item: Candidate) -> None:
        event = item.event
        assert (
            SCORER.score(event) == SCORER.score(event) == build_picks_scorer(INTERESTS).score(event)
        )

    @EXAMPLES
    @given(candidates())
    def test_the_reasons_add_up_to_the_score(self, item: Candidate) -> None:
        result = SCORER.score(item.event)
        points: list[float] = []
        for reason in result.reasons:
            found = _POINTS.search(reason)
            assert found is not None, reason
            points.append(float(found.group(1)))
        assert abs(sum(points) - result.score) < 1e-9
        assert [abs(value) for value in points] == sorted(
            (abs(value) for value in points), reverse=True
        )
        assert len(set(result.reasons)) == len(result.reasons)
        assert all(_is_plain_ascii(reason) for reason in result.reasons)
