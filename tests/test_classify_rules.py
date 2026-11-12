"""Word matching and the rule-based field tagger (seminars.classify.rules)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from factories import make_candidate, make_event
from seminars.classify.rules import (
    Hit,
    RuleFieldTagger,
    TermMatcher,
    Words,
    build_field_tagger,
    label_key,
)
from seminars.config import ConfigError, Taxonomy
from seminars.contracts import FieldTagger, FieldTaggerFactory, load_implementation
from seminars.models import Candidate, TagDecision


def words(text: str | None) -> Words:
    return Words.of(text)


def candidate(
    title: str = "TBA",
    *,
    abstract: str | None = None,
    series: str | None = None,
    **hints: Any,
) -> Candidate:
    event = make_event(
        title=title, abstract=abstract, series=series, fields=[], field_tag_method="source"
    )
    defaults: dict[str, Any] = {"source_ids": ["localist-events"], "default_fields": []}
    return make_candidate(event, **{**defaults, **hints})


# --- words -----------------------------------------------------------------------------------


class TestWords:
    def test_folds_case_and_accents(self) -> None:
        assert words("Poincar\u00e9 and K\u00e4hler Geometry").segments == (
            ("poincare", "and", "kahler", "geometry"),
        )

    def test_hyphens_slashes_and_apostrophes_separate_words(self) -> None:
        assert words("cryo-EM/AlphaFold Hilbert's").segments == (
            ("cryo", "em", "alphafold", "hilbert", "s"),
        )

    def test_sentence_punctuation_and_brackets_start_a_new_segment(self) -> None:
        assert words("Proteins. Design (folding); fast").segments == (
            ("proteins",),
            ("design",),
            ("folding",),
            ("fast",),
        )

    def test_points_inside_numbers_and_abbreviations_do_not_split(self) -> None:
        assert words("Version 3.5 by a Ph.D student").segments == (
            ("version", "3", "5", "by", "a", "ph", "d", "student"),
        )

    @pytest.mark.parametrize(
        ("text", "segment"),
        [
            ("Gut microbes: E. coli and C. elegans", ("e", "coli", "and", "c", "elegans")),
            ("A talk by J. R. Doe", ("a", "talk", "by", "j", "r", "doe")),
            ("U.S. policy", ("u", "s", "policy")),
        ],
    )
    def test_a_point_after_an_initial_does_not_split(
        self, text: str, segment: tuple[str, ...]
    ) -> None:
        assert words(text).segments[-1] == segment

    def test_a_point_after_a_longer_word_still_splits(self) -> None:
        assert words("Vitamin D. Then ab. Cd").segments == (
            ("vitamin", "d", "then", "ab"),
            ("cd",),
        )

    def test_a_line_break_starts_a_new_segment(self) -> None:
        assert words("Protein\nDesign").segments == (("protein",), ("design",))

    def test_ligatures_and_compatibility_forms_are_folded(self) -> None:
        assert words("\ufb01nite \u2102").segments == (("finite", "c"),)

    @pytest.mark.parametrize("text", [None, "", "  ", "--- ...", "\u2014"])
    def test_empty_text_has_no_segments(self, text: str | None) -> None:
        assert words(text).segments == ()

    @pytest.mark.parametrize(
        ("text", "segments"),
        [
            ("a.. b", (("a",), ("b",))),
            ("a..b.. c", (("a", "b"), ("c",))),
            ("x?!. y", (("x",), ("y",))),
            ("x?!.y", (("x", "y"),)),
            ("end...", (("end",),)),
        ],
    )
    def test_a_punctuation_run_splits_only_before_white_space(
        self, text: str, segments: tuple[tuple[str, ...], ...]
    ) -> None:
        assert words(text).segments == segments

    def test_a_long_punctuation_run_takes_linear_time(self) -> None:
        """A run of 50000 points used to take tens of seconds (quadratic backtracking)."""
        text = "a" + "." * 50_000 + "b" + ",;" * 25_000 + "cd. e"
        started = time.perf_counter()
        assert words(text).segments == (("a", "b", "cd"), ("e",))
        assert time.perf_counter() - started < 1.0

    def test_label_key_joins_all_words(self) -> None:
        assert label_key("  Chemical-Engineering. ") == "chemical engineering"
        assert label_key("CHEMICAL engineering") == label_key("Chemical Engineering")
        assert label_key("Department of Chemistry") != label_key("Chemistry")


# --- term matcher ----------------------------------------------------------------------------


def matched(terms: list[str], text: str, *, inflect: bool = True) -> tuple[str, ...]:
    return TermMatcher(terms, inflect=inflect).matched(words(text))


class TestTermMatcher:
    @pytest.mark.parametrize(
        ("term", "text"),
        [
            ("bio", "A biography of Ada Lovelace"),
            ("bio", "Symbiotic microbes"),
            ("ai", "Taiwan and the maintenance of trade, he said"),
            ("ai", "Mail and email"),
            ("chem", "Alchemy in early modern Europe"),
            ("physics", "Metaphysics of time"),
            ("math", "Aftermath of the war"),
            ("cell", "Excellence in teaching"),
            ("Boltz", "Boltzmann machines"),
            ("protein design", "protein redesign"),
        ],
    )
    def test_no_match_inside_other_words(self, term: str, text: str) -> None:
        assert matched([term], text) == ()

    @pytest.mark.parametrize(
        ("term", "text"),
        [
            ("AI", "AI for science"),
            ("AI", "Generative ai, explained"),
            ("bio", "Bio and chemistry"),
            ("machine learning", "Machine-learning potentials"),
            ("machine learning", "MACHINE   LEARNING"),
            ("cryo-EM", "Cryo EM of membrane proteins"),
            ("cryo-EM", "cryo-EM"),
            ("de novo", "De-novo design"),
            ("Poincare", "The Poincar\u00e9 conjecture"),
        ],
    )
    def test_whole_words_match_whatever_the_case_and_separators(self, term: str, text: str) -> None:
        assert matched([term], text) == (term,)

    @pytest.mark.parametrize(
        ("term", "text"),
        [
            ("enzyme", "Engineered enzymes"),
            ("galaxy", "Dwarf galaxies"),
            ("process", "Stochastic processes"),
            ("processes", "a stochastic process"),
            ("stochastic differential equations", "a stochastic differential equation"),
            ("diffusion model", "Diffusion models for proteins"),
            ("genomic", "Genomics of aging"),
        ],
    )
    def test_the_last_word_may_be_singular_or_plural(self, term: str, text: str) -> None:
        assert matched([term], text) == (term,)

    @pytest.mark.parametrize(
        ("term", "text"),
        [
            ("genomics", "genomic data"),
            ("genetics", "Genetic variation"),
            ("optics", "The optic nerve"),
            ("robotics", "Robotic process automation"),
            ("therapeutics", "A therapeutic approach"),
            ("molecular dynamics", "a molecular dynamic"),
        ],
    )
    def test_a_word_in_ics_is_never_made_singular(self, term: str, text: str) -> None:
        assert matched([term], text) == ()
        assert matched([term], term.upper()) == (term,)

    def test_only_the_last_word_is_inflected(self) -> None:
        assert matched(["protein design"], "proteins design") == ()

    def test_inflection_can_be_switched_off(self) -> None:
        assert matched(["David Baker"], "The David Bakers of science", inflect=False) == ()
        assert matched(["David Baker"], "Talk by David Baker", inflect=False) == ("David Baker",)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Superconductivity in nickelates", True),
            ("superconducting qubits", True),
            ("High-Tc superconductors", True),
            ("Conduct and misconduct", False),
            ("Semiconductor devices", False),
        ],
    )
    def test_a_trailing_star_matches_a_word_prefix(self, text: str, expected: bool) -> None:
        assert bool(matched(["superconduct*"], text)) is expected

    def test_a_star_applies_to_the_last_word_of_a_phrase(self) -> None:
        assert matched(["protein engineer*"], "Protein engineering at scale") == (
            "protein engineer*",
        )
        assert matched(["protein engineer*"], "Engineering proteins") == ()

    def test_a_phrase_never_spans_a_sentence_break(self) -> None:
        assert matched(["protein design"], "We study the protein. Design follows.") == ()
        assert matched(["protein design"], "We study protein design.") == ("protein design",)

    def test_a_phrase_needs_its_words_in_order_and_adjacent(self) -> None:
        assert matched(["number theory"], "Theory of numbers") == ()
        assert matched(["number theory"], "number and theory") == ()

    def test_a_phrase_longer_than_the_segment_does_not_match(self) -> None:
        assert matched(["large language model"], "large language") == ()

    def test_matched_terms_come_in_configuration_order_without_repeats(self) -> None:
        matcher = TermMatcher(["topology", "AI", "protein"])
        assert matcher.matched(words("protein AI topology protein"), words("AI")) == (
            "topology",
            "AI",
            "protein",
        )
        assert matcher.first(words("protein AI")) == "AI"
        assert matcher.first(words("nothing here")) is None

    def test_hits_carry_their_position(self) -> None:
        matcher = TermMatcher(["office hours", "seminar"])
        assert matcher.hits(words("Seminar. Office hours with the seminar speaker")) == (
            Hit(term="seminar", segment=0, start=0, stop=1),
            Hit(term="office hours", segment=1, start=0, stop=2),
            Hit(term="seminar", segment=1, start=4, stop=5),
        )

    def test_hits_overlap_when_terms_overlap(self) -> None:
        matcher = TermMatcher(["language model", "large language model"])
        assert [hit.term for hit in matcher.hits(words("a large language model"))] == [
            "large language model",
            "language model",
        ]

    def test_spellings_that_fold_to_the_same_words_count_once(self) -> None:
        matcher = TermMatcher(["cryo-EM", "Cryo EM", "cryo em"])
        assert matcher.terms == ("cryo-EM",)
        assert matcher.matched(words("cryo em")) == ("cryo-EM",)

    def test_a_star_term_and_a_plain_term_are_different(self) -> None:
        assert TermMatcher(["neuro*", "neuro"]).terms == ("neuro*", "neuro")

    @pytest.mark.parametrize("term", ["***", "--", "ab*", "x *"])
    def test_terms_that_cannot_match_are_configuration_errors(self, term: str) -> None:
        with pytest.raises(ConfigError, match="keywords"):
            TermMatcher(["fine", term], label="keywords")

    def test_empty_matcher_matches_nothing(self) -> None:
        matcher = TermMatcher([])
        assert matcher.terms == ()
        assert matcher.matched(words("anything at all")) == ()


# --- field tagger ----------------------------------------------------------------------------


TAXONOMY = Taxonomy.model_validate(
    {
        "fields": {
            "math": {
                "keywords": {"number theory": 1.0, "probability": 0.5, "statistics": 0.5},
                "series": ["Arithmetic Seminar"],
                "exclude_keywords": ["math education"],
            },
            "ai-ml": {"keywords": {"machine learning": 1.0, "AI": 1.0}},
            "chem": {"keywords": {"catalysis": 1.0}, "series": ["Chemistry Colloquium"]},
            "bio": {"keywords": {"protein": 1.0, "cell": 0.5}, "min_score": 1.5},
            "physics": {
                "keywords": {"physics": 1.0},
                "exclude_keywords": ["physics education"],
            },
        },
        "source_defaults": {"localist-medicine": ["med"]},
        "departments": {"Chemical Engineering": ["chem"], "Bioengineering": ["bio"]},
    }
)


@pytest.fixture
def tagger() -> RuleFieldTagger:
    return RuleFieldTagger(TAXONOMY)


class TestFieldTaggerFactory:
    def test_the_factory_is_registered_and_builds_a_field_tagger(self) -> None:
        build: FieldTaggerFactory = load_implementation("field_tagger")
        assert build is build_field_tagger
        assert isinstance(build(TAXONOMY), FieldTagger)

    def test_an_empty_taxonomy_decides_nothing(self) -> None:
        decision = build_field_tagger(Taxonomy()).tag(candidate("Machine learning"))
        assert decision == TagDecision(
            fields=[], method="rules", needs_llm=True, reasons=["no field matched"]
        )

    def test_a_bad_term_is_reported_with_its_place(self) -> None:
        taxonomy = Taxonomy.model_validate({"fields": {"cs": {"keywords": ["ok", "?!"]}}})
        with pytest.raises(ConfigError, match=r"taxonomy fields\.cs\.keywords"):
            build_field_tagger(taxonomy)


class TestSourceFields:
    def test_source_defaults_alone_decide(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("TBA", default_fields=["bio"]))
        assert decision == TagDecision(
            fields=["bio"], method="source", reasons=["source default: bio"]
        )

    def test_field_hints_are_source_fields(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("TBA", field_hints=["physics", "chem"]))
        assert decision.fields == ["chem", "physics"]
        assert decision.method == "source"
        assert decision.reasons == ["source hint: chem, physics"]

    def test_the_taxonomy_source_map_adds_fields(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("TBA", source_ids=["localist-medicine", "other-x"]))
        assert decision.fields == ["med"]
        assert decision.method == "source"
        assert decision.reasons == ["source map localist-medicine: med"]

    def test_departments_match_categories_ignoring_case_and_separators(
        self, tagger: RuleFieldTagger
    ) -> None:
        decision = tagger.tag(
            candidate("TBA", categories=["chemical-engineering", "Lecture", "BIOENGINEERING"])
        )
        assert decision.fields == ["chem", "bio"]
        assert decision.method == "source"
        assert decision.reasons == [
            'department "Chemical Engineering": chem',
            'department "Bioengineering": bio',
        ]

    def test_a_department_must_match_the_whole_label(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("TBA", categories=["Chemical Engineering Alumni Club"]))
        assert decision.fields == []
        assert decision.needs_llm

    def test_a_free_tag_never_counts_as_a_department(self, tagger: RuleFieldTagger) -> None:
        # Anyone who posts an event may tag it: a talk on the history of astronomy
        # tagged "mathematics" is not a talk in mathematics.
        decision = tagger.tag(candidate("TBA", tags=["Chemical Engineering", "Bioengineering"]))
        assert decision.fields == []
        assert decision.needs_llm
        assert not any("department" in reason for reason in decision.reasons)

    def test_rules_add_fields_to_the_source_fields(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("Machine learning for catalysis", default_fields=["chem"]))
        assert decision.fields == ["ai-ml", "chem"]
        assert decision.method == "rules"
        assert decision.reasons == [
            "source default: chem",
            'ai-ml: keywords "machine learning" (1)',
            'chem: keywords "catalysis" (1)',
        ]
        assert not decision.needs_llm

    def test_rules_that_only_confirm_the_source_keep_method_source(
        self, tagger: RuleFieldTagger
    ) -> None:
        decision = tagger.tag(candidate("Catalysis", default_fields=["chem"]))
        assert decision.fields == ["chem"]
        assert decision.method == "source"


class TestRuleFields:
    def test_keywords_in_title_and_series_count(self, tagger: RuleFieldTagger) -> None:
        for place in ("title", "series"):
            text = {"title": "TBA", place: "Advances in number theory"}
            decision = tagger.tag(candidate(**text))
            assert decision.fields == ["math"], place
            assert decision.method == "rules"
            assert not decision.needs_llm

    def test_weak_keywords_need_company(self, tagger: RuleFieldTagger) -> None:
        assert tagger.tag(candidate("Probability")).fields == []
        decision = tagger.tag(candidate("Probability and statistics"))
        assert decision.fields == ["math"]
        assert decision.reasons == ['math: keywords "probability", "statistics" (1)']

    def test_each_term_counts_once(self, tagger: RuleFieldTagger) -> None:
        assert tagger.tag(candidate("Probability, probability, probability")).fields == []

    def test_min_score_is_per_field(self, tagger: RuleFieldTagger) -> None:
        assert tagger.tag(candidate("Protein")).fields == []
        assert tagger.tag(candidate("Protein in the cell")).fields == ["bio"]

    def test_a_series_implies_its_field(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("TBA", series="Stanford Arithmetic Seminar"))
        assert decision.fields == ["math"]
        assert decision.reasons == ['math: series "Arithmetic Seminar"']

    def test_a_series_name_in_the_title_counts(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("Chemistry Colloquium: Jane Doe"))
        assert decision.fields == ["chem"]
        assert decision.reasons == ['chem: series "Chemistry Colloquium"']

    def test_series_and_keywords_are_both_reported(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("Catalysis", series="Chemistry Colloquium"))
        assert decision.reasons == ['chem: series "Chemistry Colloquium"; keywords "catalysis" (1)']

    def test_several_fields_at_once(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(
            candidate(
                "AI and number theory",
                abstract="Physics of protein cell walls.",
                default_fields=["chem"],
            )
        )
        assert decision.fields == ["math", "ai-ml", "chem", "bio", "physics"]


ABSTRACT_ONLY = "not enough in title and series (no source field)"


class TestAbstract:
    """The abstract decides only for events with source fields."""

    def test_with_source_fields_the_abstract_adds_fields(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("TBA", abstract="Number theory.", default_fields=["bio"]))
        assert decision.fields == ["math", "bio"]
        assert decision.method == "rules"
        assert decision.reasons == ["source default: bio", 'math: keywords "number theory" (1)']
        assert not decision.needs_llm

    def test_without_source_fields_the_abstract_alone_is_left_to_the_llm(
        self, tagger: RuleFieldTagger
    ) -> None:
        decision = tagger.tag(candidate("A book talk", abstract="A speaker trained in physics."))
        assert decision == TagDecision(
            fields=[],
            method="rules",
            needs_llm=True,
            reasons=[f'physics: keywords "physics" (1), {ABSTRACT_ONLY}', "no field matched"],
        )

    def test_weak_title_evidence_is_not_completed_by_the_abstract(
        self, tagger: RuleFieldTagger
    ) -> None:
        decision = tagger.tag(candidate("Probability", abstract="And statistics."))
        assert decision.fields == []
        assert decision.needs_llm
        assert decision.reasons[0] == (
            f'math: keywords "probability", "statistics" (1), {ABSTRACT_ONLY}'
        )

    def test_a_title_field_stands_and_the_abstract_only_field_is_reported(
        self, tagger: RuleFieldTagger
    ) -> None:
        decision = tagger.tag(candidate("Number theory", abstract="Uses of catalysis."))
        assert decision.fields == ["math"]
        assert not decision.needs_llm
        assert decision.reasons == [
            'math: keywords "number theory" (1)',
            f'chem: keywords "catalysis" (1), {ABSTRACT_ONLY}',
        ]

    def test_a_series_name_decides_without_the_abstract(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("TBA", series="Chemistry Colloquium", abstract="AI"))
        assert decision.fields == ["chem"]
        assert decision.reasons == [
            f'ai-ml: keywords "AI" (1), {ABSTRACT_ONLY}',
            'chem: series "Chemistry Colloquium"',
        ]

    def test_a_veto_in_the_abstract_still_counts(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("Physics today", abstract="On physics education."))
        assert decision.fields == []
        assert decision.needs_llm
        assert decision.reasons == ['physics: vetoed by "physics education"', "no field matched"]


OVERLAPS = Taxonomy.model_validate(
    {
        "fields": {
            "math": {"keywords": {"statistical": 0.25, "statistical learning": 0.5}},
            "med": {
                "keywords": {
                    "health": 0.5,
                    "public health": 1.0,
                    "drug": 0.5,
                    "drug discovery": 0.5,
                }
            },
        }
    }
)


class TestOverlappingKeywords:
    """A word counts for one keyword of a field: weak terms cannot add up inside one phrase."""

    @pytest.fixture
    def tagger(self) -> RuleFieldTagger:
        return RuleFieldTagger(OVERLAPS)

    @pytest.mark.parametrize(
        "title", ["Drug discovery", "Statistical learning", "Drug discovery in public"]
    )
    def test_a_phrase_and_its_own_words_count_once(
        self, tagger: RuleFieldTagger, title: str
    ) -> None:
        assert tagger.tag(candidate(title)).fields == []

    def test_the_heaviest_overlapping_keyword_counts(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("Public health"))
        assert decision.fields == ["med"]
        assert decision.reasons == ['med: keywords "public health" (1)']

    def test_among_equal_weights_the_longest_counts(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("Drug discovery and public health"))
        assert decision.reasons == ['med: keywords "public health", "drug discovery" (1.5)']

    def test_separate_mentions_still_add_up(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("Drug discovery: a new drug", default_fields=["bio"]))
        assert decision.reasons == [
            "source default: bio",
            'med: keywords "drug", "drug discovery" (1)',
        ]

    def test_overlaps_are_resolved_per_text(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(
            candidate("Drug discovery", abstract="Drugs for all.", default_fields=["bio"])
        )
        assert "med" in decision.fields


class TestVetoes:
    def test_a_veto_with_source_fields_drops_only_the_rule_field(
        self, tagger: RuleFieldTagger
    ) -> None:
        decision = tagger.tag(candidate("Physics education research", default_fields=["chem"]))
        assert decision.fields == ["chem"]
        assert decision.method == "source"
        assert not decision.needs_llm
        assert decision.reasons == [
            "source default: chem",
            'physics: vetoed by "physics education"',
        ]

    def test_a_veto_without_source_fields_asks_the_llm(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("Physics education and machine learning"))
        assert decision.fields == ["ai-ml"]
        assert decision.method == "rules"
        assert decision.needs_llm
        assert decision.reasons == [
            'ai-ml: keywords "machine learning" (1)',
            'physics: vetoed by "physics education"',
        ]

    def test_a_veto_never_removes_a_source_field(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("Physics education", default_fields=["physics"]))
        assert decision.fields == ["physics"]
        assert decision.method == "source"
        assert not decision.needs_llm

    def test_a_veto_overrides_a_series(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(
            candidate("Math education in middle school", series="Arithmetic Seminar")
        )
        assert decision.fields == []
        assert decision.needs_llm
        assert decision.reasons == ['math: vetoed by "math education"', "no field matched"]

    def test_a_veto_without_evidence_is_not_a_conflict(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("Math education", default_fields=["bio"]))
        assert decision.reasons == ["source default: bio"]


class TestDecisions:
    def test_nothing_found_asks_the_llm(self, tagger: RuleFieldTagger) -> None:
        decision = tagger.tag(candidate("Democracy in the age of empires"))
        assert decision == TagDecision(
            fields=[], method="rules", needs_llm=True, reasons=["no field matched"]
        )

    def test_the_same_candidate_gives_the_same_decision(self, tagger: RuleFieldTagger) -> None:
        item = candidate("AI and number theory", categories=["Bioengineering"])
        assert tagger.tag(item) == tagger.tag(item) == RuleFieldTagger(TAXONOMY).tag(item)

    def test_the_input_is_not_changed(self, tagger: RuleFieldTagger) -> None:
        item = candidate("AI and number theory", default_fields=["bio"])
        before = item.model_dump()
        tagger.tag(item)
        assert item.model_dump() == before
