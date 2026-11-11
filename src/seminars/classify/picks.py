"""Rule-based picks: how well one event fits config/interests.yaml.

The score of an event is the sum of

* every keyword found, counted once with ``weight * multiplier`` of the best
  place it appears in (title, then series, then abstract);
* ``series_bonus`` once, when a name of ``series_allowlist`` appears in the
  series or the title (titles of calendar-wide sources often carry the
  series name);
* ``speaker_bonus`` once, when a name of ``speaker_watchlist`` appears in a
  speaker's name, or in the title when no speaker name matches;
* minus the weight of every ``exclusions`` term found in title, series or
  abstract.

Terms match like every term of this package (whole words, see
``seminars.classify.rules``); speaker names are matched without plural
forms. The score is rounded to four decimals, and an event is a pick when
it is at least ``threshold``.

Every part that counts is listed in ``PicksResult.reasons``, which the
feeds and the site show to the reader: ``"protein design" in title (+3)``,
``series "Math Colloquium" (+3)``, ``speaker "Jane Doe" (+3)``,
``penalty "undergraduate" (-2)``. They are ordered by how much they count,
largest first, and in the order of the configuration file between equals.
The terms are quoted as the configuration spells them, never as the event
does, so reasons are always plain ASCII text from the configuration.

``interests.llm_rerank`` is reserved for an LLM re-ranking step
(``seminars.contracts.PicksReranker``) that does not exist yet. The rule
score here is the same whatever the switch says.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from seminars.classify.rules import EventTexts, TermMatcher, Words
from seminars.config import Interests
from seminars.contracts import PicksScorer
from seminars.models import Event, PicksResult

SCORE_DECIMALS: Final = 4


@dataclass(frozen=True, slots=True)
class _Part:
    points: float
    reason: str


def _signed(points: float) -> str:
    return f"+{points:g}" if points >= 0 else f"-{-points:g}"


class RulePicksScorer:
    """``PicksScorer`` driven by config/interests.yaml (see the module docstring)."""

    def __init__(self, interests: Interests) -> None:
        self._interests = interests
        self._keywords = TermMatcher(interests.keywords, label="interests keywords")
        self._series = TermMatcher(interests.series_allowlist, label="interests series_allowlist")
        self._speakers = TermMatcher(
            interests.speaker_watchlist, inflect=False, label="interests speaker_watchlist"
        )
        self._penalties = TermMatcher(interests.exclusions, label="interests exclusions")

    @property
    def threshold(self) -> float:
        """An event is a pick when its score is at least this value."""
        return self._interests.threshold

    def _keyword_parts(self, texts: EventTexts) -> list[_Part]:
        multipliers = self._interests.multipliers
        places = (
            ("title", set(self._keywords.matched(texts.title)), multipliers.title),
            ("series", set(self._keywords.matched(texts.series)), multipliers.series),
            ("abstract", set(self._keywords.matched(texts.abstract)), multipliers.abstract),
        )
        parts: list[_Part] = []
        for term in self._keywords.terms:
            for place, found, multiplier in places:
                if term in found:
                    points = self._interests.keywords[term] * multiplier
                    parts.append(_Part(points, f'"{term}" in {place} ({_signed(points)})'))
                    break
        return parts

    def _bonus_parts(self, event: Event, texts: EventTexts) -> list[_Part]:
        parts: list[_Part] = []
        series = self._series.first(texts.series, texts.title)
        bonus = self._interests.series_bonus
        if series is not None and bonus > 0:
            parts.append(_Part(bonus, f'series "{series}" ({_signed(bonus)})'))
        names = [Words.of(speaker.name) for speaker in event.speakers]
        speaker = self._speakers.first(*names) or self._speakers.first(texts.title)
        bonus = self._interests.speaker_bonus
        if speaker is not None and bonus > 0:
            parts.append(_Part(bonus, f'speaker "{speaker}" ({_signed(bonus)})'))
        return parts

    def _penalty_parts(self, texts: EventTexts) -> list[_Part]:
        parts: list[_Part] = []
        for term in self._penalties.matched(texts.title, texts.series, texts.abstract):
            points = -self._interests.exclusions[term]
            parts.append(_Part(points, f'penalty "{term}" ({_signed(points)})'))
        return parts

    def score(self, event: Event) -> PicksResult:
        """Score ``event``; see the module docstring for the parts and the reasons."""
        texts = EventTexts.of(event)
        parts = [
            *self._keyword_parts(texts),
            *self._bonus_parts(event, texts),
            *self._penalty_parts(texts),
        ]
        ranked = sorted(enumerate(parts), key=lambda item: (-abs(item[1].points), item[0]))
        total = round(sum(part.points for part in parts), SCORE_DECIMALS)
        return PicksResult(score=float(total), reasons=[part.reason for _, part in ranked])


def build_picks_scorer(interests: Interests) -> PicksScorer:
    """``PicksScorerFactory``: the rule-based scorer.

    Raises ``ConfigError`` for a term that can never match.
    """
    return RulePicksScorer(interests)


__all__ = ["SCORE_DECIMALS", "RulePicksScorer", "build_picks_scorer"]
