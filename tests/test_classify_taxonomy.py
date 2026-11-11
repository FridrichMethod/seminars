"""The shipped config/taxonomy.yaml: realistic seminar titles, true positives and near misses.

Every candidate here comes from a calendar-wide source without default
fields (like the Localist calendar), so the rules alone decide.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml

from factories import make_candidate, make_event
from seminars.classify.filters import build_event_filter
from seminars.classify.rules import TermMatcher, build_field_tagger
from seminars.config import Settings, Taxonomy, load_settings, load_taxonomy
from seminars.contracts import EventFilter, FieldTagger
from seminars.models import FIELD_IDS, Candidate, RuleExclusionReason

REPO_ROOT = Path(__file__).resolve().parents[1]
TAXONOMY_FILE = REPO_ROOT / "config" / "taxonomy.yaml"


@pytest.fixture(scope="module")
def taxonomy() -> Taxonomy:
    return load_taxonomy(TAXONOMY_FILE)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings(REPO_ROOT / "config" / "settings.yaml")


@pytest.fixture(scope="module")
def tagger(taxonomy: Taxonomy) -> FieldTagger:
    return build_field_tagger(taxonomy)


@pytest.fixture(scope="module")
def event_filter(taxonomy: Taxonomy, settings: Settings) -> EventFilter:
    return build_event_filter(taxonomy, settings)


def campus(
    title: str, abstract: str | None = None, *, series: str | None = None, **hints: Any
) -> Candidate:
    """A candidate from a campus-wide calendar: no default fields."""
    event = make_event(
        uid="localist-1",
        title=title,
        abstract=abstract,
        series=series,
        fields=[],
        field_tag_method="source",
    )
    base: dict[str, Any] = {"source_ids": ["localist-events"], "default_fields": []}
    return make_candidate(event, **{**base, **hints})


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that fails on a key that appears twice in one mapping."""


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    keys = [loader.construct_object(key) for key, _ in node.value]
    repeated = sorted(str(key) for key, count in Counter(keys).items() if count > 1)
    assert repeated == [], f"duplicate keys at line {node.start_mark.line + 1}: {repeated}"
    return loader.construct_mapping(node)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


class TestFile:
    def test_has_no_duplicate_keys(self) -> None:
        yaml.load(TAXONOMY_FILE.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)

    def test_every_field_has_keywords_and_series(self, taxonomy: Taxonomy) -> None:
        assert list(taxonomy.fields) == list(FIELD_IDS)
        for field, rules in taxonomy.fields.items():
            assert len(rules.keywords) >= 30, field
            assert len(rules.series) >= 10, field

    def test_every_rule_reason_has_phrases(self, taxonomy: Taxonomy) -> None:
        assert set(taxonomy.exclusions) == set(get_args(RuleExclusionReason))
        assert all(len(phrases) >= 5 for phrases in taxonomy.exclusions.values())

    def test_no_list_repeats_a_term_in_another_spelling(self, taxonomy: Taxonomy) -> None:
        lists: dict[str, list[str]] = {}
        for field, rules in taxonomy.fields.items():
            lists[f"{field}.keywords"] = list(rules.keywords)
            lists[f"{field}.series"] = rules.series
            lists[f"{field}.exclude_keywords"] = rules.exclude_keywords
        for reason, phrases in taxonomy.exclusions.items():
            lists[f"exclusions.{reason}"] = phrases
        for place, terms in lists.items():
            assert len(TermMatcher(terms).terms) == len(terms), place

    def test_departments_are_distinct_labels(self, taxonomy: Taxonomy) -> None:
        from seminars.classify.rules import label_key

        keys = [label_key(label) for label in taxonomy.departments]
        assert len(set(keys)) == len(keys)
        assert all(fields for fields in taxonomy.departments.values())

    @pytest.mark.parametrize("term", ["bio", "ai-ml", "ml", "med", "chem", "math*", "bio*"])
    def test_no_trap_terms(self, taxonomy: Taxonomy, term: str) -> None:
        """Short terms that are parts of other words, or units ("mL"), are never keywords."""
        for rules in taxonomy.fields.values():
            assert term not in {keyword.lower() for keyword in rules.keywords}


