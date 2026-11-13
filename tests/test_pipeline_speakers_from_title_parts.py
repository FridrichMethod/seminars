"""Tests for the evidence that picks the speaker among the parts of a title.

A title after a series can hold two parts that both read like a name ("Seminar: Jane
Roe - Ocean Mixing"), and one of them is the talk title. What tells them apart is a
mark that goes with the name in the title (an honorific, a degree, an organisation)
and how the abstract speaks of each part. An organisation named after a part ("the
Ocean Mixing Lab") shows that the part is a topic or a group, not a person.
"""

from __future__ import annotations

import pytest

from seminars.pipeline.speakers import ParsedSpeaker, speakers_from_title

JANE_ROE = [ParsedSpeaker("Jane Roe", None)]


@pytest.mark.parametrize(
    ("title", "abstract"),
    [
        ("Seminar: Jane Roe - Public Health", "Co-sponsored by the Public Health Department."),
        ("Seminar: Jane Roe - Human Rights", "Organized by the Human Rights Center."),
        ("Geophysics Seminar: Jane Roe - Ice Sheets", "Join the Ice Sheets Lab for a talk."),
        ("Seminar: Jane Roe - Global Health", "The Global Health Foundation supports this talk."),
        ("Seminar: Jane Roe - Ocean Mixing", "Ocean Mixing Lab members will attend."),
        ("Seminar: Jane Roe - Ocean Mixing", "The Ocean Mixing Institute hosts the series."),
        ("Seminar: Jane Roe - Public Health", "Public Health received a new grant."),
    ],
)
def test_an_organisation_named_after_the_talk_title_leaves_the_speaker(
    title: str, abstract: str
) -> None:
    assert speakers_from_title(title, abstract) == JANE_ROE


@pytest.mark.parametrize(
    ("title", "abstract"),
    [
        ("Seminar: Public Health", "Co-sponsored by the Public Health Department."),
        ("Seminar: Human Rights", "Organized by the Human Rights Center."),
        ("Seminar: Public Health - Jane Roe", "Co-sponsored by the Public Health Department."),
        ("Seminar: Ocean Mixing - Jane Roe", "Ocean Mixing Lab members will attend."),
        ("Seminar: Ocean Mixing - Jane Roe", "Sponsored by Ocean Mixing Lab."),
        # Between two parts, an organisation named after one tells only that it is no
        # speaker: a lab is named after a person as well as after a topic.
        ("Seminar: Jane Roe - Ocean Mixing", "Jane Roe Lab members will attend."),
    ],
)
def test_an_organisation_named_after_a_part_does_not_make_it_a_speaker(
    title: str, abstract: str
) -> None:
    assert speakers_from_title(title, abstract) == []


@pytest.mark.parametrize(
    ("title", "abstract"),
    [
        ("Seminar: Jane Roe", "The Jane Roe Lab studies knots."),
        ("Seminar: Jane Roe - Ocean Mixing", "Jane Roe received her PhD at MIT."),
        ("Seminar: Ocean Mixing - Jane Roe", "Jane Roe received her PhD at MIT."),
        ("Seminar: Ocean Mixing - Jane Roe", "Dr. Jane Roe studies the ocean."),
        ("Seminar: Jane Roe - Ocean Mixing", "Jane Roe's group studies the ocean."),
    ],
)
def test_the_abstract_still_names_the_speaker(title: str, abstract: str) -> None:
    assert speakers_from_title(title, abstract) == JANE_ROE


@pytest.mark.parametrize(
    ("title", "people"),
    [
        # An honorific, a degree or an organisation in brackets goes with a person only.
        ("Seminar: Dr. Jane Doe - Ocean Mixing", [("Jane Doe", None)]),
        ("Seminar: Prof. Jane Roe - Ocean Mixing", [("Jane Roe", None)]),
        ("Seminar: Professor Jane Roe - Ocean Mixing", [("Jane Roe", None)]),
        ("Seminar: Jane Roe PhD - Ocean Mixing", [("Jane Roe", None)]),
        ("Grand Rounds: Jane Roe MD - Ocean Mixing", [("Jane Roe", None)]),
        ("Seminar: Jane Roe (MIT) - Ocean Mixing", [("Jane Roe", "MIT")]),
        ("Seminar: Public Health - Jane Roe (MIT)", [("Jane Roe", "MIT")]),
        ("Seminar: Public Health - Dr. Jane Roe", [("Jane Roe", None)]),
        ("Seminar: Ocean Mixing - Jane Roe PhD", [("Jane Roe", None)]),
        ("Seminar | Ocean Mixing in the Arctic | Dr. Jane Roe", [("Jane Roe", None)]),
        # Brackets that name no organisation, a name suffix and nothing at all are no mark:
        # "II" ends a part of a series as well.
        ("Seminar: Jane Roe (she/her) - Ocean Mixing", []),
        ("Seminar: Ocean Mixing II - Jane Roe", []),
        ("Seminar: Jane Roe - Ocean Mixing", []),
    ],
)
def test_a_mark_that_goes_with_a_name_tells_the_speaker(
    title: str, people: list[tuple[str, str | None]]
) -> None:
    found = speakers_from_title(title)
    assert found == [ParsedSpeaker(name, affiliation) for name, affiliation in people]
