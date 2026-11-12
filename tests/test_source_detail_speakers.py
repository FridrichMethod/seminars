"""Speakers and their affiliations that ``html_list`` reads from detail pages.

A list card often names no speaker (Biohub) or only a name (the posts of
Chemical and Systems Biology); the page of the talk has the rest.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from factories import make_source, make_window
from fakes import FakeHttpClient
from seminars.config import load_sources
from seminars.http import offline_client
from seminars.models import EventDraft, FetchedDocument, SourceConfig
from seminars.sources.base import get_adapter
from seminars.sources.generic import html_list
from seminars.sources.generic.common import AdapterConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
SOURCES = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
NOW = datetime(2026, 11, 12, 6, 37, 13, tzinfo=UTC)
WINDOW = make_window(NOW)
LIST_URL = "https://talks.example.edu/seminar/"
TALK_URL = "https://talks.example.edu/talks/one"
GARFIELD = (
    "https://chemsysbio.stanford.edu/events/join-us-csb-special-seminar-open-discussion-"
    "david-garfield-phd-monday-november-16-2026-1100-am-1245-pm-munzer/"
)
SIEBER = (
    "https://chemsysbio.stanford.edu/events/save-the-date-csb-special-seminar-stephan-"
    "sieber-phd-tum-munich-december-3-2026-1230-pm-clark-auditorium/"
)


def run(source: SourceConfig, http: Any) -> list[EventDraft]:
    adapter = get_adapter(source.adapter)
    return adapter.parse(adapter.fetch(http, source, window=WINDOW), source)


def people(draft: EventDraft) -> list[tuple[str, str | None]]:
    return [(speaker.name, speaker.affiliation) for speaker in draft.speakers]


def on_day(drafts: list[EventDraft], day: int, month: int = 11) -> EventDraft:
    [draft] = [
        d
        for d in drafts
        if isinstance(d.start, datetime) and (d.start.month, d.start.day) == (month, day)
    ]
    return draft


def list_page(speaker: str | None = None) -> str:
    who = f"<p class='who'>{speaker}</p>" if speaker else ""
    return (
        "<html><body><ul class='talks'><li><h3><a href='/talks/one'>One</a></h3>"
        f"<span class='date'>2026-11-17</span><span class='time'>4pm - 5pm</span>{who}"
        "</li></ul></body></html>"
    )


def list_source(**detail: Any) -> SourceConfig:
    return make_source(
        id="custom-example",
        group="stanford-custom",
        adapter="html_list",
        url=LIST_URL,
        params={
            "selectors": {
                "item": "ul.talks > li",
                "title": "h3",
                "link": "h3 a",
                "date": ".date",
                "time": ".time",
                "speaker": ".who",
            },
            "link_attribute": "href",
            "detail_page": detail,
        },
    )


def serve(listing: str, detail: str | None = None) -> FakeHttpClient:
    http = FakeHttpClient()
    http.add(LIST_URL, listing)
    if detail is not None:
        http.add(TALK_URL, detail)
    return http


PEOPLE = (
    "<html><body><section class='speakers'>"
    "<div class='person'><span class='org'>NVIDIA</span><h3 class='name'>Simon Chu</h3>"
    "<p>Simon Chu works on protein models.</p></div>"
    "<div class='person'><span class='org'>Anthropic</span><h3 class='name'>Richard Shuai</h3>"
    "</div></section><section class='hosts'><div class='person'><h3 class='name'>Jane Host"
    "</h3></div></section></body></html>"
)


def detail_document(path: Path, url: str) -> FetchedDocument:
    return FetchedDocument(
        url=url,
        final_url=url,
        status=200,
        content_type="text/html",
        text=path.read_text(encoding="utf-8"),
        fetched_at=NOW,
    )


class TestSpeakersOfTheDetailPage:
    def test_the_biohub_webinar_names_its_four_speakers_with_their_affiliations(self) -> None:
        [webinar] = run(SOURCES["bayarea-biohub"], offline_client("bayarea-biohub"))
        assert people(webinar) == [
            ("Allan dos Santos Costa", "Biohub"),
            ("Simon Chu", "NVIDIA"),
            ("Dejun Lin", "NVIDIA"),
            ("Richard Shuai", "Anthropic"),
        ]

    def test_a_biohub_conference_names_its_keynote_but_not_its_organizers(self) -> None:
        # The keynote is a featured person card under "Keynote Speaker"; the
        # "Scientific Organizers" below it use the person cards of the speakers.
        params = html_list.load_params(html_list.HtmlListParams, SOURCES["bayarea-biohub"])
        assert params.detail_page is not None
        path = FIXTURES / "bayarea-biohub" / "events-physics-of-life-2026-4be4bb80.html"
        document = detail_document(path, "https://biohub.org/events/physics-of-life-2026/")
        detail = html_list.read_detail(params.detail_page, document)
        assert [(s.name, s.affiliation) for s in detail.speakers] == [("Ned Wingreen, Ph.D.", None)]
        assert detail.start is not None

    def test_a_row_without_speakers_takes_those_of_its_page(self) -> None:
        source = list_source(
            times=".nothing",
            speaker=".speakers .person",
            speaker_name=".name",
            speaker_affiliation=".org",
            required=False,
        )
        [draft] = run(source, serve(list_page(), PEOPLE))
        assert people(draft) == [("Simon Chu", "NVIDIA"), ("Richard Shuai", "Anthropic")]

    def test_the_speakers_of_the_row_come_first(self) -> None:
        source = list_source(times=".nothing", speaker=".speakers .person", speaker_name=".name")
        [draft] = run(source, serve(list_page("Ada Lovelace"), PEOPLE))
        assert people(draft) == [("Ada Lovelace", None)]

    def test_without_a_name_selector_the_whole_match_is_the_name(self) -> None:
        source = list_source(times=".nothing", speaker=".hosts .name")
        [draft] = run(source, serve(list_page(), PEOPLE))
        assert people(draft) == [("Jane Host", None)]

    @pytest.mark.parametrize("key", ["speaker_name", "speaker_affiliation"])
    def test_a_part_of_a_speaker_needs_the_speaker_selector(self, key: str) -> None:
        source = list_source(times=".nothing", **{key: ".name"})
        with pytest.raises(AdapterConfigError, match=f"{key} needs detail_page.speaker"):
            run(source, serve(list_page(), PEOPLE))


class TestAffiliationsFromTheText:
    def test_the_csb_seminar_reads_the_affiliation_under_the_name(self) -> None:
        http = offline_client("custom-csb-events")
        drafts = run(SOURCES["custom-csb-events"], http)
        assert http.requests == ["https://chemsysbio.stanford.edu/events/", GARFIELD, SIEBER]
        talk = on_day(drafts, 16)
        assert people(talk) == [("David Garfield", "AI for Life Sciences, Partnerships, Anthropic")]

    def test_a_csb_talk_whose_post_cannot_be_fetched_keeps_what_its_title_says(self) -> None:
        # The Sieber post is not recorded: its fetch fails offline.
        drafts = run(SOURCES["custom-csb-events"], offline_client("custom-csb-events"))
        assert len(drafts) == 4
        talk = on_day(drafts, 3, month=12)
        assert people(talk) == [("Stephan Sieber", None)]
        assert talk.location == "Clark Auditorium"

    def test_lines_of_a_paragraph_name_the_speaker_with_middle_initial_and_titles(self) -> None:
        post = (
            "<html><body><article><h2>Seminar, Stephan Sieber, PhD, December 3</h2>"
            "<p>The Department Presents a Special Seminar<br>Prof. Dr. Stephan A. Sieber<br>"
            "Department of Chemistry<br>Technical University Munich (TUM)</p>"
            "<p>Thursday, December 3, 2026<br>12:30 PM - 1:30 PM<br>Clark Auditorium</p>"
            "</article></body></html>"
        )
        source = list_source(body="article > div, article > p", required=False)
        [draft] = run(source, serve(list_page("Stephan Sieber"), post))
        assert people(draft) == [
            ("Stephan Sieber", "Department of Chemistry, Technical University Munich (TUM)")
        ]

    @pytest.mark.parametrize(
        ("after", "affiliation"),
        [
            ("<div>Anthropic</div><div></div><div>Lab of X</div>", "Anthropic"),
            ("<div>Anthropic</div><div>Monday, November 16, 2026</div>", "Anthropic"),
            ("<div>Anthropic</div><div>November 16, 2026</div>", "Anthropic"),
            ("<div>Anthropic</div><div>11:00 AM - 12:45 PM</div>", "Anthropic"),
            ("<div>Anthropic</div><div>Talk Title: Agents</div>", "Anthropic"),
            ("<div>A</div><div>B</div><div>C</div><div>D</div>", "A, B, C"),
            ("<div>Wednesday, November 18</div>", None),
            ("", None),
        ],
    )
    def test_the_affiliation_is_at_most_three_lines_up_to_a_date_a_label_or_a_gap(
        self, after: str, affiliation: str | None
    ) -> None:
        post = f"<html><body><article><div>Ada Lovelace, PhD</div>{after}</article></body></html>"
        source = list_source(body="article > div", required=False)
        [draft] = run(source, serve(list_page("Ada Lovelace"), post))
        assert people(draft) == [("Ada Lovelace", affiliation)]

    @pytest.mark.parametrize(
        ("article", "affiliation"),
        [
            pytest.param(
                "<div>Jane Doe</div><div>Jane Doe is a professor of chemistry at MIT, where "
                "she ...</div>",
                None,
                id="a-bio-that-names-the-speaker",
            ),
            pytest.param(
                "<div>Jane Doe</div><div>MIT</div><div>She is a professor of chemistry who "
                "has worked on enzymes and their evolution for twenty years now</div>",
                "MIT",
                id="a-long-line",
            ),
            pytest.param(
                "<div>Jane Doe</div><div>MIT</div><div>She studies enzymes.</div>",
                "MIT",
                id="a-sentence",
            ),
            pytest.param(
                "<h2>CSB Seminar: Jane Doe</h2><div>Jane Doe, PhD</div><div>MIT</div>",
                "MIT",
                id="a-heading-that-names-the-speaker",
            ),
            pytest.param(
                "<div>Jane Doe</div><div>MIT</div><div>Jane Doe, PhD</div><div>Harvard</div>",
                "MIT",
                id="the-name-again",
            ),
            pytest.param(
                "<div>Jane Doe</div><div>Abstract</div><div>We study things.</div>",
                None,
                id="an-abstract-heading",
            ),
            pytest.param(
                "<div>Jane Doe</div><div>Speaker Bio</div><div>Jane is at MIT</div>",
                None,
                id="a-bio-heading",
            ),
            pytest.param(
                "<div>Jane Doe</div><div>MIT</div><div>jdoe@mit.example</div>",
                "MIT",
                id="an-e-mail-address",
            ),
            pytest.param(
                "<div>Jane Doe</div><div>MIT</div><div>https://mit.example/jdoe</div>",
                "MIT",
                id="a-link",
            ),
            pytest.param(
                "<div><b>Jane Doe</b><div>MIT</div>Monday, November 16</div>",
                "MIT",
                id="text-after-a-nested-block",
            ),
            pytest.param(
                "<div>Jane Doe<div></div>MIT</div>",
                "MIT",
                id="text-after-an-empty-block",
            ),
        ],
    )
    def test_the_affiliation_ends_where_the_announcement_goes_on(
        self, article: str, affiliation: str | None
    ) -> None:
        post = f"<html><body><article>{article}</article></body></html>"
        source = list_source(body="article", required=False)
        [draft] = run(source, serve(list_page("Jane Doe"), post))
        assert people(draft) == [("Jane Doe", affiliation)]

    def test_a_sentence_that_mentions_the_speaker_is_not_the_line_of_the_name(self) -> None:
        post = (
            "<html><body><article><div>Speaker Bio: Ada Lovelace works on engines at the "
            "Analytical Society of London</div><div>Countess of Lovelace</div>"
            "</article></body></html>"
        )
        source = list_source(body="article > div", required=False)
        [draft] = run(source, serve(list_page("Ada Lovelace"), post))
        assert people(draft) == [("Ada Lovelace", None)]

    def test_an_affiliation_the_row_has_is_kept(self) -> None:
        post = "<html><body><article><div>Ada Lovelace</div><div>Other</div></article></body>"
        source = list_source(body="article > div", required=False)
        source = source.evolve(
            params={
                **source.params,
                "speaker_pattern": r"^(?P<speaker>.+?) \((?P<affiliation>.+)\)$",
            }
        )
        [draft] = run(source, serve(list_page("Ada Lovelace (Cambridge)"), post))
        assert people(draft) == [("Ada Lovelace", "Cambridge")]


@pytest.mark.parametrize(
    ("line", "name", "expected"),
    [
        ("Prof. Dr. Stephan A. Sieber", "Stephan Sieber", True),
        ("Hemamala (Hema) Karunadasa, Stanford", "Hemamala Karunadasa", True),
        ("STEPHAN SIEBER", "Stephan Sieber", True),
        ("Stephanie Sieberts", "Stephan Sieber", False),
        ("Sieber, Stephan", "Stephan Sieber", False),
        ("Madonna", "Madonna", True),
        ("anything", " ", False),
    ],
)
def test_the_line_of_a_name(line: str, name: str, expected: bool) -> None:
    assert html_list.names_line(line, name) is expected


class TestRowsWithoutTheirPage:
    def test_a_row_is_dropped_when_its_required_page_fails(self) -> None:
        source = list_source(times="time")
        http = serve(list_page("Ada Lovelace"))
        assert run(source, http) == []
        assert http.requests == [LIST_URL, TALK_URL]

    def test_a_row_is_kept_when_its_page_only_adds_fields(self) -> None:
        source = list_source(body="article > div", required=False)
        [draft] = run(source, serve(list_page("Ada Lovelace")))
        assert people(draft) == [("Ada Lovelace", None)]
        assert draft.start == datetime(2026, 11, 17, 16)

    def test_a_row_whose_page_is_on_another_host_is_kept_without_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        source = list_source(body="article > div", required=False)
        listing = list_page("Ada Lovelace").replace("/talks/one", "https://other.org/one")
        http = serve(listing)
        with caplog.at_level(logging.WARNING, logger=html_list.__name__):
            [draft] = run(source, http)
        assert http.requests == [LIST_URL]
        assert draft.url == "https://other.org/one"
        assert "1 row(s) in the window kept without it: their detail page is on a host" in (
            caplog.text
        )

    def test_keep_tags_still_drops_a_row_without_its_page(self) -> None:
        source = list_source(tag=".tag", required=False)
        source = source.evolve(params={**source.params, "keep_tags": ["Seminars"]})
        assert run(source, serve(list_page("Ada Lovelace"))) == []
