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
from seminars.classify.rules import TermMatcher, Words, build_field_tagger, label_key
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


# Labels of the subject, type and audience filters of the Stanford Events calendar
# (/api/2/events/filters, recorded 2026-11-11).
LOCALIST_FILTER_LABELS: tuple[str, ...] = (
    "Arts/Media",
    "Theater/Dance",
    "Health/Wellness",
    "Music",
    "Business",
    "Career Development",
    "Education",
    "Engineering/Technology",
    "Environment/Sustainability",
    "Humanities",
    "Diversity/Identity",
    "International Affairs",
    "Law/Public Policy/Politics",
    "Medicine",
    "Religion/Spirituality",
    "Science",
    "Social Justice/Human Rights",
    "Public Service/Government",
    "Working at Stanford",
    "Class/Seminar",
    "Conference/Symposium",
    "Exhibition",
    "Film/Screening",
    "Lecture/Presentation/Talk",
    "Meeting",
    "Fitness/Recreational Sport",
    "Performance",
    "Social Event/Reception",
    "Tour",
    "Workshop",
    "Other",
    "Academic Dates",
    "Student Billing Dates",
    "PhD Defense",
    "Religious/Spiritual",
    "Support Space",
    "Students",
    "Students - Undergraduates",
    "Students - Graduates",
    "Postdocs",
    "Faculty",
    "Staff",
    "Affiliates",
    "General Public",
    "Staff - Academic",
    "Everyone",
    "Staff - Managers",
    "Alumni",
    "Members",
)


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that fails on a key that appears twice in one mapping."""


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    keys = [loader.construct_object(key) for key, _ in node.value]
    repeated = sorted(str(key) for key, count in Counter(keys).items() if count > 1)
    assert repeated == [], f"duplicate keys at line {node.start_mark.line + 1}: {repeated}"
    return loader.construct_mapping(node)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load_strict_yaml(path: Path) -> Any:
    """The content of ``path``; fails when one mapping holds a key twice.

    YAML itself keeps the last of two equal keys, so a repeated keyword
    would silently lose its first weight.
    """
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)


def term_lists(taxonomy: Taxonomy) -> dict[str, list[str]]:
    """Every list of terms in ``taxonomy``, by its place in the file."""
    lists: dict[str, list[str]] = {}
    for field, rules in taxonomy.fields.items():
        lists[f"{field}.keywords"] = list(rules.keywords)
        lists[f"{field}.series"] = rules.series
        lists[f"{field}.exclude_keywords"] = rules.exclude_keywords
    for reason, phrases in taxonomy.exclusions.items():
        lists[f"exclusions.{reason}"] = phrases
    return lists


def unmatchable(terms: list[str]) -> list[str]:
    """The terms that do not match a text spelled like themselves ("E. coli" in a title)."""
    return [
        term
        for term in terms
        if TermMatcher([term]).matched(Words.of(f"Talk: {term.rstrip('*')}.")) != (term,)
    ]


class TestFile:
    def test_has_no_duplicate_keys(self) -> None:
        load_strict_yaml(TAXONOMY_FILE)

    def test_every_field_has_keywords_and_series(self, taxonomy: Taxonomy) -> None:
        assert list(taxonomy.fields) == list(FIELD_IDS)
        for field, rules in taxonomy.fields.items():
            assert len(rules.keywords) >= 30, field
            assert len(rules.series) >= 10, field

    def test_every_rule_reason_has_phrases(self, taxonomy: Taxonomy) -> None:
        assert set(taxonomy.exclusions) == set(get_args(RuleExclusionReason))
        assert all(len(phrases) >= 5 for phrases in taxonomy.exclusions.values())

    def test_no_list_repeats_a_term_in_another_spelling(self, taxonomy: Taxonomy) -> None:
        for place, terms in term_lists(taxonomy).items():
            assert len(TermMatcher(terms).terms) == len(terms), place

    def test_every_term_matches_its_own_spelling(self, taxonomy: Taxonomy) -> None:
        for place, terms in term_lists(taxonomy).items():
            assert unmatchable(terms) == [], place

    def test_no_department_is_a_broad_calendar_filter(self, taxonomy: Taxonomy) -> None:
        """A subject such as "Medicine" also holds wellness fairs; it must never be a fact."""
        broad = {label_key(label) for label in LOCALIST_FILTER_LABELS}
        assert [label for label in taxonomy.departments if label_key(label) in broad] == []

    def test_departments_are_distinct_labels(self, taxonomy: Taxonomy) -> None:
        keys = [label_key(label) for label in taxonomy.departments]
        assert len(set(keys)) == len(keys)
        assert all(fields for fields in taxonomy.departments.values())

    def test_a_keyword_alone_counts_once_and_alone(
        self, taxonomy: Taxonomy, tagger: FieldTagger
    ) -> None:
        """A text that holds one keyword counts that keyword and nothing else of its field.

        So a weak keyword (below min_score) never assigns its field alone, even
        where other keywords overlap it ("drug" in "drug discovery"), and no
        word counts twice ("therapeutic" and "therapeutics").
        """
        for field, rules in taxonomy.fields.items():
            for term, weight in rules.keywords.items():
                decision = tagger.tag(campus(term.rstrip("*")))
                own = [reason for reason in decision.reasons if reason.startswith(f"{field}: ")]
                strong = weight >= rules.min_score
                assert own == ([f'{field}: keywords "{term}" ({weight:g})'] if strong else []), term

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
    ("math", "Sumsets and a conjecture of Erdos in additive combinatorics", None),
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
    ("ai-ml", "AI safety via debate", None),
    ("ai-ml", "Graph neural networks for materials discovery", None),
    ("ai-ml", "Flow matching on Riemannian manifolds", None),
    ("ai-ml", "Self-supervised representation learning for medical images", None),
    ("ai-ml", "Machine-learning interatomic potentials", None),
    ("ai-ml", "Research Acceleration: The View Inside OpenAI", None),
    # chem
    ("chem", "Enantioselective catalysis with earth-abundant metals", None),
    ("chem", "Total synthesis of complex alkaloids", None),
    ("chem", "Electrochemical CO2 reduction on copper", None),
    ("chem", "Metal-organic frameworks for water harvesting", None),
    ("chem", "Alchemical free energy calculations for drug design", None),
    ("chem", "Polymer mechanochemistry", None),
    ("chem", "Molecular dynamics of ion transport", None),
    ("chem", "Single-atom catalysts for ammonia synthesis", None),
    # bio
    ("bio", "De novo design of protein binders", None),
    ("bio", "A single-cell atlas of the developing human heart", None),
    ("bio", "CRISPR screens reveal regulators of T cell exhaustion", None),
    ("bio", "Cryo-EM structures of voltage-gated ion channels", None),
    ("bio", "Evolutionary dynamics of bacterial populations", None),
    ("bio", "Inverse folding with AlphaFold2 and ProteinMPNN", None),
    ("bio", "Bioengineering the gut microbiome", None),
    ("bio", "Synaptic plasticity in the hippocampus", None),
    ("bio", "Aging and dietary restriction in C. elegans", None),
    ("bio", "Quorum sensing: how E. coli counts its neighbors", None),
    ("bio", "Genetic variation in human populations", None),
    ("bio", "Genomic medicine for rare diseases", None),
    ("bio", "Mechanisms of DNA repair", None),
    # physics
    ("physics", "Superconductivity in twisted bilayer graphene", None),
    ("physics", "Searching for dark matter with quantum sensors", None),
    ("physics", "Gravitational waves from merging black holes", None),
    ("physics", "Galaxy formation in the early universe", None),
    ("physics", "Attosecond science at LCLS", None),
    ("physics", "Turbulence in astrophysical plasmas", None),
    ("physics", "Topological insulators and Majorana modes", None),
    ("physics", "Photonic integrated circuits", None),
    ("physics", "Building Diamond Semiconductors for Decommissioning and Beyond", None),
    # med
    ("med", "Immunotherapy resistance in melanoma", None),
    ("med", "Clinical trials of gene therapy for sickle cell disease", None),
    ("med", "Predicting sepsis in hospitalized patients", None),
    ("med", "Global health and maternal mortality in sub-Saharan Africa", None),
    ("med", "Neurodegeneration and the aging brain", None),
    ("med", "Physical therapy after knee surgery", None),
    ("med", "Pediatric oncology in the era of precision medicine", None),
    ("med", "Long COVID: mechanisms and treatments", None),
    ("med", "mRNA vaccine design", None),
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
    ("cs", "Robotic process automation for accounting", None),
    ("cs", "Computational biology of the gut", None),
    # ai-ml
    ("ai-ml", "Taiwan and the South China Sea", None),
    ("ai-ml", "Buffer exchange", "Dissolve 5 mL of buffer and wait 10 min."),
    ("ai-ml", "The electrical transformer and the grid", None),
    ("ai-ml", "Said and unsaid: silence in literature", None),
    ("ai-ml", "Maintaining trust in public institutions", None),
    ("ai-ml", "A Conversation with Ai Weiwei", None),
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
    ("bio", "The DNA of Great Companies", None),
    # physics
    ("physics", "Physical therapy after knee surgery", None),
    ("physics", "Metaphysics and modal logic", None),
    ("physics", "Blood plasma proteomics", None),
    ("physics", "Physics education research in large lectures", None),
    ("physics", "The optic nerve in glaucoma", None),
    (
        "physics",
        "Energy, Materials, and Water Use in Semiconductor Manufacturing: Challenges and "
        "Opportunities for Supply Chain Resiliency",
        None,
    ),
    # med
    ("med", "Mediterranean trade networks in antiquity", None),
    ("med", "Financial health of local governments", None),
    ("med", "The history of medicine in colonial India", None),
    ("med", "Immediate and remediated texts", None),
    ("med", "A therapeutic approach to conflict", None),
    ("med", "Drug discovery with small molecules", None),
    ("med", "Biomedical engineering design", None),
    ("med", "New Beginnings: Jumpstarting the Rest of Your Life for Clinician Educators", None),
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
        ("Oceans Seminar Series", "bio"),
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
        (["Department of Medicine"], ["med"]),
        (["Medicine", "Health/Wellness"], []),
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


# Talks of a campus-wide calendar whose abstract names a field in passing: a word in a
# speaker's biography, a list of words, a sponsor, a wellness class. Written after the
# records of the Stanford Events calendar (2026-11).
PASSING_MENTIONS: list[tuple[str, str, str]] = [
    (
        "Book Talk: The Successor and the Decline of Modern Russia",
        "The book follows Boris Nemtsov, a talented young physicist who became a reformist "
        "politician and the most prominent opponent of the regime.",
        "physics",
    ),
    (
        "Reimagining Democracy: America at 250",
        "Co-sponsored by the Stanford Law AI Initiative. Panelists discuss the founding "
        "documents, civic trust and the future of self-government.",
        "ai-ml",
    ),
    (
        "Book Talk: Words and Power in French Politics",
        "The author studies the words that politicians avoid or repeat: immigration, "
        "identity, vaccine, security, nation.",
        "med",
    ),
    (
        "War, Sanctions and the Intimacy of Everyday Life",
        "Sanctions became a catalyst for change, and the online ecology of protest moved "
        "from public squares to private chats.",
        "chem",
    ),
    (
        "War, Sanctions and the Intimacy of Everyday Life",
        "Sanctions became a catalyst for change, and the online ecology of protest moved "
        "from public squares to private chats.",
        "bio",
    ),
    (
        "Careers in International Law",
        "Alumni who work on trade, human rights and global health law talk about their "
        "paths after law school.",
        "med",
    ),
    (
        "Financial Planning Workshop (Webinar)",
        "Learn how to plan for retirement, budget for healthcare costs and save for "
        "long-term care.",
        "med",
    ),
    (
        "Hospice 101",
        "What hospice care means for patients and families, and how the hospital and the "
        "medical team support them at home.",
        "med",
    ),
    (
        "Make a Gutsy Move for Better Health",
        "Your gut microbiome shapes immune health and the risk of disease, cancer, "
        "diabetes and obesity. Bring your questions about food.",
        "med",
    ),
    (
        "Pattern Magic: 3D Rendering of Archival Sewing Patterns",
        "Computer science students and librarians built a database of historical patterns "
        "with computational tools and an algorithmic renderer.",
        "cs",
    ),
    (
        "Using AI to Find Sources",
        "A library workshop on AI tools and LLM chatbots for finding sources, and on "
        "checking what they return.",
        "ai-ml",
    ),
    (
        "Religiosity and Risk-Taking in Full-Population Danish Data",
        "Using registry data from before and during the COVID-19 pandemic, the paper "
        "measures how religious people take risks.",
        "med",
    ),
]


@pytest.mark.parametrize(("title", "abstract", "field"), PASSING_MENTIONS)
def test_a_passing_mention_in_the_abstract_does_not_tag(
    tagger: FieldTagger, event_filter: EventFilter, title: str, abstract: str, field: str
) -> None:
    item = campus(title, abstract)
    decision = tagger.tag(item)
    assert field not in decision.fields, decision.reasons
    assert decision.fields == []
    assert decision.needs_llm
    tagged = item.event.evolve(fields=decision.fields, field_tag_method=decision.method)
    result = event_filter.drop_out_of_scope([tagged])
    assert [excluded.reason for excluded in result.excluded] == ["out_of_scope"]


@pytest.mark.parametrize(
    ("abstract", "field"),
    [
        ("We prove a conjecture of Erdos on sumsets using additive combinatorics.", "math"),
        ("Microbial communities, their transcriptomes and nitrogen cycling bacteria.", "bio"),
        ("We train large language models with reinforcement learning.", "ai-ml"),
    ],
)
def test_a_research_abstract_alone_is_left_to_the_llm_or_counts_with_a_source_field(
    tagger: FieldTagger, abstract: str, field: str
) -> None:
    without = tagger.tag(campus("Seminar: Jane Doe", abstract))
    assert without.fields == []
    assert without.needs_llm
    with_source = tagger.tag(campus("Seminar: Jane Doe", abstract, default_fields=["physics"]))
    assert field in with_source.fields
    assert not with_source.needs_llm


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
    ("fitness", "Weekly Mindfulness Session"),
    ("fitness", "Intramural Sports Sign-ups"),
    ("course", "Peer Tutoring: Organic Chemistry"),
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
    ("academic_calendar", "American Surgical Association (ASA) Meeting Abstract Deadline"),
    ("academic_calendar", "The Pacific Association of Pediatric Surgeons (PAPS) Abstract Deadline"),
    ("academic_calendar", "No Seminar - Thanksgiving Holiday"),
    ("academic_calendar", "No SLAC Colloquium 11/23"),
    ("academic_calendar", "No Class: University Holiday"),
    ("social", "Alumni Reception at the Faculty Club"),
    ("fitness", "Chair Yoga for Staff"),
    ("fitness", "Group Workout on the Oval"),
    ("fitness", "World Diabetes Day Wellness Fair"),
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
        "Intelligent tutoring systems with large language models",
        "The neuroscience of mindfulness",
        "Lessons from the NIH intramural research program",
        "The Greek and Arabic Reception of Ptolemy's Almagest",
        "Wireless signal reception in dense urban areas",
        "Yoga and the vagus nerve: a clinical trial",
        "Lecture: the neuroscience of yoga",
        "Workout recovery and muscle biology",
        "Dark Matter Town Hall: the next decade of experiments",
        "No free lunch theorems for optimization",
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
        ("Physics Colloquium and Welcome Reception", []),
        ("Lecture and Guided Meditation", []),
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
