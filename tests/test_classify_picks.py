"""The rule-based picks scorer (seminars.classify.picks)."""

from __future__ import annotations

from typing import Any

import pytest

from factories import make_event, make_speaker
from seminars.classify.picks import RulePicksScorer, build_picks_scorer
from seminars.config import ConfigError, Interests
from seminars.contracts import PicksScorer, PicksScorerFactory, load_implementation
from seminars.models import Event, PicksResult

INTERESTS = Interests.model_validate(
    {
        "threshold": 3.0,
        "multipliers": {"title": 3.0, "series": 2.0, "abstract": 1.0},
        "keywords": {"protein design": 1.0, "cryo-EM": 1.0, "topology": 0.5, "Boltz": 1.0},
        "series_allowlist": ["Math Colloquium", "ChEM-H seminars"],
        "series_bonus": 3.0,
        "speaker_watchlist": ["David Baker"],
        "speaker_bonus": 2.0,
        "exclusions": {"undergraduate": 2.0, "high school": 1.0},
    }
)


def event(
    title: str = "TBA",
    *,
    series: str | None = None,
    abstract: str | None = None,
    speakers: list[str] | None = None,
    **overrides: Any,
) -> Event:
    return make_event(
        title=title,
        series=series,
        abstract=abstract,
        speakers=[make_speaker(name=name) for name in speakers or []],
        **overrides,
    )


@pytest.fixture
def scorer() -> RulePicksScorer:
    return RulePicksScorer(INTERESTS)


class TestFactory:
    def test_the_factory_is_registered_and_builds_a_picks_scorer(self) -> None:
        build: PicksScorerFactory = load_implementation("picks_scorer")
        assert build is build_picks_scorer
        scorer = build(INTERESTS)
        assert isinstance(scorer, PicksScorer)
        assert scorer.threshold == 3.0

    def test_no_interests_no_score(self) -> None:
        scorer = build_picks_scorer(Interests())
        assert scorer.score(event("Protein design")) == PicksResult(score=0.0, reasons=[])
        assert scorer.threshold == Interests().threshold

    def test_a_bad_term_is_reported_with_its_place(self) -> None:
        interests = Interests.model_validate({"series_allowlist": ["Math Colloquium", "--"]})
        with pytest.raises(ConfigError, match="interests series_allowlist"):
            build_picks_scorer(interests)


class TestKeywords:
    @pytest.mark.parametrize(
        ("place", "points"), [("title", 3.0), ("series", 2.0), ("abstract", 1.0)]
    )
    def test_the_place_sets_the_multiplier(
        self, scorer: RulePicksScorer, place: str, points: float
    ) -> None:
        texts: dict[str, Any] = {"title": "TBA", "series": None, "abstract": None}
        result = scorer.score(make_event(**{**texts, place: "Generative protein design"}))
        assert result == PicksResult(
            score=points, reasons=[f'"protein design" in {place} (+{points:g})']
        )

    def test_a_keyword_counts_once_at_its_best_place(self, scorer: RulePicksScorer) -> None:
        result = scorer.score(
            event("Protein design", series="Protein design club", abstract="protein designs")
        )
        assert result == PicksResult(score=3.0, reasons=['"protein design" in title (+3)'])

    def test_keywords_add_up_with_their_weights(self, scorer: RulePicksScorer) -> None:
        result = scorer.score(event("Topology of cryo-EM maps", abstract="Protein design."))
        assert result.score == 5.5
        assert result.reasons == [
            '"cryo-EM" in title (+3)',
            '"topology" in title (+1.5)',
            '"protein design" in abstract (+1)',
        ]

    def test_near_misses_score_nothing(self, scorer: RulePicksScorer) -> None:
        assert scorer.score(event("Boltzmann machines and protein redesign")).score == 0.0
        assert scorer.score(event("Boltz-2 for binding affinity")).score == 3.0


