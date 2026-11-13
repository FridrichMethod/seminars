"""Time series in config/taxonomy.yaml: topic phrases decide, the bare words need a partner.

"Time series" is a method that talks of every field use ("a time series of calcium
imaging", "EHR vital-sign time series"). On a department source the abstract counts
like the title, so the bare words alone would add math to such a talk. They weigh 0.5
and decide together with a second term; the phrases of the topic weigh 1.0. So does
"statistical model", which talks of every field use as well: it has no weight of its
own beyond that of "statistical".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from factories import make_candidate, make_event
from seminars.classify.rules import build_field_tagger
from seminars.config import Taxonomy, load_taxonomy
from seminars.contracts import FieldTagger
from seminars.models import Candidate

REPO_ROOT = Path(__file__).resolve().parents[1]

RAIN_TITLE = "Some Results on Time Series"
RAIN_TALK = (
    "In this talk, I will describe a research program on time series that I have been"
    " pursuing with various collaborators over the past decade. I will start by recalling"
    " the literature on time series. I will discuss a representation for time series data"
    " through the lens of low-rank matrices and its implications for robust, scalable"
    " forecasting, imputation methods. Finally, I will touch upon ongoing project with the"
    " goal of developing efficient time series foundation model with synthetic data"
    " inspired by the traditional statistical models."
)
"""The talk of the RAIN/OR Seminar recorded in tests/fixtures/custom-rain-seminar."""


@pytest.fixture(scope="module")
def taxonomy() -> Taxonomy:
    return load_taxonomy(REPO_ROOT / "config" / "taxonomy.yaml")


@pytest.fixture(scope="module")
def tagger(taxonomy: Taxonomy) -> FieldTagger:
    return build_field_tagger(taxonomy)


def candidate(title: str, abstract: str | None, source: str, fields: list[str]) -> Candidate:
    event = make_event(
        title=title, abstract=abstract, series=None, fields=[], field_tag_method="source"
    )
    hints: dict[str, Any] = {"source_ids": [source], "default_fields": fields}
    return make_candidate(event, **hints)


TOPIC_PHRASES = (
    "time series analysis",
    "time series forecasting",
    "time series model*",
    "time series foundation model*",
)


def test_the_bare_words_are_weak_and_the_phrases_strong(taxonomy: Taxonomy) -> None:
    keywords = taxonomy.fields["math"].keywords
    assert keywords["time series"] == 0.5
    for phrase in TOPIC_PHRASES:
        assert keywords[phrase] == 1.0, phrase


def test_the_rain_talk_keeps_math_through_its_topic_phrase(tagger: FieldTagger) -> None:
    decision = tagger.tag(candidate(RAIN_TITLE, RAIN_TALK, "custom-rain-seminar", ["cs", "ai-ml"]))
    assert decision.fields == ["math", "cs", "ai-ml"], decision.reasons
    assert any("time series foundation model*" in reason for reason in decision.reasons)


@pytest.mark.parametrize(
    "abstract",
    [
        "We build statistical models of gene expression and use Bayesian methods.",
        "Statistical modeling of single-cell data, with statistics for the masses.",
        "We use statistical modelling and optimization of dosing.",
        "A statistical model of probability of relapse.",
        "We fit statistical models with Bayesian inference to single-cell data.",
    ],
)
def test_a_statistical_model_and_a_method_word_add_no_math(
    tagger: FieldTagger, abstract: str
) -> None:
    talk = candidate("Neural circuits of memory", abstract, "sites-biology", ["bio"])
    decision = tagger.tag(talk)
    assert decision.fields == ["bio"], decision.reasons


@pytest.mark.parametrize(
    "abstract",
    [
        "We record a time series of calcium imaging in the mouse cortex.",
        "We predict sepsis from EHR vital-sign time series in the ICU.",
    ],
)
def test_a_passing_mention_in_an_abstract_adds_no_math(tagger: FieldTagger, abstract: str) -> None:
    talk = candidate("Neural circuits of memory", abstract, "sites-biology", ["bio"])
    decision = tagger.tag(talk)
    assert "math" not in decision.fields, decision.reasons


@pytest.mark.parametrize(
    "title",
    [
        "Time series analysis of financial markets",
        "Time series forecasting with foundation models",
        "Time series models for climate risk",
        "Time series foundation models for wearable sensors",
    ],
)
def test_a_topic_phrase_decides_alone(tagger: FieldTagger, title: str) -> None:
    decision = tagger.tag(candidate(title, None, "localist-events", []))
    assert "math" in decision.fields, decision.reasons


def test_the_bare_words_alone_in_a_campus_title_are_not_enough(tagger: FieldTagger) -> None:
    """The trade-off: on a calendar without source fields, the title alone needs a phrase."""
    decision = tagger.tag(candidate(RAIN_TITLE, None, "localist-events", []))
    assert "math" not in decision.fields, decision.reasons
