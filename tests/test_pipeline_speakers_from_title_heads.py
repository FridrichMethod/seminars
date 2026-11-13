"""Tests for the heads of a title that tell what the name after them stands for.

A head that honours a person ("In Memoriam", "In Honor of", "Obituary", "Tribute",
"Celebration of Life") names the person a memorial is for, not a speaker, so such a
title gives no speaker at all. Every part in front of the name is such a head, except
inside a quoted talk title; the parts after the first one that starts with a name are
the talk title, quoted or not.
A head that is only a term, a month or a year ("Spring 2025", "Jan. 2025") is no series
either, and an honorific in any part after it does not vouch for the name: an honorific
goes with a person, not with a speaker.
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
        "In Remembrance: Dr. Jane Doe",
        "In Remembrance of: Dr. Jane Doe",
        "In Memory of: Dr. Jane Doe",
        "In Loving Memory of: Dr. Jane Doe",
        "In Honor of: Dr. Jane Doe",
        "In Honour of: Dr. Jane Doe",
        "Honoring: Dr. Jane Doe",
        "Honouring: Dr. Jane Doe",
        "Obituary: Dr. Jane Doe",
        "Obituaries: Dr. Jane Doe",
        "Celebrating the Life of: Dr. Jane Doe",
        "Celebrating Life of: Dr. Jane Doe",
        "Celebration of the Life of: Dr. Jane Doe",
        '"In Memoriam": Dr. Jane Doe',
        # The first separator ends the head even after a quote that is never closed.
        '"In Memoriam: Dr. Jane Doe',
        # A memorial in any part in front of the last one, not only in the first.
        "Physics Colloquium | In Memoriam | Dr. Jane Doe",
        "Seminar Series: Tribute: Prof. Jane Doe",
        "Physics Colloquium | In Honor of | Jane Doe, PhD",
        "Seminar: In Memoriam - Jane Doe (MIT)",
        # An unquoted talk title in front of the name cannot be told from a memorial head.
        "Seminar | In Memoriam: The Last Glacier | Dr. Jane Doe",
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
        "Physics Colloquium | Smith Memorial Lecture | Dr. Jane Doe",
    ],
)
def test_a_memorial_that_names_a_series_keeps_its_speaker(title: str) -> None:
    assert speakers_from_title(title) == [ParsedSpeaker("Jane Doe", None)]


@pytest.mark.parametrize(
    "title",
    [
        # The last part is the talk title, and so is a quoted title: a memorial there is
        # the topic of the talk, not a head.
        "Physics Colloquium: Dr. Jane Doe - In Memoriam",
        'Seminar: Dr. Jane Doe - "Tribute: Songs of the Sea"',
        'Seminar: Dr. Jane Doe, "In Memoriam: The Last Glacier"',
        'Seminar | "In Memoriam: The Last Glacier" | Dr. Jane Doe',
        'Seminar: "Tribute: Songs of the Sea" - Dr. Jane Doe',
        "Seminar | \u201cIn Memoriam: The Last Glacier\u201d | Dr. Jane Doe",
        # After the name, the talk title keeps its memorial head, quoted or not.
        "Seminar: Dr. Jane Doe - In Memoriam: The Last Glacier",
        "Seminar: Dr. Jane Doe - Tribute: Songs of the Sea",
        "Seminar: Dr. Jane Doe - 'Tribute: Songs of the Sea'",
    ],
)
def test_a_memorial_in_the_talk_title_keeps_its_speaker(title: str) -> None:
    assert speakers_from_title(title) == [ParsedSpeaker("Jane Doe", None)]


def test_a_memorial_after_a_name_first_keeps_its_speaker() -> None:
    found = speakers_from_title("Jane Doe (MIT): Tribute - Knots")
    assert found == [ParsedSpeaker("Jane Doe", "MIT")]


@pytest.mark.parametrize(
    "title",
    [
        "Tribute to Euler: Dr. Jane Doe",
        "Seminar: Tribute to Euler - Dr. Jane Doe",
        "Physics Colloquium | Remembering Slavery | Dr. Jane Doe",
        "Seminar | Honoring Indigenous Knowledge | Dr. Jane Doe",
    ],
)
def test_a_memorial_word_inside_a_longer_part_keeps_its_speaker(title: str) -> None:
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
        # The honorific of a later part is no mark either.
        "Spring 2025: Ocean Mixing - Dr. Jane Roe",
        "Spring 2025 | Ocean Mixing | Dr. Jane Roe",
        # A month, with or without a year, is a term as well.
        "January 2025: Dr. Jane Doe",
        "Jan. 2025: Dr. Jane Doe",
        "Jan 2025: Dr. Jane Doe",
        "Sept. 2025 | Prof. Jane Doe",
        "September: Dr. Jane Doe",
        "May 2025 - Dr. Jane Doe",
        "Dec. 2025: Ocean Mixing - Dr. Jane Roe",
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
        ("January 2025: Jane Doe (MIT)", None, [("Jane Doe", "MIT")]),
        ("Spring 2025: Ocean Mixing - Jane Roe, PhD", None, [("Jane Roe", None)]),
        (
            "Spring 2025: Ocean Mixing - Dr. Jane Roe",
            "Dr. Jane Roe is a professor of oceanography.",
            [("Jane Roe", None)],
        ),
        # A term in front of a series is a series.
        ("Spring 2025 Seminar: Dr. Jane Doe", None, [("Jane Doe", None)]),
        ("Winter Quarter Colloquium: Prof. Jane Doe", None, [("Jane Doe", None)]),
        ("January 2025 Colloquium: Dr. Jane Doe", None, [("Jane Doe", None)]),
        ("Seminar: Ocean Mixing - Dr. Jane Roe", None, [("Jane Roe", None)]),
    ],
)
def test_a_term_or_a_year_leaves_the_other_marks_of_a_name(
    title: str, abstract: str | None, people: list[tuple[str, str | None]]
) -> None:
    found = speakers_from_title(title, abstract)
    assert found == [ParsedSpeaker(name, affiliation) for name, affiliation in people]


def test_an_empty_head_is_no_term() -> None:
    assert speakers_from_title("| Dr. Jane Doe") == [ParsedSpeaker("Jane Doe", None)]
