"""The department labels of config/taxonomy.yaml agree with the Stanford Events department map.

The owner chose the fields of the calendar's units (2026-11-11), and the map in
config/sources.d/localist.yaml holds those choices. The tagger adds the fields
of every ``taxonomy.departments`` label that equals one of a candidate's
categories to the hints of the source, so a label that both files know must
give the same fields in both, or the taxonomy would add a field the owner did
not choose. The same holds for a unit that the taxonomy spells differently.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Final

import pytest

from seminars.classify.rules import label_key
from seminars.config import Taxonomy, load_taxonomy
from seminars.config.loader import load_sources
from seminars.sources.generic.localist import LocalistParams, localist_params

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

OWNER_CHOICES: Final[dict[str, list[str]]] = {
    "Electrical Engineering": ["cs"],
    "Institute for Computational and Mathematical Engineering (ICME)": ["math"],
    "Stanford Data Science": ["math", "ai-ml"],
    "Materials Science and Engineering": ["chem", "physics"],
    "Wu Tsai Neurosciences Institute": ["bio"],
    "School of Medicine": ["med"],
}
"""Calendar unit -> fields, as the owner answered (the order of the fields does not matter)."""

OTHER_SPELLINGS: Final[dict[str, str]] = {
    "ICME": "Institute for Computational and Mathematical Engineering (ICME)",
    "Institute for Computational and Mathematical Engineering": (
        "Institute for Computational and Mathematical Engineering (ICME)"
    ),
    "Computational and Mathematical Engineering": (
        "Institute for Computational and Mathematical Engineering (ICME)"
    ),
    "Biomedical Data Science": "Department of Biomedical Data Science",
}
"""Taxonomy label -> the calendar's name of the same unit."""


@functools.cache
def calendar() -> LocalistParams:
    sources = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
    return localist_params(sources["localist-stanford"])


@functools.cache
def taxonomy() -> Taxonomy:
    return load_taxonomy(REPO_ROOT / "config" / "taxonomy.yaml")


def calendar_fields() -> dict[str, list[str]]:
    """The calendar's department and group names (stripped) -> fields."""
    params = calendar()
    names = {**params.department_fields, **params.group_fields}
    return {name.strip(): fields for name, fields in names.items()}


@pytest.mark.parametrize(("unit", "fields"), OWNER_CHOICES.items())
def test_the_calendar_map_holds_the_owners_choices(unit: str, fields: list[str]) -> None:
    assert sorted(calendar_fields()[unit]) == sorted(fields)


@pytest.mark.parametrize(
    ("label", "unit"),
    [
        *((unit, unit) for unit in OWNER_CHOICES if unit in taxonomy().departments),
        *((label, unit) for label, unit in OTHER_SPELLINGS.items() if unit in OWNER_CHOICES),
    ],
)
def test_the_taxonomy_gives_the_owners_fields(label: str, unit: str) -> None:
    assert sorted(taxonomy().departments[label]) == sorted(OWNER_CHOICES[unit])


def test_the_owners_units_are_known_to_the_taxonomy() -> None:
    """Every unit of the owner's answer has a taxonomy label, for sources without a map."""
    labels = {label_key(label) for label in taxonomy().departments}
    names = {
        unit: [unit, *(label for label, same in OTHER_SPELLINGS.items() if same == unit)]
        for unit in OWNER_CHOICES
    }
    missing = [unit for unit, spelled in names.items() if not {*map(label_key, spelled)} & labels]
    assert missing == []


def test_labels_in_both_files_give_the_same_fields() -> None:
    by_key = {label_key(name): (name, fields) for name, fields in calendar_fields().items()}
    shared = [
        (label, fields, by_key[label_key(label)])
        for label, fields in taxonomy().departments.items()
        if label_key(label) in by_key
    ]
    assert len(shared) >= 20
    differing = [
        (label, fields, theirs)
        for label, fields, (_, theirs) in shared
        if sorted(fields) != sorted(theirs)
    ]
    assert differing == []


@pytest.mark.parametrize(("label", "unit"), OTHER_SPELLINGS.items())
def test_other_spellings_of_a_calendar_unit_give_its_fields(label: str, unit: str) -> None:
    assert sorted(taxonomy().departments[label]) == sorted(calendar_fields()[unit])
