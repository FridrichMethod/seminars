"""Names of meetings imply their field (config/taxonomy.yaml, fields.<id>.series).

A series name counts in the series and in the title, so the title of an edition
("ICLR 2027"), of a workshop at a meeting or of a stand at one ("Biohub at
NeurIPS 2026") is placed by the name alone. Names match whole words: RECOMB is
no part of "recombination".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from factories import make_candidate, make_event
from seminars.classify.rules import build_field_tagger
from seminars.config import load_taxonomy
from seminars.models import Candidate, FieldId

REPO_ROOT = Path(__file__).resolve().parents[1]
TAXONOMY = load_taxonomy(REPO_ROOT / "config" / "taxonomy.yaml")
TAGGER = build_field_tagger(TAXONOMY)
MEETINGS: dict[FieldId, tuple[str, ...]] = {
    "ai-ml": ("NeurIPS", "ICML", "ICLR"),
    "bio": ("RECOMB", "ISMB", "Protein Society", "RosettaCon"),
}


def campus(title: str, *, series: str | None = None, abstract: str | None = None) -> Candidate:
    """A candidate of a calendar-wide source: no source fields."""
    event = make_event(
        uid="localist-1",
        title=title,
        series=series,
        abstract=abstract,
        fields=[],
        field_tag_method="source",
    )
    return make_candidate(event, source_ids=["localist-stanford"], default_fields=[])


@pytest.mark.parametrize(
    ("field", "name"), [(field, name) for field, names in MEETINGS.items() for name in names]
)
def test_the_meetings_are_series_of_their_field(field: FieldId, name: str) -> None:
    assert name in TAXONOMY.fields[field].series


@pytest.mark.parametrize(
    ("title", "fields", "series"),
    [
        ("ICLR 2027", ["ai-ml"], "ICLR"),
        ("NeurIPS 2026 recap", ["ai-ml"], "NeurIPS"),
        ("ICML 2027 workshop on computational biology", ["ai-ml", "bio"], "ICML"),
        ("RECOMB 2027", ["bio"], "RECOMB"),
        ("RECOMB-Seq 2027", ["bio"], "RECOMB"),
        ("ISMB/ECCB 2027", ["bio"], "ISMB"),
        ("Protein Society 41st Annual Symposium", ["bio"], "Protein Society"),
        ("RosettaCon 2027", ["bio"], "RosettaCon"),
    ],
)
def test_an_edition_is_placed_by_its_name(title: str, fields: list[str], series: str) -> None:
    decision = TAGGER.tag(campus(title))
    assert decision.fields == fields, decision.reasons
    assert any(f'series "{series}"' in reason for reason in decision.reasons)
    assert not decision.needs_llm


def test_the_name_in_the_series_counts_like_the_title() -> None:
    assert TAGGER.tag(campus("Opening keynote", series="NeurIPS 2026")).fields == ["ai-ml"]


def test_a_source_field_is_kept_and_the_meeting_adds_its_own() -> None:
    event = make_event(title="Biohub at NeurIPS 2026", series=None, abstract=None, fields=[])
    decision = TAGGER.tag(make_candidate(event, source_ids=["bayarea-biohub"]))
    assert decision.fields == ["ai-ml", "bio"]
    assert decision.method == "rules"


@pytest.mark.parametrize(
    "title",
    [
        "Recombination hotspots in meiosis",
        "Recombinant antibodies for imaging",
        "ICME Xpo: industry affiliates day",
        "Society, technology and the law",
        "The Rosetta Stone and its decipherment",
    ],
)
def test_words_that_only_look_like_a_meeting_name_place_nothing(title: str) -> None:
    decision = TAGGER.tag(campus(title))
    names = [name for names in MEETINGS.values() for name in names]
    assert not any(f'series "{name}"' in reason for reason in decision.reasons for name in names)


def test_a_meeting_named_only_in_the_abstract_places_nothing() -> None:
    decision = TAGGER.tag(campus("Opening remarks", abstract="Our papers at NeurIPS and ICML."))
    assert "ai-ml" not in decision.fields