class TestBonuses:
    def test_an_allowlisted_series_earns_the_bonus(self, scorer: RulePicksScorer) -> None:
        result = scorer.score(event("TBA", series="Stanford Math Colloquium"))
        assert result == PicksResult(score=3.0, reasons=['series "Math Colloquium" (+3)'])

    def test_the_series_may_be_named_in_the_title(self, scorer: RulePicksScorer) -> None:
        result = scorer.score(event("ChEM-H Seminar: Jane Doe"))
        assert result.reasons == ['series "ChEM-H seminars" (+3)']

    def test_the_series_bonus_is_given_once(self, scorer: RulePicksScorer) -> None:
        result = scorer.score(event("Math Colloquium", series="ChEM-H Seminar"))
        assert result == PicksResult(score=3.0, reasons=['series "Math Colloquium" (+3)'])

    def test_a_watched_speaker_earns_the_bonus(self, scorer: RulePicksScorer) -> None:
        result = scorer.score(event("TBA", speakers=["Jane Doe", "Prof. David Baker"]))
        assert result == PicksResult(score=2.0, reasons=['speaker "David Baker" (+2)'])

    def test_a_watched_speaker_may_be_named_in_the_title(self, scorer: RulePicksScorer) -> None:
        assert scorer.score(event("David Baker: designing proteins")).score == 2.0

    def test_speaker_names_are_not_inflected(self, scorer: RulePicksScorer) -> None:
        assert scorer.score(event("TBA", speakers=["David Bakers"])).score == 0.0

    def test_a_zero_bonus_adds_no_reason(self) -> None:
        interests = INTERESTS.evolve(series_bonus=0.0, speaker_bonus=0.0)
        result = build_picks_scorer(interests).score(
            event("Math Colloquium", speakers=["David Baker"])
        )
        assert result == PicksResult(score=0.0, reasons=[])


class TestPenalties:
    def test_penalties_subtract_wherever_they_match(self, scorer: RulePicksScorer) -> None:
        result = scorer.score(
            event("Protein design", abstract="For undergraduates and high school students.")
        )
        assert result.score == 0.0
        assert result.reasons == [
            '"protein design" in title (+3)',
            'penalty "undergraduate" (-2)',
            'penalty "high school" (-1)',
        ]

    def test_a_score_can_be_negative(self, scorer: RulePicksScorer) -> None:
        result = scorer.score(event("Undergraduate research"))
        assert result == PicksResult(score=-2.0, reasons=['penalty "undergraduate" (-2)'])


class TestResult:
    def test_reasons_are_ordered_by_weight_then_configuration(
        self, scorer: RulePicksScorer
    ) -> None:
        result = scorer.score(
            event(
                "Topology",
                series="Math Colloquium",
                abstract="cryo-EM",
                speakers=["David Baker"],
            )
        )
        assert result.score == 7.5
        assert result.reasons == [
            'series "Math Colloquium" (+3)',
            'speaker "David Baker" (+2)',
            '"topology" in title (+1.5)',
            '"cryo-EM" in abstract (+1)',
        ]

    def test_scores_are_rounded(self) -> None:
        interests = Interests.model_validate(
            {"keywords": {"alpha": 0.1, "beta": 0.2}, "multipliers": {"title": 3.0}}
        )
        result = build_picks_scorer(interests).score(event("alpha beta"))
        assert result.score == 0.9
        assert result.reasons == ['"beta" in title (+0.6)', '"alpha" in title (+0.3)']

    def test_the_threshold_decides_the_pick(self, scorer: RulePicksScorer) -> None:
        scored = event("Protein design")
        result = scorer.score(scored)
        picked = scored.evolve(picks_score=result.score, picks_reasons=result.reasons)
        assert picked.is_pick(scorer.threshold)
        assert not event("Topology").evolve(picks_score=1.5).is_pick(scorer.threshold)

    def test_the_same_event_gives_the_same_result(self, scorer: RulePicksScorer) -> None:
        item = event("Topology of cryo-EM maps", series="Math Colloquium")
        assert scorer.score(item) == scorer.score(item) == RulePicksScorer(INTERESTS).score(item)

    def test_the_llm_rerank_switch_does_not_change_rule_scores(self) -> None:
        item = event("Topology of cryo-EM maps", series="Math Colloquium")
        assert Interests().llm_rerank is False
        rerank = build_picks_scorer(INTERESTS.evolve(llm_rerank=True)).score(item)
        assert rerank == build_picks_scorer(INTERESTS).score(item)

    def test_the_event_is_not_changed(self, scorer: RulePicksScorer) -> None:
        item = event("Protein design")
        before = item.model_dump()
        scorer.score(item)
        assert item.model_dump() == before
