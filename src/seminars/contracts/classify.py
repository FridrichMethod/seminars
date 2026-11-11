"""Contracts of rule-based classification: field tagger, event filter and picks."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from seminars.config import Interests, Settings, Taxonomy
from seminars.models import (
    Candidate,
    Event,
    FilterResult,
    PicksResult,
    ScopeResult,
    TagDecision,
)


@runtime_checkable
class FieldTagger(Protocol):
    """Rule-based field tagging: source defaults first, then keyword and series rules."""

    def tag(self, candidate: Candidate) -> TagDecision:
        """Decide the fields of one candidate; set ``needs_llm`` when the rules cannot."""
        ...


class FieldTaggerFactory(Protocol):
    """``seminars.classify.rules:build_field_tagger``."""

    def __call__(self, taxonomy: Taxonomy) -> FieldTagger: ...


@runtime_checkable
class EventFilter(Protocol):
    """Removes what is not an academic talk, and later what is out of scope."""

    def filter(self, candidates: Sequence[Candidate]) -> FilterResult:
        """Apply the exclusion rules (courses, office hours, ...) before de-duplication.

        This is also where the PhD defense switch lives: when
        ``settings.events.include_defenses`` is false, every candidate whose
        ``event.event_type`` is ``"defense"`` is excluded with reason
        ``"defense"``. No other stage looks at that setting. The event type
        itself is decided earlier, by normalization, from the draft's
        ``event_type_hint`` or the source's ``default_event_type``.
        """
        ...

    def drop_out_of_scope(self, events: Sequence[Event]) -> ScopeResult:
        """After classification: exclude events without any field (``out_of_scope``)."""
        ...


class EventFilterFactory(Protocol):
    """``seminars.classify.filters:build_event_filter``.

    ``taxonomy.exclusions`` supplies the phrases of the rule reasons;
    ``settings.events.include_defenses`` is the defense switch (see
    ``EventFilter.filter``).
    """

    def __call__(self, taxonomy: Taxonomy, settings: Settings) -> EventFilter: ...


@runtime_checkable
class PicksScorer(Protocol):
    """Scores events against config/interests.yaml."""

    @property
    def threshold(self) -> float:
        """An event is a pick when its score is at least this value."""
        ...

    def score(self, event: Event) -> PicksResult: ...


class PicksScorerFactory(Protocol):
    """``seminars.classify.picks:build_picks_scorer``."""

    def __call__(self, interests: Interests) -> PicksScorer: ...


@runtime_checkable
class PicksReranker(Protocol):
    """Reserved for ``interests.llm_rerank`` (off by default, not implemented yet)."""

    def rerank(self, events: Sequence[Event]) -> dict[str, PicksResult]:
        """New scores keyed by event uid; events left out keep their rule-based score."""
        ...