FIELD_CASES: list[tuple[str, str, str | None]] = [
    # math
    ("math", "Rigidity of hyperbolic manifolds and Teichmuller curves", None),
    ("math", "The arithmetic of elliptic curves over function fields", None),
    ("math", "Homological mirror symmetry for log Calabi-Yau surfaces", None),
    ("math", "Random matrix universality and the local semicircle law", None),
    ("math", "Conformal prediction under distribution shift", None),
    ("math", "Ricci curvature, optimal transport and concentration inequalities", None),
    ("math", "TBA", "We prove a conjecture of Erdos on sumsets using additive combinatorics."),
    # cs
    ("cs", "Verified compilers for smart contracts", None),
    ("cs", "Differential privacy at scale", None),
    ("cs", "Approximation algorithms for k-means clustering", None),
    ("cs", "Zero-knowledge proofs in practice", None),
    ("cs", "Fault-tolerant distributed systems in the cloud", None),
    ("cs", "Soft robotics for minimally invasive surgery", None),
    ("cs", "Designing user interfaces for older adults", "A study in human-computer interaction."),
    # ai-ml
    ("ai-ml", "Scaling laws for large language models", None),
    ("ai-ml", "Reinforcement learning from human feedback", None),
    ("ai-ml", "AI for science: promises and pitfalls", None),
    ("ai-ml", "Graph neural networks for materials discovery", None),
    ("ai-ml", "Flow matching on Riemannian manifolds", None),
    ("ai-ml", "Self-supervised representation learning for medical images", None),
    ("ai-ml", "Machine-learning interatomic potentials", None),
    # chem
    ("chem", "Enantioselective catalysis with earth-abundant metals", None),
    ("chem", "Total synthesis of complex alkaloids", None),
    ("chem", "Electrochemical CO2 reduction on copper", None),
    ("chem", "Metal-organic frameworks for water harvesting", None),
    ("chem", "Alchemical free energy calculations for drug design", None),
    ("chem", "Polymer mechanochemistry", None),
    ("chem", "Molecular dynamics of ion transport", None),
    # bio
    ("bio", "De novo design of protein binders", None),
    ("bio", "A single-cell atlas of the developing human heart", None),
    ("bio", "CRISPR screens reveal regulators of T cell exhaustion", None),
    ("bio", "Cryo-EM structures of voltage-gated ion channels", None),
    ("bio", "Evolutionary dynamics of bacterial populations", None),
    ("bio", "Inverse folding with AlphaFold2 and ProteinMPNN", None),
    ("bio", "Bioengineering the gut microbiome", None),
    ("bio", "Synaptic plasticity in the hippocampus", None),
    # physics
    ("physics", "Superconductivity in twisted bilayer graphene", None),
    ("physics", "Searching for dark matter with quantum sensors", None),
    ("physics", "Gravitational waves from merging black holes", None),
    ("physics", "Galaxy formation in the early universe", None),
    ("physics", "Attosecond science at LCLS", None),
    ("physics", "Turbulence in astrophysical plasmas", None),
    ("physics", "Topological insulators and Majorana modes", None),
    # med
    ("med", "Immunotherapy resistance in melanoma", None),
    ("med", "Clinical trials of gene therapy for sickle cell disease", None),
    ("med", "Predicting sepsis in hospitalized patients", None),
    ("med", "Global health and maternal mortality in sub-Saharan Africa", None),
    ("med", "Neurodegeneration and the aging brain", None),
    ("med", "Physical therapy after knee surgery", None),
    ("med", "Pediatric oncology in the era of precision medicine", None),
]


@pytest.mark.parametrize(("field", "title", "abstract"), FIELD_CASES)
def test_true_positives(tagger: FieldTagger, field: str, title: str, abstract: str | None) -> None:
    decision = tagger.tag(campus(title, abstract))
    assert field in decision.fields, decision.reasons
    assert decision.method == "rules"


