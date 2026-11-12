"""Rule-based event filter: what is not an academic talk, and what is out of scope.

``EventFilter.filter`` runs before de-duplication and decides, per candidate:

1. The PhD defense switch. When ``settings.events.include_defenses`` is
   false, a candidate is excluded with reason ``defense`` when its event
   type is ``defense``, and also when its title or series names one
   (DEFENSE_PHRASES, for sources that do not set the type). When the
   switch is on, defenses are ordinary talks.
2. The categories that are always out of scope
   (``taxonomy.out_of_scope_categories``). A candidate with one of these
   labels among ``hints.categories`` (equal words after folding, like
   ``taxonomy.departments``; never a tag, ``hints.tags``) is excluded with
   reason ``out_of_scope``, whatever its title, other categories or source
   fields say. This
   happens here rather than after classification so that such an event
   is never merged with another one, never sent to the LLM tagger, and
   the run report names the category that decided.
3. The exclusion phrases of ``taxonomy.exclusions``, one list per reason,
   matched like every term of this package (whole words, see
   ``seminars.classify.rules``). The reasons are tried in the order of
   ``RuleExclusionReason`` and the phrases in the order of the file; the
   first match decides. A phrase in the title or the series excludes the
   candidate, unless the title or the series also names a talk
   (TALK_MARKERS, outside the matched phrases): "Career Development
   Seminar" or "Colloquium and Welcome Reception" are kept and marked
   uncertain. A phrase that names a talk itself ("no seminar", "career
   talk") was written for such titles and always excludes; these phrases
   are tried first. A phrase that only matches one of the source's category
   labels or tags (``hints.labels``) marks the candidate uncertain as well.
   Uncertain candidates stay in unless the LLM tagger answers that they are
   not talks.
4. Everything else is kept. The abstract is never read: announcements of
   real talks routinely mention office hours or a reception afterwards.
   A cancelled talk is a talk: normalization has already taken the word
   off its title ("CANCELED: ...") and set its status to ``cancelled``, so
   it stays and the feeds show it as cancelled.

``EventFilter.drop_out_of_scope`` runs after classification and excludes
every event that still has no field, with reason ``out_of_scope`` as well.

Both keep the order of their input, so the result is the same however
often it is computed, and the decision about one event never depends on
the others.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, get_args

from seminars.classify.rules import TermMatcher, Words, label_key
from seminars.config import ConfigError, Settings, Taxonomy
from seminars.contracts import EventFilter
from seminars.models import (
    Candidate,
    Event,
    ExcludedEvent,
    ExclusionReason,
    FilterResult,
    RuleExclusionReason,
    ScopeResult,
)

RULE_REASONS: Final[tuple[RuleExclusionReason, ...]] = get_args(RuleExclusionReason)
"""The rule reasons in the order they are tried."""

TALK_MARKERS: Final[tuple[str, ...]] = (
    "seminar",
    "colloquium",
    "colloquia",
    "lecture",
    "talk",
    "symposium",
    "keynote",
    "grand rounds",
)
"""Words that name an academic talk; a title with one of them is only excluded by a phrase
that contains one itself ("no seminar").

