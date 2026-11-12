"""Words of computing in the shipped taxonomy: computer science needs a topic, not a method.

Talks of every field compute, run algorithms and write software, so the
weak words of computing ("algorithm", "computation", "security") never put
a talk into cs, however many of them it uses; a topic of computer science
does ("approximation algorithms", "parallel computing", "encryption").
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


@pytest.fixture(scope="module")
def taxonomy() -> Taxonomy:
    return load_taxonomy(REPO_ROOT / "config" / "taxonomy.yaml")


@pytest.fixture(scope="module")
def tagger(taxonomy: Taxonomy) -> FieldTagger:
    return build_field_tagger(taxonomy)


def talk(title: str, abstract: str | None = None, **hints: Any) -> Candidate:
    event = make_event(
        title=title, abstract=abstract, series=None, fields=[], field_tag_method="source"
    )
    base: dict[str, Any] = {"source_ids": ["localist-stanford"], "default_fields": []}
    return make_candidate(event, **{**base, **hints})


def test_the_weak_words_of_computing_together_never_reach_the_score(taxonomy: Taxonomy) -> None:
    rules = taxonomy.fields["cs"]
    weak = [weight for weight in rules.keywords.values() if weight < rules.min_score]
    assert sum(weak) < rules.min_score


STRING_THEORY = (
    "String theory provides our most developed framework for unifying quantum mechanics and "
    "gravity. I will discuss recent progress drawing on new analytic techniques and "
    "large-scale computations, and briefly describe how AI can guide these searches and help "
    "develop new algorithms for studying string compactifications."
)
VISION = (
    "Our lab explores the neural circuits of natural vision in mice, with neural recording, "
    "computational analysis and models of visual computation."
)


@pytest.mark.parametrize(
    ("title", "abstract", "source"),
    [
        ("A Landscape of Candidate de Sitter Vacua in String Theory", STRING_THEORY, "physics"),
        ("Neural circuits for natural vision", VISION, "bio"),
        ("Climate models", "Software, hardware and GPU computing for climate models.", "physics"),
    ],
)
def test_words_of_method_in_an_abstract_add_no_cs(
    tagger: FieldTagger, title: str, abstract: str, source: str
) -> None:
    decision = tagger.tag(talk(title, abstract, default_fields=[source]))
    assert "cs" not in decision.fields, decision.reasons
    assert source in decision.fields


@pytest.mark.parametrize(
    "title",
    [
        "Security and privacy of health records",
        "Software and hardware of the modern orchestra",
        "Algorithms, computation and the law",
        "Robots and the internet in public life",
    ],
)
def test_weak_words_of_computing_in_a_title_add_no_cs(tagger: FieldTagger, title: str) -> None:
    assert "cs" not in tagger.tag(talk(title)).fields


@pytest.mark.parametrize(
    ("title", "abstract", "source"),
    [
        (
            "Heterogenous CPU-GPU Parallel Computing for Real-Time Hybrid Simulation",
            None,
            None,
        ),
        ("Detecting coordinated behavior across multiple fully encrypted domains", None, "ai-ml"),
        ("Computing on encrypted data: from concept to reality", None, None),
        (
            "Accelerating Dark Matter Searches Using Grover's Algorithm",
            "The qubits are then available for quantum computational processing.",
            "physics",
        ),
        (
            "How to construct a quantum Markov chain",
            "A rigorous quantum algorithmic framework for sampling Gibbs states on quantum "
            "computers.",
            "math",
        ),
        ("Approximation algorithms for k-means clustering", None, None),
        ("Differential privacy at scale", None, None),
    ],
)
def test_a_topic_of_computer_science_still_adds_cs(
    tagger: FieldTagger, title: str, abstract: str | None, source: str | None
) -> None:
    defaults = [source] if source else []
    decision = tagger.tag(talk(title, abstract, default_fields=defaults))
    assert "cs" in decision.fields, decision.reasons
