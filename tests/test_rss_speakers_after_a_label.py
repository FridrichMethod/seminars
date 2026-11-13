"""Speakers that only the text of a detail page names, after a label such as "Speaker Bio:".

The ``rss`` adapter reads them with ``detail_page.speaker_label`` and
``detail_page.speaker_pattern``; the lines and the affiliations below a name
are those of ``seminars.sources.generic.common`` ("Affiliations from the
text"), as for ``html_list``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from factories import make_source, make_window
from fakes import FakeHttpClient
from seminars.config import load_sources
from seminars.models import EventDraft, SourceConfig
from seminars.sources.base import get_adapter
from seminars.sources.generic.common import (
    AdapterConfigError,
    Pattern,
    load_params,
    speakers_after_label,
)
from seminars.sources.generic.rss import RssParams

FEED_URL = "https://dept.example.edu/events/feed"
LIST_URL = "https://dept.example.edu/events"
DETAIL_URL = "https://dept.example.edu/events/talk-one"
WINDOW = make_window(datetime(2026, 11, 11, 20, 0, tzinfo=UTC))
LABEL = re.compile(r"^Speakers?(?: Bio)?:")
REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCES = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}


def systemx_bio() -> tuple[Pattern, Pattern]:
    """The label and the pattern that the source sites-systemx reads its speaker with."""
    detail = load_params(RssParams, SOURCES["sites-systemx"]).detail_page
    assert detail is not None
    assert detail.speaker_label is not None
    assert detail.speaker_pattern is not None
    return detail.speaker_label, detail.speaker_pattern


BIO = systemx_bio()[1]
"""The pattern of a source configured with one: name and affiliation in the first sentence."""


def people(speakers: Any) -> list[tuple[str, str | None]]:
    return [(speaker.name, speaker.affiliation) for speaker in speakers]


class TestTheLinesAfterTheLabel:
    def test_a_sentence_of_the_bio_names_the_speaker_and_the_affiliation(self) -> None:
        lines = [
            "Abstract:",
            "We compute on encrypted data.",
            "Speaker Bio:",
            "Dr. Ir. Jane Doe is a Professor in the group COSIC at KU Leuven. She founded a lab.",
        ]
        assert people(speakers_after_label(lines, LABEL, BIO)) == [("Jane Doe", "KU Leuven")]

    def test_the_rest_of_the_line_of_the_label_comes_first(self) -> None:
        lines = ["Speaker: Jane Doe", "MIT", "Monday, November 16"]
        assert people(speakers_after_label(lines, LABEL, None)) == [("Jane Doe", "MIT")]

    def test_without_a_pattern_the_next_line_is_a_list_of_names(self) -> None:
        lines = ["Speakers:", "Jane Doe and John Roe", "Abstract:", "A talk."]
        assert people(speakers_after_label(lines, LABEL, None)) == [
            ("Jane Doe", None),
            ("John Roe", None),
        ]

    def test_a_name_without_an_affiliation_group_takes_the_lines_below(self) -> None:
        lines = ["Speaker:", "Jane Doe, PhD", "Department of Physics", "Stanford University", ""]
        pattern = re.compile(r"^(?P<speaker>[^,]+)")
        assert people(speakers_after_label(lines, LABEL, pattern)) == [
            ("Jane Doe", "Department of Physics, Stanford University")
        ]

    def test_a_bio_the_pattern_does_not_fit_names_nobody(self) -> None:
        lines = ["Speaker Bio:", "Jane Doe received her PhD from MIT in 2001."]
        assert speakers_after_label(lines, LABEL, BIO) == []

    def test_without_the_label_nobody_is_named(self) -> None:
        lines = ["Abstract:", "Dr. Jane Doe is a Professor at MIT."]
        assert speakers_after_label(lines, LABEL, BIO) == []

    def test_a_label_on_the_last_line_names_nobody(self) -> None:
        assert speakers_after_label(["Abstract", "Speaker:"], LABEL, None) == []

    def test_only_the_first_label_counts(self) -> None:
        lines = ["Speaker:", "Jane Doe", "Speaker:", "John Roe"]
        assert people(speakers_after_label(lines, LABEL, None)) == [("Jane Doe", None)]


class TestTheBioOfASystemXSpeaker:
    """The configured label and pattern of sites-systemx over first sentences of a bio."""

    @pytest.mark.parametrize(
        ("bio", "speaker"),
        [
            pytest.param(
                "Jane Doe is an Assistant Professor of Electrical Engineering at the "
                "University of California, Berkeley.",
                ("Jane Doe", "University of California, Berkeley"),
                id="a university of a state and its campus",
            ),
            pytest.param(
                "John Smith is a Professor at Massachusetts Institute of Technology, where "
                "he leads the Lab for Things.",
                ("John Smith", "Massachusetts Institute of Technology"),
                id="an institute of something",
            ),
            pytest.param(
                "Prof. Ada Byron is a Professor of Computer Science at Stanford University.",
                ("Ada Byron", "Stanford University"),
                id="a university named after a place",
            ),
            pytest.param(
                "Dr. Ir. Ingrid Verbauwhede is a Professor in the research group COSIC at "
                "KU Leuven. She leads a lab.",
                ("Ingrid Verbauwhede", "KU Leuven"),
                id="the recorded bio",
            ),
            pytest.param(
                "Mary Major is a Research Scientist at the Max Planck Institute for "
                "Intelligent Systems (Tuebingen).",
                ("Mary Major", "Max Planck Institute for Intelligent Systems"),
                id="an institute for something",
            ),
            pytest.param(
                "Raj Patel is a PhD student at the Ecole Polytechnique Federale de Lausanne.",
                ("Raj Patel", "Ecole Polytechnique Federale de Lausanne"),
                id="a name with de",
            ),
            pytest.param(
                "Ann Lee is a Professor at the University of Texas at Austin and a member "
                "of its robotics center.",
                ("Ann Lee", "University of Texas at Austin"),
                id="a campus after at, then and a lowercase word",
            ),
            pytest.param(
                "Ann Lee is a Professor at Texas A&M University.",
                ("Ann Lee", "Texas A&M University"),
                id="an ampersand in a word",
            ),
            pytest.param(
                "Ann Lee is a Professor at Texas A&M. She leads a lab.",
                ("Ann Lee", "Texas A&M"),
                id="a capital after an ampersand at the end of the sentence",
            ),
            pytest.param(
                "Ann Lee is a Senior Researcher at NVIDIA Research. She builds chips.",
                ("Ann Lee", "NVIDIA Research"),
                id="a word in capitals before the end of the sentence",
            ),
            pytest.param(
                "Ann Lee is a Professor at MIT.",
                ("Ann Lee", "MIT"),
                id="an acronym at the end of the bio",
            ),
            pytest.param(
                "Ann Lee is a Lecturer at Stanford. She teaches circuits.",
                ("Ann Lee", "Stanford"),
                id="one word before the end of the sentence",
            ),
            pytest.param(
                "Ann Lee is an Associate Professor at Stanford University in the Department "
                "of Electrical Engineering.",
                ("Ann Lee", "Stanford University"),
                id="in and a lowercase word after the affiliation",
            ),
            pytest.param(
                "Ann Lee is a Professor at Stanford Univ.",
                ("Ann Lee", "Stanford Univ"),
                id="an abbreviation at the end of the line",
            ),
            pytest.param(
                "Ann Lee is a Professor at Stanford Univ., where she studies chips.",
                ("Ann Lee", "Stanford Univ"),
                id="an abbreviation before a comma",
            ),
            pytest.param(
                "Ann Lee is a Professor at Stanford Univ.; she studies chips.",
                ("Ann Lee", "Stanford Univ"),
                id="an abbreviation before a semicolon",
            ),
            pytest.param(
                "Ann Lee is a Professor at Stanford Univ. (Palo Alto).",
                ("Ann Lee", "Stanford Univ"),
                id="an abbreviation before a bracket",
            ),
            pytest.param(
                "Ann Lee is a Professor at Mount St.",
                ("Ann Lee", "Mount St"),
                id="the abbreviation St at the end of the line",
            ),
            pytest.param(
                "Ann Lee is a Professor at Washington U.",
                ("Ann Lee", "Washington U"),
                id="a single capital at the end of the line",
            ),
            pytest.param(
                "Ann Lee is a Scientist at Apple Inc. She builds chips.",
                ("Ann Lee", "Apple Inc"),
                id="the abbreviation Inc ends the sentence",
            ),
            pytest.param(
                "Ann Lee is a Postdoc at the Media Lab. She builds chips.",
                ("Ann Lee", "Media Lab"),
                id="the word Lab ends the sentence",
            ),
        ],
    )
    def test_a_bio_names_the_speaker_and_the_whole_affiliation(
        self, bio: str, speaker: tuple[str, str]
    ) -> None:
        label, pattern = systemx_bio()
        assert people(speakers_after_label(["Speaker Bio:", bio], label, pattern)) == [speaker]

    @pytest.mark.parametrize(
        "bio",
        [
            pytest.param("Ann Lee is a Professor at the Univ. of Texas.", id="an abbreviation"),
            pytest.param(
                "Ann Lee is a Professor at Stanford University and Director of the Center "
                "for Integrated Systems.",
                id="and a capitalised word after the affiliation",
            ),
            pytest.param(
                "Ann Lee is a Professor at the Department of Electrical Engineering and "
                "Computer Science.",
                id="and inside the affiliation",
            ),
            pytest.param("Ann Lee is a researcher at heart.", id="no capitalised word after at"),
            pytest.param(
                "Ann Lee is a Professor at St. Jude Children's Research Hospital.",
                id="the abbreviation St",
            ),
            pytest.param(
                "Ann Lee is a Scientist at the U.S. Naval Research Laboratory.",
                id="an abbreviation of single capitals",
            ),
            pytest.param("Ann Lee is a Professor at Mt. Sinai Hospital.", id="the abbreviation Mt"),
            pytest.param(
                "Ann Lee is a Professor at U.C. Berkeley.", id="an abbreviated university"
            ),
            pytest.param(
                "Ann Lee is a Professor at Univ. College London.", id="the abbreviation Univ"
            ),
            pytest.param(
                "Ann Lee is a Professor at Johns Hopkins Univ. School of Medicine.",
                id="the abbreviation Univ inside the affiliation",
            ),
            pytest.param(
                "Ann Lee is a Scientist at Argonne Natl. Laboratory.", id="the abbreviation Natl"
            ),
            pytest.param(
                "Ann Lee is a Scientist at the Allen Inst. Seattle.", id="the abbreviation Inst"
            ),
            pytest.param(
                "Ann Lee is a Lecturer at the EECS Dept. UC Berkeley.", id="the abbreviation Dept"
            ),
        ],
    )
    def test_an_affiliation_the_pattern_cannot_bound_is_left_out(self, bio: str) -> None:
        label, pattern = systemx_bio()
        assert people(speakers_after_label(["Speaker Bio:", bio], label, pattern)) == [
            ("Ann Lee", None)
        ]


def source(**detail: Any) -> SourceConfig:
    params: dict[str, Any] = {
        "feed": {"start": "pubDate", "native_id": "guid"},
        "list_page": {"item": "article.ev", "title": "h3 a", "link": "h3 a", "times": "time"},
        "detail_page": {"fetch": "native_only", "body": ".body", **detail},
    }
    return make_source(
        id="custom-example",
        group="stanford-custom",
        url=FEED_URL,
        urls=[LIST_URL],
        params=params,
    )


def run(config: SourceConfig, body: str, *, card_speaker: str | None = None) -> EventDraft:
    feed = (
        '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>E</title>'
        "<link>https://dept.example.edu/</link><item><title>Talk One</title>"
        "<link>/events/talk-one</link><guid>g1</guid>"
        "<pubDate>Mon, 16 Nov 2026 16:30:00 -0800</pubDate></item></channel></rss>"
    )
    who = f"<div class='who'>{card_speaker}</div>" if card_speaker else ""
    listing = (
        "<html><body><article class='ev'><h3><a href='/events/talk-one'>Talk One</a></h3>"
        f"<time datetime='2026-11-16T16:30:00-08:00'>x</time>{who}</article></body></html>"
    )
    http = FakeHttpClient()
    http.add(FEED_URL, feed, content_type="application/rss+xml")
    http.add(LIST_URL, listing)
    http.add(DETAIL_URL, f"<html><body><div class='body'>{body}</div></body></html>")
    adapter = get_adapter(config.adapter)
    [draft] = adapter.parse(adapter.fetch(http, config, window=WINDOW), config)
    return draft


BODY = (
    "<h2>Abstract:</h2><p>We compute on encrypted data.</p><h2>Speaker Bio:</h2>"
    "<div><img alt='Jane Doe'></div><p>Dr. Jane Doe is a Professor in the group COSIC at "
    "KU Leuven. She founded a lab.</p>"
)


class TestTheAdapter:
    def test_a_draft_without_speakers_takes_those_of_its_page(self) -> None:
        draft = run(source(speaker_label=LABEL.pattern, speaker_pattern=BIO.pattern), BODY)
        assert people(draft.speakers) == [("Jane Doe", "KU Leuven")]
        assert "encrypted data" in (draft.abstract_html or "")

    def test_speakers_of_the_card_come_first(self) -> None:
        config = source(speaker_label=LABEL.pattern, speaker_pattern=BIO.pattern)
        config = config.evolve(
            params={
                **config.params,
                "list_page": {**config.params["list_page"], "subheadline": ".who"},
                "card_fields": {"speakers": "subheadline"},
            }
        )
        draft = run(config, BODY, card_speaker="John Roe")
        assert people(draft.speakers) == [("John Roe", None)]

    def test_without_the_label_the_page_names_nobody(self) -> None:
        assert run(source(), BODY).speakers == []

    @pytest.mark.parametrize(
        ("detail", "message"),
        [
            ({"speaker_pattern": BIO.pattern}, "speaker_pattern needs detail_page.speaker_label"),
            (
                {"speaker_label": LABEL.pattern, "body": None},
                "speaker_label needs detail_page.body",
            ),
            (
                {"speaker_label": LABEL.pattern, "speaker_pattern": r"^(?P<name>.+)$"},
                "needs the group",
            ),
            (
                {"speaker_label": LABEL.pattern, "speaker_pattern": r"^(?P<speaker>.+)(?P<x>.)$"},
                "unknown group",
            ),
        ],
    )
    def test_the_params_fit_together(self, detail: dict[str, Any], message: str) -> None:
        config = source(**detail)
        params = dict(config.params["detail_page"])
        config = config.evolve(
            params={
                **config.params,
                "detail_page": {key: value for key, value in params.items() if value is not None},
            }
        )
        with pytest.raises(AdapterConfigError, match=message):
            run(config, BODY)