"workshop", "webinar" and "panel" are not among them: career workshops and
admission webinars are what the phrases are for.
"""

DEFENSE_PHRASES: Final[tuple[str, ...]] = (
    "thesis defense",
    "thesis defence",
    "dissertation defense",
    "dissertation defence",
    "PhD defense",
    "PhD defence",
    "doctoral defense",
    "doctoral defence",
    "oral defense",
    "oral defence",
    "university oral examination",
    "university oral exam",
    "PhD oral examination",
    "PhD oral exam",
)
"""Titles of PhD defenses, for sources that do not mark them with event type ``defense``."""

DEFENSE_TYPE_DETAIL: Final = "event type defense"
OUT_OF_SCOPE_DETAIL: Final = "no field after classification"
OUT_OF_SCOPE_CATEGORY_DETAIL: Final = 'category "{label}" is out of scope'
"""Detail of an exclusion by ``taxonomy.out_of_scope_categories``; ``{label}`` as configured."""


@dataclass(frozen=True, slots=True)
class _Verdict:
    reason: ExclusionReason | None = None
    detail: str | None = None
    uncertain: bool = False


_KEEP: Final = _Verdict()
_UNCERTAIN: Final = _Verdict(uncertain=True)

_Phrases = tuple[tuple[RuleExclusionReason, TermMatcher], ...]


def _category_labels(labels: Sequence[str], *, place: str) -> dict[str, str]:
    """``label_key`` -> the label as configured, in configuration order.

    Raises ``ConfigError`` for a label without letters or digits: it would
    match every category label that has none either.
    """
    keys: dict[str, str] = {}
    for label in labels:
        key = label_key(label)
        if not key:
            raise ConfigError(f"{place}: label {label!r} has no letters or digits")
        keys.setdefault(key, label)
    return keys


def _first_match(phrases: _Phrases, texts: dict[str, Words]) -> _Verdict | None:
    """The first phrase found, by reason, then place, then the order of the file."""
    for reason, matcher in phrases:
        for place, words in texts.items():
            phrase = matcher.first(words)
            if phrase is not None:
                return _Verdict(reason, f'{place} matches "{phrase}"')
    return None


class RuleEventFilter:
    """``EventFilter`` driven by the taxonomy and the defense switch.

    The taxonomy supplies ``exclusions`` and ``out_of_scope_categories``.
    """

    def __init__(self, taxonomy: Taxonomy, settings: Settings) -> None:
        self._include_defenses = settings.events.include_defenses
        self._phrases: _Phrases = tuple(
            (
                reason,
                TermMatcher(
                    taxonomy.exclusions.get(reason, []), label=f"taxonomy exclusions.{reason}"
                ),
            )
            for reason in RULE_REASONS
        )
        self._markers = TermMatcher(TALK_MARKERS, label="talk markers")
        self._defenses = TermMatcher(DEFENSE_PHRASES, label="defense phrases")
        self._final: _Phrases = tuple(
            (reason, TermMatcher(self._naming_a_talk(matcher.terms)))
            for reason, matcher in self._phrases
        )
        self._out_of_scope = _category_labels(
            taxonomy.out_of_scope_categories, place="taxonomy out_of_scope_categories"
        )

    def _naming_a_talk(self, phrases: Sequence[str]) -> list[str]:
        """The phrases that contain a talk marker themselves ("no seminar")."""
        return [phrase for phrase in phrases if self._markers.first(Words.of(phrase.rstrip("*")))]

    def _defense(self, event: Event, texts: dict[str, Words]) -> _Verdict | None:
        if self._include_defenses:
            return None
        if event.event_type == "defense":
            return _Verdict("defense", DEFENSE_TYPE_DETAIL)
        for place, words in texts.items():
            phrase = self._defenses.first(words)
            if phrase is not None:
                return _Verdict("defense", f'{place} matches "{phrase}"')
        return None

    def _out_of_scope_category(self, candidate: Candidate) -> _Verdict | None:
        """The verdict for a candidate of a category that is always out of scope."""
        carried = {label_key(category) for category in candidate.hints.categories}
        for key, label in self._out_of_scope.items():
            if key in carried:
                return _Verdict("out_of_scope", OUT_OF_SCOPE_CATEGORY_DETAIL.format(label=label))
        return None

    def _names_a_talk(self, words: Words) -> bool:
        """True when ``words`` hold a talk marker that is not part of an exclusion phrase."""
        phrases = [hit for _, matcher in self._phrases for hit in matcher.hits(words)]
        return any(
            not any(marker.overlaps(hit) for hit in phrases) for marker in self._markers.hits(words)
        )

    def _judge(self, candidate: Candidate) -> _Verdict:
        event = candidate.event
        texts = {"title": Words.of(event.title), "series": Words.of(event.series)}
        defense = self._defense(event, texts)
        if defense is not None:
            return defense
        out_of_scope = self._out_of_scope_category(candidate)
        if out_of_scope is not None:
            return out_of_scope
        final = _first_match(self._final, texts)
        if final is not None:
            return final
        verdict = _first_match(self._phrases, texts)
        if verdict is not None:
            names_a_talk = any(self._names_a_talk(text) for text in texts.values())
            return _UNCERTAIN if names_a_talk else verdict
        labels = [Words.of(label) for label in candidate.hints.labels]
        if any(matcher.first(*labels) for _, matcher in self._phrases):
            return _UNCERTAIN
        return _KEEP

    def filter(self, candidates: Sequence[Candidate]) -> FilterResult:
        """Exclude by the defense switch, by category and by rule; see the module docstring."""
        kept: list[Candidate] = []
        excluded: list[ExcludedEvent] = []
        uncertain: list[str] = []
        for candidate in candidates:
            verdict = self._judge(candidate)
            if verdict.reason is not None:
                excluded.append(
                    ExcludedEvent.from_event(candidate.event, verdict.reason, verdict.detail)
                )
                continue
            kept.append(candidate)
            if verdict.uncertain:
                uncertain.append(candidate.event.uid)
        return FilterResult(kept=kept, excluded=excluded, uncertain=uncertain)

    def drop_out_of_scope(self, events: Sequence[Event]) -> ScopeResult:
        """Exclude every event without a field, with reason ``out_of_scope``."""
        return ScopeResult(
            kept=[event for event in events if event.fields],
            excluded=[
                ExcludedEvent.from_event(event, "out_of_scope", OUT_OF_SCOPE_DETAIL)
                for event in events
                if not event.fields
            ],
        )


def build_event_filter(taxonomy: Taxonomy, settings: Settings) -> EventFilter:
    """``EventFilterFactory``: the rule-based filter.

    Raises ``ConfigError`` for an exclusion phrase that can never match and
    for an out-of-scope category label without letters or digits.
    """
    return RuleEventFilter(taxonomy, settings)


__all__ = [
    "DEFENSE_PHRASES",
    "DEFENSE_TYPE_DETAIL",
    "OUT_OF_SCOPE_CATEGORY_DETAIL",
    "OUT_OF_SCOPE_DETAIL",
    "RULE_REASONS",
    "TALK_MARKERS",
    "RuleEventFilter",
    "build_event_filter",
]
