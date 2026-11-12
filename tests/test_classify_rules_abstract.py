"""What the field rules read of an abstract: the talk, not the speaker or the series."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from factories import make_candidate, make_event
from seminars.classify.rules import RuleFieldTagger, build_field_tagger, talk_text
from seminars.config import Taxonomy, load_taxonomy
from seminars.models import Candidate

REPO_ROOT = Path(__file__).resolve().parents[1]

TALK = "Abstract\nWe study neural circuits of memory in mice."

TAXONOMY = Taxonomy.model_validate(
    {
        "fields": {
            "math": {"keywords": {"mathematics": 1.0}, "exclude_keywords": ["math education"]},
            "bio": {"keywords": {"neural circuit": 1.0}},
            "physics": {"keywords": {"physics": 1.0, "applied physics": 1.0}},
        }
    }
)


@pytest.fixture
def tagger() -> RuleFieldTagger:
    return RuleFieldTagger(TAXONOMY)


def candidate(title: str, abstract: str | None, **hints: Any) -> Candidate:
    event = make_event(
        title=title, abstract=abstract, series=None, fields=[], field_tag_method="source"
    )
    base: dict[str, Any] = {"source_ids": ["custom-hai-events"], "default_fields": ["ai-ml"]}
    return make_candidate(event, **{**base, **hints})


class TestTalkText:
    @pytest.mark.parametrize(
        "heading",
        [
            "Bio",
            "Bio:",
            "Bios",
            "Biography",
            "Speaker Bio",
            "Speaker Biography:",
            "Speaker's Bio",
            "Speakers' Bios",
            "Short bio",
            "Biographical sketch",
            "About the Speaker",
            "About the Speaker:",
            "about the speakers:",
            "About the moderator:",
            "About the Presenters",
            "About the Series",
            "About the Seminar Series:",
            "About the Wu Tsai Neurosciences Seminar Series",
            "About the Center for Neural Data Science Seminar Series",
            "  About this series  ",
        ],
    )
    def test_a_speaker_or_series_section_is_left_out_to_the_end(self, heading: str) -> None:
        text = f"{TALK}\n{heading}\nJane studied physics.\nHost: John Roe"
        assert talk_text(text) == TALK

    def test_a_heading_with_its_text_on_the_same_line_opens_the_section(self) -> None:
        assert talk_text(f"{TALK}\nBio: Jane studied physics.") == TALK
        assert talk_text(f"{TALK}\nAbout the series: Jane hosts it.") == TALK

    @pytest.mark.parametrize(
        "heading",
        [
            "Abstract",
            "Abstract:",
            "Talk Abstract",
            "Summary",
            "Description",
            "Title:",
            "Talk title",
            "About the Seminar",
            "About the talk",
            "About this lecture",
        ],
    )
    def test_a_heading_of_the_talk_ends_the_section(self, heading: str) -> None:
        text = f"Bio\nJane studied physics.\n{heading}\nNeural circuits."
        assert talk_text(text) == f"{heading}\nNeural circuits."

    @pytest.mark.parametrize(
        "line",
        [
            "About the Wu Tsai Neurosciences Seminar SeriesThe series brings physics to brains.",
            "About the SeriesThe first Monday of each month, physicists meet.",
            "About the SpeakerJane Doe studied physics.",
        ],
    )
    def test_an_about_heading_run_into_its_first_sentence_opens_the_section(
        self, line: str
    ) -> None:
        """A page that sets the heading apart without a line break, flattened to one line."""
        assert talk_text(f"{TALK}\n{line}\nMore of it.") == TALK

    def test_a_talk_heading_with_its_text_on_the_same_line_ends_the_section(self) -> None:
        text = "Bio: Jane studied physics.\nAbstract: Neural circuits."
        assert talk_text(text) == "Abstract: Neural circuits."

    def test_every_speaker_section_is_left_out(self) -> None:
        text = "About the speakers:\nJane.\nAbstract\nTalk.\nAbout the moderator:\nJohn."
        assert talk_text(text) == "Abstract\nTalk."

    @pytest.mark.parametrize(
        "line",
        [
            "A biography of Rosalind Franklin",
            "Bio-inspired materials for soft robots",
            "Biographies of genes: what sequencing tells",
            "About the series of experiments we ran: none failed",
            "About the speaker's lab, nothing is known.",
            "BioE Seminar",
            "BioX Fellows present their physics",
            "About the seriesthe lowercase run-on is a sentence",
        ],
    )
    def test_a_sentence_that_starts_like_a_heading_is_no_heading(self, line: str) -> None:
        text = f"{TALK}\n{line}\nMore talk."
        assert talk_text(text) == text

    def test_a_long_line_of_white_space_takes_linear_time(self) -> None:
        text = "Bio" + " " * 50_000 + "x\n" + "About the" + " " * 50_000 + "talk x"
        started = time.perf_counter()
        assert talk_text(text) == text
        assert time.perf_counter() - started < 1.0

    @pytest.mark.parametrize("text", [None, "", "Abstract only."])
    def test_text_without_sections_is_returned_unchanged(self, text: str | None) -> None:
        assert talk_text(text) == text


class TestTagging:
    def test_words_of_a_biography_add_no_field(self, tagger: RuleFieldTagger) -> None:
        abstract = f"{TALK}\nBio\nJane holds a Ph.D. in Applied Physics."
        decision = tagger.tag(candidate("Discovery of neural computations", abstract))
        assert decision.fields == ["ai-ml", "bio"]
        assert not any(reason.startswith("physics") for reason in decision.reasons)

    def test_the_same_words_in_the_talk_add_their_field(self, tagger: RuleFieldTagger) -> None:
        abstract = f"{TALK}\nWe use the physics of light."
        decision = tagger.tag(candidate("Discovery of neural computations", abstract))
        assert decision.fields == ["ai-ml", "bio", "physics"]

    def test_a_series_blurb_adds_no_field(self, tagger: RuleFieldTagger) -> None:
        abstract = (
            f"{TALK}\nAbout the Neural Data Science Seminar Series\n"
            "The series bridges neuroscience, mathematics and physics."
        )
        decision = tagger.tag(candidate("Neural computations", abstract))
        assert decision.fields == ["ai-ml", "bio"]

    def test_a_veto_in_a_biography_vetoes_nothing(self, tagger: RuleFieldTagger) -> None:
        abstract = (
            "Abstract\nNew mathematics of neural circuits.\n"
            "About the Speaker\nJane founded a math education charity."
        )
        decision = tagger.tag(candidate("Neural computations", abstract))
        assert decision.fields == ["math", "ai-ml", "bio"]
        assert not any("vetoed" in reason for reason in decision.reasons)

    def test_the_input_is_not_changed(self, tagger: RuleFieldTagger) -> None:
        abstract = f"{TALK}\nBio\nJane studied physics."
        given = candidate("Neural computations", abstract)
        tagger.tag(given)
        assert given.event.abstract == abstract


NEURAL_TALK = (
    "Abstract\n"
    "The brain is a nonlinear dynamical system whose population activity gives rise to "
    "computations that guide behavior. A data-driven systems neuroscience framework "
    "integrates large-scale neurophysiology, dynamical systems modeling and targeted "
    "perturbations in animal models."
)
CAREER = (
    "Jane Doe is an Assistant Professor of Electrical Engineering and Computer Sciences. "
    "She completed a Ph.D. in Applied Physics and a B.S. in Physics and Mathematics, and "
    "studied computational systems neuroscience."
)
SPEAKER = f"Bio\n{CAREER}"
SERIES = (
    "About the Center for Neural Data Science Seminar Series\n"
    "The series bridges neuroscience and data science, statistics and engineering."
)
WU_TSAI = (
    "Talk title to be announced\n"
    "About the Wu Tsai Neurosciences Seminar SeriesThe seminar series discusses brain "
    "research, from biochemistry to behavior. Topics include advances in human and "
    "translational neuroscience and computational neuroscience."
)


@pytest.fixture(scope="module")
def shipped() -> RuleFieldTagger:
    tagger = build_field_tagger(load_taxonomy(REPO_ROOT / "config" / "taxonomy.yaml"))
    assert isinstance(tagger, RuleFieldTagger)
    return tagger


class TestShippedTaxonomy:
    """config/taxonomy.yaml on the texts of recorded talks (shortened)."""

    def test_the_degrees_of_a_speaker_add_no_field(self, shipped: RuleFieldTagger) -> None:
        """A neuroscience talk of an ai-ml source: physics stood only in the biography."""
        abstract = f"{NEURAL_TALK}\n{SPEAKER}\n{SERIES}"
        decision = shipped.tag(
            candidate("Data-driven Discovery and Control of Neural Computations", abstract)
        )
        assert "physics" not in decision.fields
        assert "cs" not in decision.fields
        assert {"ai-ml", "bio"} <= set(decision.fields)
        without_heading = shipped.tag(
            candidate(
                "Data-driven Discovery and Control of Neural Computations",
                f"{NEURAL_TALK}\n{CAREER}",
            )
        )
        assert "physics" in without_heading.fields

    def test_the_words_of_a_series_blurb_add_no_field(self, shipped: RuleFieldTagger) -> None:
        decision = shipped.tag(
            candidate(
                "Jane Doe",
                WU_TSAI,
                source_ids=["localist-stanford"],
                default_fields=[],
                categories=["Wu Tsai Neurosciences Institute"],
            )
        )
        assert decision.fields == ["bio"]
        assert decision.method == "source"
