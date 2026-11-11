"""Values exchanged between the pipeline and the classification stages."""

from __future__ import annotations

from typing import Literal, Self

from seminars.models.base import FieldList, Model, SortedStrList, Timestamp, canonical_fields
from seminars.models.event import Event

RuleExclusionReason = Literal[
    "course",  # regular class sessions
    "office_hours",
    "admissions",  # admissions and information sessions
    "career",  # career development, recruiting
    "social",  # receptions, mixers, parties
    "fitness",
    "religious",
    "administrative",  # committee and staff meetings
    "academic_calendar",  # deadlines, billing dates, holidays
]
"""Exclusion reasons the rule-based filter can assign (SPEC section 5.5)."""

ExclusionReason = Literal[
    RuleExclusionReason,
    "not_a_talk",  # the LLM judged an ambiguous item not to be an academic talk
    "out_of_scope",  # no field matched after classification
    "invalid",  # the draft could not be normalized (for example no usable start)
]
"""Why an event was left out; written to the run report."""

RuleTagMethod = Literal["source", "rules"]


class EventHints(Model):
    """Side information that travels with an event until it is classified.

    Built by normalization from the source configuration and the draft, and
    merged when events from several sources are merged.
    """

    source_ids: SortedStrList = []  # ids of the SourceConfig entries that contributed
    default_fields: FieldList = []  # union of the sources' default_fields
    field_hints: FieldList = []  # union of the drafts' field_hints
    categories: SortedStrList = []  # union of the drafts' free-text categories

    @property
    def suggested_fields(self) -> list[str]:
        """``default_fields`` and ``field_hints`` combined, in canonical order."""
        return canonical_fields([*self.default_fields, *self.field_hints])

    def merge(self, other: EventHints) -> Self:
        """Union of two hint sets; commutative, associative and idempotent."""
        return type(self)(
            source_ids=[*self.source_ids, *other.source_ids],
            default_fields=[*self.default_fields, *other.default_fields],
            field_hints=[*self.field_hints, *other.field_hints],
            categories=[*self.categories, *other.categories],
        )


class Candidate(Model):
    """A normalized event plus its hints; the unit of filtering, merging and tagging."""

    event: Event
    hints: EventHints = EventHints()


class TagDecision(Model):
    """Result of the rule-based field tagger for one candidate.

    ``needs_llm`` is true when the rules found nothing or found conflicting
    evidence; the pipeline then asks the LLM tagger if one is available and
    otherwise keeps ``fields`` as they are.
    """

    fields: FieldList = []
    method: RuleTagMethod
    needs_llm: bool = False
    reasons: list[str] = []  # which defaults or rules matched, for the run report


class LLMTagResult(Model):
    """Answer of the LLM tagger for one event.

    ``is_talk`` is false when the item is not an academic talk at all; the
    pipeline then excludes it with reason ``not_a_talk``.
    """

    fields: FieldList = []
    is_talk: bool = True


class ExcludedEvent(Model):
    """One event that was left out of every output, with the reason."""

    source_ids: SortedStrList = []
    title: str
    start: Timestamp | None = None
    url: str | None = None
    reason: ExclusionReason
    detail: str | None = None  # the rule or keyword that decided, when there is one

    @classmethod
    def from_event(cls, event: Event, reason: ExclusionReason, detail: str | None = None) -> Self:
        """Describe ``event`` as excluded for ``reason``."""
        return cls(
            source_ids=event.source_ids,
            title=event.title,
            start=event.start,
            url=str(event.url),
            reason=reason,
            detail=detail,
        )


class FilterResult(Model):
    """Outcome of the rule-based filter (SPEC section 5.5).

    ``uncertain`` lists uids of kept candidates the rules could not decide.
    They stay in unless the LLM tagger answers ``is_talk=False`` for them.
    """

    kept: list[Candidate] = []
    excluded: list[ExcludedEvent] = []
    uncertain: SortedStrList = []


class ScopeResult(Model):
    """Outcome of the out-of-scope check that runs after classification."""

    kept: list[Event] = []
    excluded: list[ExcludedEvent] = []


class PicksResult(Model):
    """Score of one event against config/interests.yaml."""

    score: float = 0.0
    reasons: list[str] = []  # human-readable matches, most important first
