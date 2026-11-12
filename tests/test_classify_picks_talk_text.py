"""Picks read the talk part of an abstract, as the field rules do.

An abstract can hold a section on the speaker ("Bio", "About the Speaker") or on the
series ("About the ... Series"). A word there, such as the name of an award or a seat on
a board, says nothing about the talk, so neither a keyword nor a penalty found only
there counts for the picks score.
"""

from __future__ import annotations

import pytest

from factories import make_event, make_speaker
from seminars.classify.picks import RulePicksScorer
from seminars.config import Interests

INTERESTS = Interests.model_validate(
    {
        "threshold": 3.0,
        "multipliers": {"title": 3.0, "series": 2.0, "abstract": 1.0},
        "keywords": {"protein design": 1.0, "probability": 1.0},
        "speaker_watchlist": ["Jane Doe"],
        "speaker_bonus": 2.0,
        "exclusions": {"high school": 2.0},
    }
)


@pytest.fixture
def scorer() -> RulePicksScorer:
    return RulePicksScorer(INTERESTS)


def score(scorer: RulePicksScorer, abstract: str) -> tuple[float, list[str]]:
    result = scorer.score(make_event(title="Knots", series=None, abstract=abstract, speakers=[]))
    return result.score, result.reasons


@pytest.mark.parametrize(
    "abstract",
    [
        "We study knots.\nBio\nJane Doe won the Protein Design Prize.",
        "We study knots.\nBio: Jane Doe holds a prize in probability.",
        "We study knots.\nAbout the Speaker\nJane Doe works on protein design.",
        "We study knots.\nAbout the Applied Probability Seminar Series\nTalks on probability.",
    ],
)
def test_an_interest_keyword_only_in_a_speaker_or_series_section_scores_nothing(
    scorer: RulePicksScorer, abstract: str
) -> None:
    assert score(scorer, abstract) == (0.0, [])


def test_a_penalty_only_in_a_speaker_section_costs_nothing(scorer: RulePicksScorer) -> None:
    abstract = "We study knots.\nBio\nJane Doe serves on the board of a high school."
    assert score(scorer, abstract) == (0.0, [])


def test_the_same_words_in_the_talk_part_count(scorer: RulePicksScorer) -> None:
    abstract = (
        "Abstract\nProtein design for a high school audience.\nBio\nJane Doe likes probability."
    )
    assert score(scorer, abstract) == (
        -1.0,
        ['penalty "high school" (-2)', '"protein design" in abstract (+1)'],
    )


def test_a_talk_heading_after_the_biography_counts_again(scorer: RulePicksScorer) -> None:
    abstract = "Bio\nJane Doe likes knots.\nAbstract\nWe use protein design."
    assert score(scorer, abstract) == (1.0, ['"protein design" in abstract (+1)'])


def test_a_watched_speaker_still_counts_by_name(scorer: RulePicksScorer) -> None:
    event = make_event(
        title="Knots",
        series=None,
        abstract="Bio\nJane Doe likes knots.",
        speakers=[make_speaker(name="Jane Doe")],
    )
    assert scorer.score(event).reasons == ['speaker "Jane Doe" (+2)']
