"""Tests for the heads of a title that tell what the name after them stands for.

A head that honours a person ("In Memoriam", "Tribute", "Celebration of Life") names
the person a memorial is for, not a speaker, so such a title gives no speaker at all.
A head that is only a term or a year ("Spring 2025") is no series either, and an
honorific after it does not vouch for the name: an honorific goes with a person, not
with a speaker.
"""

from __future__ import annotations

import pytest

from seminars.pipeline.speakers import ParsedSpeaker
from seminars.pipeline.title_speakers import speakers_from_title

OBITUARY = "Dr. Jane Doe was a professor of physics at MIT for forty years."


@pytest.mark.parametrize(
    "title",
    [
        "In Memoriam: Dr. Jane Doe",
        "Memorial: Dr. Jane Doe",
        "Tribute: Dr. Jane Doe",
        "A Tribute: Prof. Jane Doe",
        "Remembering: Dr. Jane Doe",
        "Remembrance: Dr. Jane Doe",
        "Celebration of Life: Dr. Jane Doe",
        "A Celebration of Life - Prof. Jane Doe",
        "Memorial Service: Dr. Jane Doe",
        "In Loving Memory | Dr. Jane Doe",
        "In Memoriam: Jane Doe, PhD",
        "In Memoriam: Jane Doe (MIT)",
        "Memorial: Jane Doe in conversation with John Roe",
    ],
)
def test_a_head_that_honours_a_person_gives_no_speaker(title: str) -> None:
    assert speakers_from_title(title) == []
    assert speakers_from_title(title, OBITUARY) == []


@pytest.mark.parametrize(
    "title",
    [
        "Smith Memorial Lecture: Dr. Jane Doe",
        "The Smith Memorial Lecture - Prof. Jane Doe",
        "Celebration: Dr. Jane Doe",
    ],
)
def test_a_memorial_that_names_a_series_keeps_its_speaker(title: str) -> None:
    assert speakers_from_title(title) == [ParsedSpeaker("Jane Doe", None)]


@pytest.mark.parametrize(
    "title",
    [
        "Spring 2025: Dr. Jane Doe",
        "Fall 2025: Prof. Jane Doe",
        "2025: Dr. Jane Doe",
        "Autumn Quarter 2025: Dr. Jane Doe",
        "Winter 2025-26 | Dr. Jane Doe",
        "AY 2025/26 - Professor Jane Doe",
        "Spring 2025: Dr. Jane Doe - Ocean Mixing",
    ],
)
def test_an_honorific_after_a_term_or_a_year_does_not_vouch_for_the_name(title: str) -> None:
    assert speakers_from_title(title) == []


@pytest.mark.parametrize(
    ("title", "abstract", "people"),
    [
        # A degree or an organisation still goes with a person, and so does the abstract.
        ("Spring 2025: Jane Doe, MIT", None, [("Jane Doe", "MIT")]),
        ("Spring 2025: Jane Doe (MIT)", None, [("Jane Doe", "MIT")]),
        ("Spring 2025: Jane Doe, PhD", None, [("Jane Doe", None)]),
        (
            "Spring 2025: Dr. Jane Doe",
            "Dr. Jane Doe is a professor of physics.",
            [("Jane Doe", None)],
        ),
        # A term in front of a series is a series.
        ("Spring 2025 Seminar: Dr. Jane Doe", None, [("Jane Doe", None)]),
        ("Winter Quarter Colloquium: Prof. Jane Doe", None, [("Jane Doe", None)]),
    ],
)
def test_a_term_or_a_year_leaves_the_other_marks_of_a_name(
    title: str, abstract: str | None, people: list[tuple[str, str | None]]
) -> None:
    found = speakers_from_title(title, abstract)
    assert found == [ParsedSpeaker(name, affiliation) for name, affiliation in people]


def test_an_empty_head_is_no_term() -> None:
    assert speakers_from_title("| Dr. Jane Doe") == [ParsedSpeaker("Jane Doe", None)]