NEAR_MISSES: list[tuple[str, str, str | None]] = [
    # math
    ("math", "Aftermath of the 2008 financial crisis", None),
    ("math", "Topological insulators and Majorana modes", None),
    ("math", "The probability of rain: forecasting for farmers", None),
    ("math", "Teaching mathematics in middle school classrooms", None),
    ("math", "Stanford Math Circle: puzzles for kids", None),
    # cs
    ("cs", "National security and the Pacific", None),
    ("cs", "Networking night for alumni", None),
    ("cs", "Computer science education in K-12 schools", None),
    ("cs", "International Security Seminar: nuclear deterrence", None),
    # ai-ml
    ("ai-ml", "Taiwan and the South China Sea", None),
    ("ai-ml", "Buffer exchange", "Dissolve 5 mL of buffer and wait 10 min."),
    ("ai-ml", "The electrical transformer and the grid", None),
    ("ai-ml", "Said and unsaid: silence in literature", None),
    ("ai-ml", "Maintaining trust in public institutions", None),
    # chem
    ("chem", "Team chemistry in professional sports", None),
    ("chem", "The alchemy of medieval Europe", None),
    ("chem", "Chemical weapons and international law", None),
    # bio
    ("bio", "A biography of Rosalind Franklin", None),
    ("bio", "Perovskite solar cells", None),
    ("bio", "Cell phones and cellular networks", None),
    ("bio", "Power plant emissions and air quality", None),
    ("bio", "The evolution of democratic institutions", None),
    ("bio", "Symbolic logic and the bioethics of naming", None),
    # physics
    ("physics", "Physical therapy after knee surgery", None),
    ("physics", "Metaphysics and modal logic", None),
    ("physics", "Blood plasma proteomics", None),
    ("physics", "Physics education research in large lectures", None),
    # med
    ("med", "Mediterranean trade networks in antiquity", None),
    ("med", "Financial health of local governments", None),
    ("med", "The history of medicine in colonial India", None),
    ("med", "Immediate and remediated texts", None),
]


@pytest.mark.parametrize(("field", "title", "abstract"), NEAR_MISSES)
def test_near_misses(tagger: FieldTagger, field: str, title: str, abstract: str | None) -> None:
    decision = tagger.tag(campus(title, abstract))
    assert field not in decision.fields, decision.reasons


@pytest.mark.parametrize(
    ("series", "field"),
    [
        ("Mathematics Colloquium", "math"),
        ("Stanford Statistics Seminar", "math"),
        ("Theory Lunch", "cs"),
        ("Stanford NLP Seminar", "ai-ml"),
        ("MLSys Seminar Series", "ai-ml"),
        ("Physical Chemistry Seminar", "chem"),
        ("Bioengineering Seminar Series", "bio"),
        ("Applied Physics/Physics Colloquium", "physics"),
        ("SLAC Colloquium", "physics"),
        ("Medicine Grand Rounds", "med"),
    ],
)
def test_series_imply_their_field(tagger: FieldTagger, series: str, field: str) -> None:
    assert field in tagger.tag(campus("TBA", series=series)).fields


@pytest.mark.parametrize(
    ("categories", "fields"),
    [
        (["Chemistry"], ["chem"]),
        (["Department of Mathematics"], ["math"]),
        (["Bioengineering", "Lecture / Presentation"], ["bio"]),
        (["Microbiology and Immunology"], ["bio", "med"]),
        (["SLAC National Accelerator Laboratory"], ["physics"]),
        (["Stanford Medicine"], ["med"]),
        (["History"], []),
    ],
)
def test_departments(tagger: FieldTagger, categories: list[str], fields: list[str]) -> None:
    decision = tagger.tag(campus("TBA", categories=categories))
    assert decision.fields == fields
    assert decision.needs_llm is not fields


@pytest.mark.parametrize(
    "title",
    [
        "Poetry reading: new voices from the Pacific",
        "Opera in the age of Verdi",
        "Democracy and its discontents",
        "Book talk: the Ottoman Empire and the modern world",
        "The aesthetics of Japanese gardens",
    ],
)
def test_humanities_talks_are_out_of_scope(
    tagger: FieldTagger, event_filter: EventFilter, title: str
) -> None:
    item = campus(title)
    decision = tagger.tag(item)
    assert decision.fields == []
    assert decision.needs_llm
    tagged = item.event.evolve(fields=decision.fields, field_tag_method=decision.method)
    result = event_filter.drop_out_of_scope([tagged])
    assert result.kept == []
    assert [excluded.reason for excluded in result.excluded] == ["out_of_scope"]


