"""Meetings in the owner's fields are picks (config/interests.yaml).

The series allow-list names the meetings ("Protein Society", "ICLR", ...), and
an allow-listed name counts in the title too, so the title of an edition is
enough. The keywords "enzyme engineering" and "biomolecular design" count like
the other keywords of protein and enzyme design. Talks that name none of them
score what they scored without these entries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from factories import make_event
from seminars.classify.picks import build_picks_scorer
from seminars.config import load_interests
from seminars.models import Event

REPO_ROOT = Path(__file__).resolve().parents[1]
INTERESTS = load_interests(REPO_ROOT / "config" / "interests.yaml")
SCORER = build_picks_scorer(INTERESTS)

MEETING_SERIES: tuple[str, ...] = (
    "Protein Society",
    "RosettaCon",
    "Rosetta Commons",
    "RosettaCommons",
    "BayLearn",
    "MLSB",
    "MLBio",
    "RECOMB",
    "ISMB",
    "ICLR",
    "Enzyme Engineering",
    "Generative and Experimental Perspectives",
)
MEETING_KEYWORDS: tuple[str, ...] = ("enzyme engineering", "biomolecular design")

WITHOUT_MEETINGS = build_picks_scorer(
    INTERESTS.model_copy(
        update={
            "series_allowlist": [
                name for name in INTERESTS.series_allowlist if name not in MEETING_SERIES
            ],
            "keywords": {
                term: weight
                for term, weight in INTERESTS.keywords.items()
                if term not in MEETING_KEYWORDS
            },
        }
    )
)
"""The shipped interests without the entries of this file."""


def meeting(title: str, **overrides: Any) -> Event:
    values: dict[str, Any] = {
        "title": title,
        "speakers": [],
        "series": None,
        "abstract": None,
        "event_type": "conference",
        "all_day": True,
    }
    return make_event(**{**values, **overrides})


def test_the_entries_are_configured() -> None:
    assert set(MEETING_SERIES) <= set(INTERESTS.series_allowlist)
    assert {term: INTERESTS.keywords.get(term) for term in MEETING_KEYWORDS} == {
        "enzyme engineering": 1.0,
        "biomolecular design": 1.0,
    }
    assert "GEM" not in INTERESTS.series_allowlist


@pytest.mark.parametrize(
    ("title", "reasons"),
    [
        ("Protein Society 41st Annual Symposium", ['series "Protein Society" (+3)']),
        ("ICLR 2027", ['series "ICLR" (+3)']),
        ("RosettaCon 2027", ['series "RosettaCon" (+3)']),
        ("Rosetta Commons Winter Meeting", ['series "Rosetta Commons" (+3)']),
        ("RosettaCommons Summer Workshop", ['series "RosettaCommons" (+3)']),
        ("BayLearn 2027: Bay Area Machine Learning Symposium", ['series "BayLearn" (+3)']),
        ("MLSB 2026: Machine Learning in Structural Biology", ['series "MLSB" (+3)']),
        ("MLBio workshop at ICML", ['series "MLBio" (+3)']),
        ("RECOMB 2027", ['series "RECOMB" (+3)']),
        ("RECOMB-Seq 2027", ['series "RECOMB" (+3)']),
        ("ISMB/ECCB 2027", ['series "ISMB" (+3)']),
        (
            "Enzyme Engineering XXIX",
            ['"enzyme engineering" in title (+3)', 'series "Enzyme Engineering" (+3)'],
        ),
        (
            "Generative and Experimental Perspectives for Biomolecular Design",
            [
                '"biomolecular design" in title (+3)',
                'series "Generative and Experimental Perspectives" (+3)',
            ],
        ),
    ],
)
def test_a_meeting_in_the_owners_fields_is_a_pick(title: str, reasons: list[str]) -> None:
    result = SCORER.score(meeting(title))
    assert result.reasons == reasons
    assert result.score >= SCORER.threshold
    assert WITHOUT_MEETINGS.score(meeting(title)).score < SCORER.threshold


@pytest.mark.parametrize(
    ("fields", "score", "reasons"),
    [
        (
            {"title": "Machine learning for enzyme engineering"},
            6.0,
            ['"enzyme engineering" in title (+3)', 'series "Enzyme Engineering" (+3)'],
        ),
        (
            {"title": "Biomolecular design with diffusion models"},
            6.0,
            ['"biomolecular design" in title (+3)', '"diffusion model" in title (+3)'],
        ),
        (
            {"title": "TBA", "abstract": "Directed evolution for enzyme engineering."},
            2.0,
            ['"enzyme engineering" in abstract (+1)', '"directed evolution" in abstract (+1)'],
        ),
    ],
)
def test_the_new_keywords(fields: dict[str, Any], score: float, reasons: list[str]) -> None:
    result = SCORER.score(make_event(**{"series": None, "abstract": None, **fields}))
    assert (result.score, result.reasons) == (score, reasons)


@pytest.mark.parametrize(
    "title",
    [
        "GEM: a gene expression model of the developing cortex",
        "Gems and minerals of California",
        "Society for Neuroscience annual meeting",
        "The Rosetta Stone and the decipherment of hieroglyphs",
        "Recombination hotspots in bacterial genomes",
        "Enzyme kinetics in the classroom",
        "Protein secretion and the society of cells",
        "Biomolecular condensates and phase separation",
        "Design of molecular machines",
    ],
)
def test_near_misses_name_no_meeting(title: str) -> None:
    result = SCORER.score(make_event(title=title, series=None, abstract=None))
    assert result.reasons == []
    assert result.score == 0.0


@pytest.mark.parametrize(
    "fields",
    [
        {"title": "Inverse folding with ProteinMPNN and beyond"},
        {"title": "Equivariant cohomology and localization", "series": "Topology Seminar"},
        {"title": "TBA", "series": "Stanford Mathematics Colloquium"},
        {"title": "SLAC Colloquium: the LCLS-II upgrade"},
        {"title": "De novo mutations in neurodevelopmental disorders"},
        {"title": "Physical Chemistry Seminar: Professor Connor Coley, MIT"},
        {"title": "Viehweg-Zuo I", "series": "Algebraic Geometry Seminar"},
        {"title": "Long-Context Structure Prediction with ESMFold2 Webinar"},
        {
            "title": "Learning to see molecules",
            "abstract": "We combine cryo-EM with molecular dynamics and free energy methods.",
        },
    ],
)
def test_talks_that_name_no_meeting_score_as_before(fields: dict[str, Any]) -> None:
    event = make_event(**{"series": None, **fields})
    assert SCORER.score(event) == WITHOUT_MEETINGS.score(event)
