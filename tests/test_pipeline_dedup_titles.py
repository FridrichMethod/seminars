"""Tests for the title of a merged talk when one title wraps the other.

A wrapper is a title that puts words in front of the speaker's name and
the title of the talk after it: 'Biology Seminar Series - Jane Doe, MD
"Protein design"'. It gives way to the title of the talk. A title that
begins with the name of its series and ends with the talk ("Machine
Learning Seminar: Jane Doe, Diffusion") wraps nothing: the talk is the
part after the name, so it stays against the bare name of the series.
"""

from __future__ import annotations

from typing import Any

import pytest

from factories import REFERENCE_NOW, make_event
from seminars.config import DedupSettings
from seminars.models import Candidate, EventHints, Speaker
from seminars.pipeline.dedup import dedupe

SETTINGS = DedupSettings()
SPEAKERS = [Speaker(name="Jane Doe")]


def listing(uid: str, source: str, title: str, **overrides: Any) -> Candidate:
    url = f"https://{source}.example.edu/talks/{uid}"
    values: dict[str, Any] = {
        "uid": uid,
        "source_ids": [f"{source}:{uid}"],
        "url": url,
        "source_urls": [url],
        "title": title,
        "speakers": SPEAKERS,
        "first_seen": REFERENCE_NOW,
        "fields": [],
        "abstract": None,
    }
    event = make_event(**{**values, **overrides})
    return Candidate(event=event, hints=EventHints(source_ids=[source]))


def merged_title(host_title: str, calendar_title: str) -> str:
    host = listing("site-1", "sites-x", host_title)
    calendar = listing("agg-1", "localist-stanford", calendar_title)
    titles = set()
    for order in ((host, calendar), (calendar, host)):
        result = dedupe(list(order), settings=SETTINGS, aggregators={"localist-stanford"})
        (event,) = result.candidates
        titles.add(event.event.title)
    (title,) = titles
    return title


GENERIC_FIRST = [
    (
        "Wu Tsai Neurosciences Seminar",
        "Wu Tsai Neurosciences Seminar: Jane Doe - Synaptic plasticity",
    ),
    (
        "Stanford Cancer Institute Seminar",
        "Stanford Cancer Institute Seminar: Jane Doe - Immunotherapy",
    ),
    (
        "Cancer Biology Program Seminar",
        "Cancer Biology Program Seminar: Jane Doe, Tumor immunology",
    ),
    ("Machine Learning Seminar", "Machine Learning Seminar: Jane Doe, Diffusion"),
    ("Applied Physics Colloquium", "Applied Physics Colloquium: Jane Doe, Materials"),
]


class TestTheTalkFollowsTheName:
    @pytest.mark.parametrize(("generic", "talk"), GENERIC_FIRST)
    def test_a_series_name_in_front_of_the_speaker_wraps_nothing(
        self, generic: str, talk: str
    ) -> None:
        assert merged_title(generic, talk) == talk
        assert merged_title(talk, generic) == talk

    def test_a_talk_title_with_words_of_its_own_after_the_name_stays(self) -> None:
        longer = "Jane Doe: Causal inference in medicine"
        assert merged_title("Causal inference", longer) == longer
        assert merged_title(longer, "Causal inference") == longer

    @pytest.mark.parametrize(
        "wrapper",
        [
            'Biology Seminar Series - Jane Doe, MD "Protein design "',
            "Jane Doe, PhD: Protein design",
            "Prof. Jane Doe - Protein design",
            "Jane Doe (MIT): Protein design",
            "Jane Doe on protein design",
        ],
    )
    def test_a_wrapper_still_gives_way_to_the_talk_title(self, wrapper: str) -> None:
        assert merged_title("Protein design", wrapper) == "Protein design"
        assert merged_title(wrapper, "Protein design") == "Protein design"

    def test_a_title_that_adds_nothing_but_the_name_gives_way_wherever_the_name_is(self) -> None:
        assert merged_title("Protein design", "Protein design - Jane Doe, PhD") == "Protein design"
        assert merged_title(
            "Applied Physics Colloquium: Jane Doe", "Applied Physics Colloquium"
        ) == ("Applied Physics Colloquium")

    def test_a_wrapper_that_adds_as_many_words_as_it_holds_still_stays(self) -> None:
        assert merged_title("Diffusion", "Jane Doe (MIT): Diffusion") == "Jane Doe (MIT): Diffusion"