def test_a_vetoed_field_is_left_to_the_llm(tagger: FieldTagger) -> None:
    decision = tagger.tag(campus("The history of medicine in colonial India"))
    assert decision.fields == []
    assert decision.needs_llm
    assert 'med: vetoed by "history of medicine"' in decision.reasons


def test_department_sources_keep_their_field_whatever_the_title(tagger: FieldTagger) -> None:
    decision = tagger.tag(campus("Holiday reading list", default_fields=["chem"]))
    assert decision.fields == ["chem"]
    assert decision.method == "source"
    assert not decision.needs_llm


# --- the filter phrases ----------------------------------------------------------------------


EXCLUDED: list[tuple[str, str]] = [
    ("course", "CS 229 Midterm Review Session"),
    ("course", "Math 51 Problem Session"),
    ("course", "Chem 33 Discussion Section"),
    ("office_hours", "Dean's Office Hours"),
    ("office_hours", "Drop-in Hours with Career Coaches"),
    ("admissions", "Bioengineering PhD Admissions Info Session"),
    ("admissions", "Admit Weekend for Prospective Students"),
    ("admissions", "Virtual Information Session: MS in Statistics"),
    ("career", "Career Fair: Engineering and Science"),
    ("career", "Resume Review with BEAM"),
    ("career", "Networking Night for Postdocs"),
    ("social", "Department Holiday Party"),
    ("social", "Graduate Student Happy Hour"),
    ("social", "Welcome Reception"),
    ("fitness", "Lunchtime Yoga"),
    ("fitness", "Mindfulness for Graduate Students"),
    ("religious", "Catholic Mass"),
    ("religious", "Shabbat Dinner"),
    ("religious", "Interfaith Service of Remembrance"),
    ("administrative", "Faculty Meeting"),
    ("administrative", "Lab Safety Training"),
    ("administrative", "Town Hall with the Provost"),
    ("academic_calendar", "Tuition Due"),
    ("academic_calendar", "Last Day to Drop Classes"),
    ("academic_calendar", "Thanksgiving Holiday (No Classes)"),
    ("academic_calendar", "Spring Break"),
]


@pytest.mark.parametrize(("reason", "title"), EXCLUDED)
def test_non_talks_are_excluded(event_filter: EventFilter, reason: str, title: str) -> None:
    result = event_filter.filter([campus(title)])
    assert [excluded.reason for excluded in result.excluded] == [reason]
    assert result.kept == []


@pytest.mark.parametrize(
    "title",
    [
        "Receptor signaling in neurons",
        "Mass spectrometry of membrane proteins",
        "Deadline-aware scheduling for real-time systems",
        "Forecasting hospital admissions with machine learning",
        "Molecular orientation at interfaces",
        "Social networks and epidemics",
        "Theory Lunch: lower bounds for matrix multiplication",
        "KIPAC Tea Talk: fast radio bursts",
        "Chaotic mixers for microfluidics",
        "Partial differential equations in fluid flow",
        "Spin liquids in frustrated magnets",
        "Midterm elections and polarization",
        "The office of the future: robots at work",
    ],
)
def test_talks_with_tricky_words_are_kept(event_filter: EventFilter, title: str) -> None:
    result = event_filter.filter([campus(title)])
    assert result.excluded == []
    assert result.uncertain == []


@pytest.mark.parametrize(
    ("title", "categories"),
    [
        ("Career Development Seminar: navigating industry", []),
        ("Physics Colloquium and Reception", []),
        ("Lecture: the neuroscience of yoga", []),
        ("Dark matter at SLAC", ["Social Event / Reception"]),
    ],
)
def test_ambiguous_items_are_kept_and_left_to_the_llm(
    event_filter: EventFilter, title: str, categories: list[str]
) -> None:
    item = campus(title, categories=categories)
    result = event_filter.filter([item])
    assert result.kept == [item]
    assert result.uncertain == [item.event.uid]
