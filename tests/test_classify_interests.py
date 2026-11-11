"""The shipped config/interests.yaml: the preset interests, the series allow-list and picks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from factories import make_event, make_speaker
from seminars.classify.picks import build_picks_scorer
from seminars.classify.rules import TermMatcher, Words
from seminars.config import Interests, load_interests
from seminars.contracts import PicksScorer
from seminars.models import Event
from test_classify_taxonomy import load_strict_yaml, unmatchable

REPO_ROOT = Path(__file__).resolve().parents[1]
INTERESTS_FILE = REPO_ROOT / "config" / "interests.yaml"

PRESET_KEYWORDS: list[str] = [
    "protein design",
    "protein engineering",
    "enzyme design",
    "de novo",
    "inverse folding",
    "protein language model",
    "structure prediction",
    "AlphaFold",
    "RoseTTAFold",
    "Boltz",
    "diffusion model",
    "flow matching",
    "generative model",
    "molecular dynamics",
    "free energy",
    "FEP",
    "cryo-EM",
    "directed evolution",
    "biocatalysis",
    "equivariant",
    "geometric deep learning",
    "optimal transport",
    "stochastic differential equations",
    "algebraic geometry",
    "number theory",
    "topology",
    "representation theory",
    "probability",
    "combinatorics",
]
"""The preset interest keywords, spelled as the project plan lists them."""

WEAK_PRESETS: frozenset[str] = frozenset({"de novo"})
"""Presets that are too common on their own ("de novo mutations") to make a pick alone."""

PRESET_SERIES: list[str] = [
    "Math Colloquium",
    "Applied Physics/Physics Colloquium",
    "Bioengineering seminar",
    "ChEM-H seminars",
    "Statistics seminar",
    "NLP seminar",
    "MLSys seminar",
    "SLAC Colloquium",
]
"""The preset series allow-list, spelled as the project plan lists it."""


@pytest.fixture(scope="module")
def interests() -> Interests:
    return load_interests(INTERESTS_FILE)


@pytest.fixture(scope="module")
def scorer(interests: Interests) -> PicksScorer:
    return build_picks_scorer(interests)


def talk(
    title: str,
    abstract: str | None = None,
    *,
    series: str | None = None,
    speakers: list[str] | None = None,
    **overrides: Any,
) -> Event:
    return make_event(
        title=title,
        abstract=abstract,
        series=series,
        speakers=[make_speaker(name=name) for name in speakers or ["Jane Doe"]],
        **overrides,
    )


class TestFile:
    def test_has_no_duplicate_keys(self) -> None:
        load_strict_yaml(INTERESTS_FILE)

    def test_the_strict_reader_rejects_a_repeated_key(self, tmp_path: Path) -> None:
        path = tmp_path / "interests.yaml"
        path.write_text("keywords:\n  topology: 1\n  topology: 2\n", encoding="utf-8")
        with pytest.raises(AssertionError, match="duplicate keys at line 2"):
            load_strict_yaml(path)

    def test_spells_out_every_setting(self) -> None:
        raw = load_strict_yaml(INTERESTS_FILE)
        assert set(raw) == set(Interests.model_fields)

    def test_the_llm_rerank_switch_exists_and_is_off(self, interests: Interests) -> None:
        assert load_strict_yaml(INTERESTS_FILE)["llm_rerank"] is False
        assert interests.llm_rerank is False

    def test_the_scale(self, interests: Interests) -> None:
        assert interests.threshold == 3.0
        multipliers = interests.multipliers
        assert (multipliers.title, multipliers.series, multipliers.abstract) == (3.0, 2.0, 1.0)
        assert interests.series_bonus == interests.threshold
        assert interests.speaker_bonus == interests.threshold

    def test_no_list_repeats_a_term_in_another_spelling(self, interests: Interests) -> None:
        lists = {
            "keywords": list(interests.keywords),
            "series_allowlist": interests.series_allowlist,
            "speaker_watchlist": interests.speaker_watchlist,
            "exclusions": list(interests.exclusions),
        }
        for place, terms in lists.items():
            assert len(TermMatcher(terms).terms) == len(terms), place
            assert unmatchable(terms) == [], place

    @pytest.mark.parametrize("spelling", PRESET_KEYWORDS)
    def test_every_preset_keyword_is_configured(self, interests: Interests, spelling: str) -> None:
        assert TermMatcher(interests.keywords).matched(Words.of(spelling)) != ()

    @pytest.mark.parametrize("series", PRESET_SERIES)
    def test_every_preset_series_is_allowlisted(self, interests: Interests, series: str) -> None:
        assert series in interests.series_allowlist


@pytest.mark.parametrize("spelling", [term for term in PRESET_KEYWORDS if term not in WEAK_PRESETS])
def test_a_preset_keyword_in_the_title_makes_a_pick(scorer: PicksScorer, spelling: str) -> None:
    result = scorer.score(talk(f"Recent progress in {spelling}"))
    assert result.score >= scorer.threshold, result.reasons
    assert result.reasons[0].endswith(" in title (+3)")


@pytest.mark.parametrize("series", PRESET_SERIES)
def test_an_allowlisted_series_makes_a_pick(scorer: PicksScorer, series: str) -> None:
    result = scorer.score(talk("TBA", series=series))
    assert result.score >= scorer.threshold
    assert result.reasons == [f'series "{series}" (+3)']


PICKS: list[tuple[dict[str, Any], float, list[str]]] = [
    (
        {"title": "Inverse folding with ProteinMPNN and beyond"},
        3.0,
        ['"inverse folding" in title (+3)'],
    ),
    (
        {"title": "Boltz-2: towards accurate and efficient binding affinity prediction"},
        3.0,
        ['"Boltz" in title (+3)'],
    ),
    (
        {"title": "AlphaFold3 and the protein structure universe"},
        3.0,
        ['"AlphaFold*" in title (+3)'],
    ),
    (
        {"title": "Generative models of protein backbones with flow matching"},
        6.0,
        ['"flow matching" in title (+3)', '"generative model" in title (+3)'],
    ),
    (
        {"title": "De novo design of luciferases"},
        4.5,
        ['"de novo design*" in title (+3)', '"de novo" in title (+1.5)'],
    ),
    (
        {"title": "De novo designed binders against influenza"},
        4.5,
        ['"de novo design*" in title (+3)', '"de novo" in title (+1.5)'],
    ),
    (
        {"title": "Equivariant cohomology and localization", "series": "Topology Seminar"},
        5.0,
        ['"equivariant" in title (+3)', '"topology" in series (+2)'],
    ),
    (
        {
            "title": "Learning to see molecules",
            "abstract": "We combine cryo-EM with molecular dynamics and free energy methods.",
        },
        3.0,
        [
            '"molecular dynamics" in abstract (+1)',
            '"free energy" in abstract (+1)',
            '"cryo-EM" in abstract (+1)',
        ],
    ),
    (
        {"title": "TBA", "series": "Stanford Mathematics Colloquium"},
        3.0,
        ['series "Mathematics Colloquium" (+3)'],
    ),
    (
        {"title": "SLAC Colloquium: the LCLS-II upgrade"},
        3.0,
        ['series "SLAC Colloquium" (+3)'],
    ),
]


@pytest.mark.parametrize(("fields", "score", "reasons"), PICKS)
def test_picks_and_their_reasons(
    scorer: PicksScorer, fields: dict[str, Any], score: float, reasons: list[str]
) -> None:
    result = scorer.score(talk(**fields))
    assert result.score == score
    assert result.reasons == reasons
    assert result.score >= scorer.threshold


@pytest.mark.parametrize(
    "fields",
    [
        {"title": "De novo mutations in neurodevelopmental disorders"},
        {"title": "Recent advances", "abstract": "We use molecular dynamics."},
        {"title": "Protein design for high school students"},
        {"title": "Undergraduate research in number theory"},
        {"title": "Fun with topology for kids", "series": "Math Circle"},
    ],
)
def test_weak_or_penalized_matches_are_not_picks(
    scorer: PicksScorer, fields: dict[str, Any]
) -> None:
    result = scorer.score(talk(**fields))
    assert result.reasons
    assert result.score < scorer.threshold


@pytest.mark.parametrize(
    "title",
    [
        "Boltzmann machines and energy-based models",
        "Topological insulators and Majorana modes",
        "Probabilistic numerics for scientific computing",
        "Diffusion of innovations in rural markets",
        "Cryogenic storage of vaccines",
        "Free will and moral responsibility",
        "Enzymes in laundry detergents",
        "Combinatorial auctions for spectrum",
        "Number of cases in the pandemic",
        "Folding chairs and the design of public spaces",
    ],
)
def test_near_misses_score_nothing(scorer: PicksScorer, title: str) -> None:
    result = scorer.score(talk(title))
    assert result.score == 0.0
    assert result.reasons == []


def test_scores_do_not_depend_on_what_else_is_scored(scorer: PicksScorer) -> None:
    events = [talk(**fields, uid=f"uid-{number}") for number, (fields, _, _) in enumerate(PICKS)]
    forward = {event.uid: scorer.score(event) for event in events}
    backward = {event.uid: scorer.score(event) for event in reversed(events)}
    fresh = build_picks_scorer(load_interests(INTERESTS_FILE))
    assert forward == backward == {event.uid: fresh.score(event) for event in events}
