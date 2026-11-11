"""The drupal_jsonapi adapter: requests, pagination and the mapping of H&S event nodes."""

from __future__ import annotations

import functools
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

import pytest

from factories import make_document, make_payload, make_source, make_window
from fakes import FakeHttpClient
from seminars.config import ConfigError
from seminars.config.loader import load_sources
from seminars.http import offline_client
from seminars.models import EventDraft, RawPayload, SourceConfig, TimeWindow
from seminars.sources import base
from seminars.sources.base import AdapterError, ForeignHostError, ParseError
from seminars.sources.generic.drupal_jsonapi import (
    MAX_PATTERN_INPUT,
    DrupalJsonApiAdapter,
    drupal_params,
    first_page_url,
    split_speaker_label,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
PACIFIC = ZoneInfo("America/Los_Angeles")
RECORDED = datetime(2026, 11, 11, 15, 42, tzinfo=UTC)
"""Close to the time the fixtures were recorded; its run window is the recorded one."""

SOURCES = (
    "sites-mathematics",
    "sites-mrc",
    "sites-statistics",
    "sites-chemistry",
    "sites-physics",
    "sites-biology",
    "bayarea-litp",
)
SITE = "https://history.example.edu"
ENDPOINT = f"{SITE}/jsonapi/node/hs_event"


@functools.cache
def configured(source_id: str) -> SourceConfig:
    sources = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
    return sources[source_id]


@functools.cache
def recorded_drafts(source_id: str) -> tuple[list[str], list[EventDraft]]:
    """The requests and drafts of one run of the source on its fixtures (computed once)."""
    source = configured(source_id)
    http = offline_client(source_id)
    adapter = DrupalJsonApiAdapter()
    payload = adapter.fetch(http, source, window=make_window(RECORDED))
    return http.requests, adapter.parse(payload, source)


def by_title(drafts: list[EventDraft], start: str) -> EventDraft:
    (draft,) = [draft for draft in drafts if (draft.title or "").startswith(start)]
    return draft


def example_source(**params: Any) -> SourceConfig:
    """A source of the same kind on a site nobody configured: YAML only, no code."""
    return make_source(
        id="sites-history",
        name="Example History",
        adapter="drupal_jsonapi",
        url=ENDPOINT,
        host_org="Example History",
        default_fields=[],
        params={**configured("sites-mathematics").params, **params},
    )


def node(identifier: str = "n1", **attributes: Any) -> dict[str, Any]:
    """A ``node--hs_event`` resource of a one-hour talk on 2026-11-17 at 16:00 PST."""
    relationships = attributes.pop("relationships", {})
    values = {
        "title": "A talk",
        "path": {"alias": "/events/a-talk"},
        "body": {"value": "<p>Abstract</p>", "processed": "<p>Abstract</p>"},
        "field_hs_event_date": span("2026-11-18T00:00", "2026-11-18T01:00", duration=60),
        "field_hs_event_location": "Room 101",
        **attributes,
    }
    return {
        "type": "node--hs_event",
        "id": identifier,
        "attributes": values,
        "relationships": {name: {"data": data} for name, data in relationships.items()},
    }


def span(start: str, end: str | None = None, **extra: Any) -> dict[str, Any]:
    """A Smart Date value from UTC times written as ``YYYY-MM-DDTHH:MM``."""
    return {"value": f"{start}:00+00:00", "end_value": end and f"{end}:00+00:00", **extra}


def utc(day: int, hour: int) -> datetime:
    """A time in November 2026, UTC."""
    return datetime(2026, 11, day, hour, 0, tzinfo=UTC)


PST_4PM_NOV_2: Final = datetime(2026, 11, 2, 16, 0, tzinfo=PACIFIC)


def term(kind: str, identifier: str, **attributes: Any) -> dict[str, Any]:
    return {"type": kind, "id": identifier, "attributes": attributes}


def link(kind: str, identifier: str) -> dict[str, str]:
    return {"type": kind, "id": identifier}


def answer(data: list[Any], included: list[Any] | None = None, next_href: Any = None) -> str:
    links = {"self": {"href": ENDPOINT}}
    if next_href is not None:
        links["next"] = {"href": next_href}
    return json.dumps({"jsonapi": {}, "data": data, "included": included or [], "links": links})


def parse_answer(text: str, source: SourceConfig | None = None) -> list[EventDraft]:
    source = source or example_source()
    document = make_document(url=ENDPOINT, text=text, content_type="application/vnd.api+json")
    return DrupalJsonApiAdapter().parse(make_payload(document, source_id=source.id), source)


def parse_nodes(*nodes: Any, included: list[Any] | None = None, **params: Any) -> list[EventDraft]:
    return parse_answer(answer(list(nodes), included), example_source(**params))


# --- registration --------------------------------------------------------------------------


def test_the_module_registers_the_drupal_jsonapi_adapter() -> None:
    assert isinstance(base.get_adapter("drupal_jsonapi"), DrupalJsonApiAdapter)


# --- the recorded sites --------------------------------------------------------------------


class TestRecordedSites:
    @pytest.mark.parametrize(
        ("source_id", "requests", "drafts"),
        [
            ("sites-mathematics", 2, 56),
            ("sites-mrc", 1, 2),
            ("sites-statistics", 1, 10),
            ("sites-chemistry", 1, 19),
            ("sites-physics", 1, 7),
            ("sites-biology", 1, 8),
            ("bayarea-litp", 1, 9),
        ],
    )
    def test_every_source_yields_drafts(self, source_id: str, requests: int, drafts: int) -> None:
        sent, found = recorded_drafts(source_id)
        assert len(sent) == requests
        assert len(found) == drafts
        assert {draft.source_id for draft in found} == {source_id}
        assert all(draft.native_id for draft in found)
        assert all(isinstance(draft.start, datetime | date) for draft in found)

    @pytest.mark.parametrize("source_id", SOURCES)
    def test_the_first_request_is_the_documented_one(self, source_id: str) -> None:
        sent, _ = recorded_drafts(source_id)
        parts = urlsplit(sent[0])
        query = dict(parse_qsl(parts.query))
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == configured(source_id).url
        assert query["page[limit]"] == "50"
        assert query["sort"] == "field_hs_event_date.value,drupal_internal__nid"
        assert query["include"].split(",")[0] == "field_hs_event_speaker"
        assert query["filter[start][condition][path]"] == "field_hs_event_date.value"
        assert query["filter[start][condition][operator]"] == ">="
        assert query["filter[start][condition][value]"] == "1793170800"  # 2026-10-28 00:00 PDT
        assert query["filter[end][condition][operator]"] == "<"
        assert query["filter[end][condition][value]"] == "1804838400"  # 2027-03-12 00:00 PST
        assert query["fields[taxonomy_term--hs_event_status]"] == "name"
        assert "title" in query["fields[node--hs_event]"].split(",")

    def test_links_next_is_followed(self) -> None:
        sent, drafts = recorded_drafts("sites-mathematics")
        page1 = json.loads((FIXTURES / "sites-mathematics" / "hs-event-page1.json").read_text())
        assert sent[1] == page1["links"]["next"]["href"]
        assert by_title(drafts, "Popa-Schnell II").series == "Student Algebraic Geometry"

    def test_the_litp_request_asks_for_the_affiliation_attribute(self) -> None:
        sent, _ = recorded_drafts("bayarea-litp")
        fields = dict(parse_qsl(urlsplit(sent[0]).query))["fields[node--hs_event]"]
        assert fields.split(",")[-1] == "custm_speaker_affiliation"

    def test_mathematics(self) -> None:
        _, drafts = recorded_drafts("sites-mathematics")
        draft = by_title(drafts, "Intermittent Weak Solutions of the 3D Euler Equations")
        assert draft.native_id == "037b3f59-4b3c-4945-aa06-fa33de5ac0f8"
        assert draft.start == datetime(2026, 10, 28, 19, 0, tzinfo=UTC)
        assert draft.start == datetime(2026, 10, 28, 12, 0, tzinfo=PACIFIC)
        assert draft.end == datetime(2026, 10, 28, 13, 30, tzinfo=PACIFIC)
        assert [(s.name, s.affiliation) for s in draft.speakers] == [
            ("Matthew Novack", "Notre Dame University")
        ]
        assert draft.location == "384H"
        assert draft.series == "Applied Math"
        assert draft.event_type_hint == "seminar"
        assert draft.url == (
            "https://mathematics.stanford.edu/events/intermittent-weak-solutions-3d-euler-equations"
        )
        assert draft.abstract_html is not None
        assert draft.abstract_html.startswith("<p>")
        assert draft.status_hint is None
        assert draft.extraction == "api"

    def test_mathematics_series_win_over_type_terms(self) -> None:
        _, drafts = recorded_drafts("sites-mathematics")
        colloquium = by_title(drafts, "How to construct a quantum Markov chain")
        assert colloquium.series == "Department Colloquium"
        assert colloquium.categories == ["Seminar"]
        assert colloquium.event_type_hint == "colloquium"

    def test_mathematics_trims_names(self) -> None:
        _, drafts = recorded_drafts("sites-mathematics")
        geometry = by_title(drafts, "Quasi-convexity of harmonic map energy functions")
        assert geometry.series == "Geometry"
        lecture = by_title(drafts, "Bergman Distinguished Lecture")
        assert lecture.event_type_hint == "lecture"
        assert by_title(drafts, "Large deviations of Brownian").end is None
        hou = [d for d in drafts if any(s.name == "Thomas Hou" for s in d.speakers)]
        assert [s.affiliation for d in hou for s in d.speakers] == ["Caltech"]

    def test_an_import_of_the_campus_calendar(self) -> None:
        _, drafts = recorded_drafts("sites-mathematics")
        draft = by_title(drafts, "MRC Public Lecture: Tadashi Tokieda")
        assert draft.status_hint == "confirmed"
        assert draft.is_online is False
        assert draft.location is not None
        assert draft.location.startswith("GSB Knight - Arbuckle / Cemex, 641 Knight Way,")
        assert "\r" not in draft.location

    def test_mrc(self) -> None:
        _, drafts = recorded_drafts("sites-mrc")
        draft = by_title(drafts, "Tadashi Tokieda: Surprises from rubbing the wrong way")
        assert draft.start == datetime(2026, 11, 12, 19, 30, tzinfo=PACIFIC)
        assert [(s.name, s.affiliation) for s in draft.speakers] == [
            ("Tadashi Tokieda", "Stanford University")
        ]
        assert draft.location == "CEMEX Auditorium"
        assert draft.series == "Public Lectures"
        assert draft.event_type_hint == "lecture"

    def test_statistics(self) -> None:
        _, drafts = recorded_drafts("sites-statistics")
        draft = by_title(drafts, "Taming the statistical multiverse")
        assert draft.start == datetime(2026, 11, 10, 16, 0, tzinfo=PACIFIC)
        assert draft.end is None
        assert [(s.name, s.affiliation) for s in draft.speakers] == [
            ("Dominik Rothenh\u00e4usler", "Stanford Statistics")
        ]
        assert draft.location == "CoDa E160"
        assert draft.series == "Statistics Seminar"

    def test_statistics_all_day_conference(self) -> None:
        _, drafts = recorded_drafts("sites-statistics")
        draft = by_title(drafts, "2026 Industrial Affiliates Annual Conference")
        assert draft.start == date(2026, 11, 5)
        assert draft.end is None
        assert draft.all_day
        assert draft.series == "Conferences"
        assert draft.event_type_hint == "conference"

    def test_chemistry_reads_the_speaker_from_the_title(self) -> None:
        _, drafts = recorded_drafts("sites-chemistry")
        draft = by_title(drafts, "Physical Chemistry Seminar: Professor Connor Coley, MIT")
        assert draft.start == datetime(2026, 11, 11, 15, 0, tzinfo=PACIFIC)
        assert [(s.name, s.affiliation) for s in draft.speakers] == [("Connor Coley", "MIT")]
        assert draft.series == "Physical Chemistry Seminar"
        assert draft.location == "Sapp Center Lecture Hall (STLC 114)"
        assert draft.title == "Physical Chemistry Seminar: Professor Connor Coley, MIT"
        defense = by_title(drafts, "Thesis Defense: Julisia Chau")
        assert defense.event_type_hint == "defense"

    def test_chemistry_keeps_meeting_links_for_normalization(self) -> None:
        _, drafts = recorded_drafts("sites-chemistry")
        assert any("pwd=REDACTED" in (draft.abstract_html or "") for draft in drafts)

    def test_physics_title_patterns(self) -> None:
        _, drafts = recorded_drafts("sites-physics")
        draft = by_title(drafts, "The Simons Observatory")
        assert draft.start == datetime(2026, 12, 1, 15, 30, tzinfo=PACIFIC)
        assert draft.end == datetime(2026, 12, 1, 16, 30, tzinfo=PACIFIC)
        assert [(s.name, s.affiliation) for s in draft.speakers] == [("Jo Dunkley", None)]
        assert draft.series == "Applied Physics/Physics Colloquium"
        assert draft.location == (
            "Hewlett Teaching Center, 370 Jane Stanford Way, Stanford, CA 94305, 201"
        )
        assert draft.status_hint == "confirmed"
        assert draft.event_type_hint is None
        assert "Lecture/Presentation/Talk" in draft.categories
        defense = by_title(drafts, "DEPARTMENT OF PHYSICS DISSERTATION DEFENSE")
        assert [s.name for s in defense.speakers] == ["Connie Miao"]
        assert defense.location == "Building: Y2E2, Room: 299"
        assert defense.event_type_hint == "defense"

    def test_physics_cancelled_colloquia(self) -> None:
        source = configured("sites-physics")
        manifest = json.loads((FIXTURES / "sites-physics" / "manifest.json").read_text())
        (url,) = [key for key, entry in manifest["entries"].items() if "canceled" in entry["file"]]
        document = offline_client("sites-physics").get(url)
        drafts = DrupalJsonApiAdapter().parse(
            make_payload(document, source_id=source.id, window=make_window(RECORDED)), source
        )
        assert len(drafts) == 3
        assert {draft.status_hint for draft in drafts} == {"cancelled"}
        assert {draft.event_type_hint for draft in drafts} == {"colloquium"}
        (birgeneau,) = [d for d in drafts if [s.name for s in d.speakers] == ["Bob Birgeneau"]]
        assert birgeneau.series == "Colloquium"
        assert birgeneau.title is not None
        assert not birgeneau.title.lower().startswith("canceled")
        assert birgeneau.start == datetime(2025, 1, 21, 15, 30, tzinfo=PACIFIC)

    def test_biology(self) -> None:
        _, drafts = recorded_drafts("sites-biology")
        draft = by_title(drafts, "Targeting the Cell\u2019s Stress Pathways")
        assert draft.title == "Targeting the Cell\u2019s Stress Pathways for Therapeutic Benefit"
        assert draft.start == datetime(2026, 11, 30, 16, 0, tzinfo=PACIFIC)
        assert [(s.name, s.affiliation) for s in draft.speakers] == [("Peter Walter", None)]
        assert draft.series == "Department Seminars"
        assert draft.location is not None
        assert draft.location.startswith("Clark Center, 318 Campus Drive")
        assert draft.abstract_html is None
        defense = by_title(drafts, "Aaron Behr Dissertation Defense")
        assert [s.name for s in defense.speakers] == ["Aaron Behr"]
        assert defense.location == "Clark S360 or on Zoom"
        assert defense.event_type_hint == "defense"

    def test_litp_speakers_and_excluded_imports(self) -> None:
        _, drafts = recorded_drafts("bayarea-litp")
        assert not [d for d in drafts if (d.title or "").startswith("Applied Physics")]
        draft = by_title(drafts, "Can one hear the shape of a black hole singularity?")
        assert draft.start == datetime(2026, 11, 30, 14, 0, tzinfo=PACIFIC)
        assert [(s.name, s.affiliation) for s in draft.speakers] == [
            ("Sean Hartnoll", "University of Cambridge")
        ]
        assert draft.location == "Varian 355"
        assert draft.series == "LITP Monday Colloquia"
        assert draft.event_type_hint == "colloquium"
        zurich = by_title(drafts, "A precision bound for gravitational transseries")
        assert [s.affiliation for s in zurich.speakers] == ["ETH Z\u00fcrich"]

    @pytest.mark.parametrize("source_id", SOURCES)
    def test_every_configured_source_has_valid_params(self, source_id: str) -> None:
        params = drupal_params(configured(source_id))
        assert params.window_filter is not None
        assert params.max_pages == 4


# --- requests and pagination ---------------------------------------------------------------


class TestRequests:
    def test_a_new_site_needs_only_yaml(self) -> None:
        source = example_source()
        window = make_window()
        http = FakeHttpClient()
        url = first_page_url(ENDPOINT, drupal_params(source), window)
        http.add(url, answer([node()]), content_type="application/vnd.api+json")
        adapter = DrupalJsonApiAdapter()
        (draft,) = adapter.parse(adapter.fetch(http, source, window=window), source)
        assert http.requests == [url]
        assert draft.url == f"{SITE}/events/a-talk"
        assert draft.start == datetime(2026, 11, 17, 16, 0, tzinfo=PACIFIC)

    def test_the_url_of_the_first_page(self) -> None:
        window = TimeWindow(
            start=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
            end=datetime(2026, 11, 2, 8, 0, tzinfo=UTC),
        )
        params = drupal_params(
            example_source(
                sort=["a", "b"],
                include=["x"],
                fields={"node--hs_event": ["title", "path"]},
                window_filter={"path": "d.value", "value_format": "iso8601"},
                page_limit=10,
            )
        )
        assert first_page_url(ENDPOINT, params, window) == (
            f"{ENDPOINT}?page[limit]=10&sort=a,b&include=x"
            "&filter[start][condition][path]=d.value"
            "&filter[start][condition][operator]=%3E%3D"
            "&filter[start][condition][value]=2026-11-01T07%3A00%3A00%2B00%3A00"
            "&filter[end][condition][path]=d.value"
            "&filter[end][condition][operator]=%3C"
            "&filter[end][condition][value]=2026-11-02T08%3A00%3A00%2B00%3A00"
            "&fields[node--hs_event]=title,path"
        )

    def test_without_optional_parameters(self) -> None:
        source = make_source(id="sites-history", adapter="drupal_jsonapi", url=f"{ENDPOINT}?x=1")
        assert first_page_url(f"{ENDPOINT}?x=1", drupal_params(source), make_window()) == (
            f"{ENDPOINT}?x=1&page[limit]=50"
        )

    def pages(self, *texts: str) -> FakeHttpClient:
        http = FakeHttpClient()
        source = example_source()
        http.add(first_page_url(ENDPOINT, drupal_params(source), make_window()), texts[0])
        for number, text in enumerate(texts[1:], start=2):
            http.add(f"{ENDPOINT}?page={number}", text)
        return http

    def fetch(self, http: FakeHttpClient, source: SourceConfig | None = None) -> RawPayload:
        return DrupalJsonApiAdapter().fetch(http, source or example_source(), window=make_window())

    def test_pages_are_followed_and_parsed(self) -> None:
        http = self.pages(
            answer([node("a")], next_href=f"{ENDPOINT}?page=2"),
            answer([node("b")]),
        )
        drafts = DrupalJsonApiAdapter().parse(self.fetch(http), example_source())
        assert http.requests[1:] == [f"{ENDPOINT}?page=2"]
        assert [draft.native_id for draft in drafts] == ["a", "b"]

    def test_a_next_link_without_href_ends_the_pages(self) -> None:
        last = json.dumps({"data": [], "links": {"next": {"href": None}}})
        http = self.pages(answer([], next_href=f"{ENDPOINT}?page=2"), last)
        self.fetch(http)
        assert len(http.requests) == 2

    def test_a_relative_next_link_resolves_against_the_page(self) -> None:
        http = self.pages(answer([], next_href="/jsonapi/node/hs_event?page=2"), answer([]))
        self.fetch(http)
        assert http.requests[1] == f"{ENDPOINT}?page=2"

    def test_a_next_link_to_another_host_is_refused(self) -> None:
        http = self.pages(answer([], next_href="https://elsewhere.example.org/jsonapi?page=2"))
        with pytest.raises(ForeignHostError, match=r"elsewhere\.example\.org"):
            self.fetch(http)
        assert len(http.requests) == 1

    def test_a_next_link_to_an_extra_host_is_followed(self) -> None:
        http = self.pages(answer([], next_href="https://mirror.example.org/page2"))
        http.add("https://mirror.example.org/page2", answer([]))
        self.fetch(http, example_source(extra_hosts=["mirror.example.org"]))
        assert http.requests[-1] == "https://mirror.example.org/page2"

    def test_a_repeated_page_is_an_error(self) -> None:
        http = self.pages(
            answer([], next_href=f"{ENDPOINT}?page=2"), answer([], next_href=f"{ENDPOINT}?page=2")
        )
        with pytest.raises(ParseError, match="repeats a page"):
            self.fetch(http)

    def test_more_pages_than_max_pages_fail_the_source(self) -> None:
        texts = [answer([], next_href=f"{ENDPOINT}?page={n + 2}") for n in range(5)]
        http = self.pages(*texts)
        with pytest.raises(AdapterError, match=r"more than max_pages \(4\)"):
            self.fetch(http)
        assert len(http.requests) == 4

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("<!DOCTYPE html><p>Maintenance</p>", "not JSON"),
            (json.dumps({"errors": [{"title": "Bad"}]}), "no data list"),
            (json.dumps([]), "no data list"),
            (answer([], next_href=["x"]), "not a web address"),
            (answer([], next_href="mailto:web@example.edu"), "not a web address"),
        ],
    )
    def test_an_answer_that_cannot_be_read(self, text: str, message: str) -> None:
        with pytest.raises(ParseError, match=message):
            self.fetch(self.pages(text))


# --- mapping -------------------------------------------------------------------------------


SPEAKER = "hs_entity--event_collections__speaker"
SERIES = "node--hs_event_series"
TYPE = "taxonomy_term--hs_event_type"
STATUS = "taxonomy_term--hs_event_status"
CATEGORY = "taxonomy_term--hs_event_category"
AUDIENCE = "taxonomy_term--hs_event_audience"


class TestMapping:
    def test_a_plain_node(self) -> None:
        (draft,) = parse_nodes(node())
        assert draft.native_id == "n1"
        assert draft.title == "A talk"
        assert draft.start == datetime(2026, 11, 18, 0, 0, tzinfo=UTC)
        assert draft.end == datetime(2026, 11, 18, 1, 0, tzinfo=UTC)
        assert draft.location == "Room 101"
        assert draft.abstract_html == "<p>Abstract</p>"
        assert draft.speakers == []
        assert draft.series is None
        assert draft.is_online is None

    def test_relationships_are_resolved_by_type_and_id(self) -> None:
        resource = node(
            relationships={
                "field_hs_event_speaker": [
                    link(SPEAKER, "s1"),
                    link(SPEAKER, "s2"),
                    link(SPEAKER, "gone"),
                ],
                "field_hs_event_series": [link(SERIES, "x1"), link(SERIES, "x2")],
                "field_hs_event_type": [link(TYPE, "t2"), link(TYPE, "t1")],
                "field_hs_event_category": [link(CATEGORY, "c1")],
                "field_hs_event_audience": [link(AUDIENCE, "a1")],
                "field_hs_event_status": link(STATUS, "st"),
            }
        )
        included = [
            term(SPEAKER, "s1", label=" Ada  Lovelace (Analytical Engines, London) "),
            term(SPEAKER, "s2", label="Jane Doe, PhD"),
            term(SERIES, "x1", title="Number  Theory "),
            term(SERIES, "x2", title="Other"),
            term(SERIES, "s1", title="same id, other type"),
            term(TYPE, "t1", name=" Lecture"),
            term(TYPE, "t2", name="  "),
            term(CATEGORY, "c1", name="Class/Seminar"),
            term(AUDIENCE, "a1", name="Everyone"),
            term(STATUS, "st", name="canceled"),
            "junk",
            {"type": TYPE},
        ]
        (draft,) = parse_nodes(
            resource,
            included=included,
            event_type_names={"Lecture": "lecture"},
            status_names={"canceled": "cancelled"},
        )
        assert [(s.name, s.affiliation) for s in draft.speakers] == [
            ("Ada Lovelace", "Analytical Engines, London"),
            ("Jane Doe, PhD", None),
        ]
        assert draft.series == "Number Theory"
        assert draft.categories == ["Lecture", "Class/Seminar", "Everyone"]
        assert draft.event_type_hint == "lecture"
        assert draft.status_hint == "cancelled"

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            (
                "Matthew Novack (Notre Dame University) ",
                ("Matthew Novack", "Notre Dame University"),
            ),
            (
                "Alec Shelley (Stanford, Applied Physics)",
                ("Alec Shelley", "Stanford, Applied Physics"),
            ),
            ("A (B) Smith (Univ (Main) Campus)", ("A (B) Smith", "Univ (Main) Campus")),
            ("Hang Du, MIT", ("Hang Du", "MIT")),
            ("Peter Walter, PhD", ("Peter Walter, PhD", None)),
            ("saehoon", ("saehoon", None)),
            ("(Stanford)", ("(Stanford)", None)),
            ("Name ()", ("Name", None)),
            ("Broken)", ("Broken)", None)),
            ("Fran\u00e7ois Charles (ENS)", ("Fran\u00e7ois Charles", "ENS")),
            (", MIT", (", MIT", None)),
        ],
    )
    def test_speaker_labels(self, label: str, expected: tuple[str, str | None]) -> None:
        assert split_speaker_label(label) == expected

    def test_speaker_affiliation_attribute(self) -> None:
        resource = node(
            custm_speaker_affiliation=" ETH  Zurich ",
            relationships={"field_hs_event_speaker": [link(SPEAKER, "s1")]},
        )
        (draft,) = parse_nodes(
            resource,
            included=[term(SPEAKER, "s1", label="Ji Hoon Lee (not split)")],
            speaker_affiliation_attribute="custm_speaker_affiliation",
        )
        assert [(s.name, s.affiliation) for s in draft.speakers] == [
            ("Ji Hoon Lee (not split)", "ETH Zurich")
        ]

    def test_title_patterns(self) -> None:
        params = {
            "title_patterns": [
                r"^Never: (?P<title>.+)$",
                r'^(?P<series>[^:]+):\s*(?P<speaker>[^-]+?)\s*\((?P<affiliation>[^)]+)\)\s*-\s*"(?P<title>.+)"$',
            ],
            "event_type_names": {"Algebra Seminar": "seminar"},
        }
        (draft,) = parse_nodes(
            node(title='Algebra  Seminar: Emmy Noether (Erlangen) - "Rings"'), **params
        )
        assert draft.title == "Rings"
        assert draft.series == "Algebra Seminar"
        assert [(s.name, s.affiliation) for s in draft.speakers] == [("Emmy Noether", "Erlangen")]
        assert draft.event_type_hint == "seminar"

    def test_relationships_win_over_title_patterns(self) -> None:
        resource = node(
            title="Talks: Someone Else",
            relationships={
                "field_hs_event_speaker": [link(SPEAKER, "s1")],
                "field_hs_event_series": [link(SERIES, "x1")],
            },
        )
        (draft,) = parse_nodes(
            resource,
            included=[term(SPEAKER, "s1", label="Ada"), term(SERIES, "x1", title="Real Series")],
            title_patterns=[r"^(?P<series>[^:]+): (?P<speaker>.+)$"],
        )
        assert [s.name for s in draft.speakers] == ["Ada"]
        assert draft.series == "Real Series"
        assert draft.title == "Talks: Someone Else"

    def test_patterns_are_not_applied_to_very_long_titles(self) -> None:
        title = "Series: " + "x" * MAX_PATTERN_INPUT
        (draft,) = parse_nodes(
            node(title=title),
            title_patterns=[r"^(?P<series>[^:]+): (?P<title>.+)$"],
            title_status_pattern=r"(x)",
            title_status_names={"x": "cancelled"},
        )
        assert draft.title == title
        assert draft.series is None
        assert draft.status_hint is None

    @pytest.mark.parametrize(
        ("title", "term_name", "status"),
        [
            ("(CANCELED) Gluing small black holes", None, "cancelled"),
            ("Gluing small black holes (POSTPONED)", None, "cancelled"),
            ("Topology (Cancelled)", "live", "cancelled"),
            ("Better than square-root cancellation", None, None),
            ("A talk", "live", "confirmed"),
            ("A talk", "Published", "confirmed"),
            ("A talk", "unknown term", None),
            ("A talk", None, None),
            ("Canceled: Colloquium", "canceled", "cancelled"),
            ("Maybe (tentative)", None, "tentative"),
        ],
    )
    def test_status_from_the_term_and_the_title(
        self, title: str, term_name: str | None, status: str | None
    ) -> None:
        relationships = {"field_hs_event_status": link(STATUS, "st") if term_name else None}
        included = [term(STATUS, "st", name=term_name)] if term_name else []
        (draft,) = parse_nodes(
            node(title=title, relationships=relationships),
            included=included,
            title_status_names={
                "canceled": "cancelled",
                "cancelled": "cancelled",
                "postponed": "cancelled",
                "tentative": "tentative",
            },
            title_status_pattern=r"(?i)\b(cancell?ed|postponed|tentative)\b",
        )
        assert draft.status_hint == status

    def test_a_status_pattern_without_a_group_uses_the_whole_match(self) -> None:
        (draft,) = parse_nodes(
            node(title="Seminar CANCELLED"),
            title_status_pattern=r"(?i)cancelled",
            title_status_names={"cancelled": "cancelled"},
        )
        assert draft.status_hint == "cancelled"

    @pytest.mark.parametrize(
        ("date_field", "start", "end", "all_day"),
        [
            # 2026-11-05 00:00 to 23:59 PST, duration 1439
            (span("2026-11-05T08:00", "2026-11-06T07:59"), date(2026, 11, 5), None, True),
            # three days, 17 to 19 November
            (
                span("2026-11-17T08:00", "2026-11-20T07:59"),
                date(2026, 11, 17),
                date(2026, 11, 19),
                True,
            ),
            # across the change to standard time: 1 November 00:00 PDT to 23:59 PST
            (span("2026-11-01T07:00", "2026-11-02T07:59"), date(2026, 11, 1), None, True),
            # no end time
            (span("2026-11-03T00:00", "2026-11-03T00:00", duration=0), PST_4PM_NOV_2, None, False),
            (span("2026-11-03T00:00"), datetime(2026, 11, 3, tzinfo=UTC), None, False),
            # midnight to midnight is not all day by this rule, so the times stay
            (span("2026-11-05T08:00", "2026-11-06T08:00"), utc(5, 8), utc(6, 8), False),
            # an end before the start is passed on as the source says
            (span("2026-11-05T08:00", "2026-11-05T07:00"), utc(5, 8), utc(5, 7), False),
            # a naive time stays naive
            ({"value": "2026-11-05T16:00:00"}, datetime(2026, 11, 5, 16, 0), None, False),
        ],
    )
    def test_dates(self, date_field: dict[str, Any], start: Any, end: Any, all_day: bool) -> None:
        (draft,) = parse_nodes(node(field_hs_event_date=date_field))
        assert (draft.start, draft.end, draft.all_day) == (start, end, all_day)

    def test_several_dates_give_several_drafts(self) -> None:
        dates = [
            span("2026-11-18T00:00", "2026-11-18T01:00", rrule_index=0),
            span("2026-11-25T00:00", "2026-11-25T01:00", rrule_index=1),
            span("2026-11-25T00:00", "2026-11-25T01:00", rrule_index=1),
            {"value": "bad", "end_value": None},
            "junk",
        ]
        drafts = parse_nodes(node(field_hs_event_date=dates))
        assert [draft.native_id for draft in drafts] == ["n1/0", "n1/1"]
        assert drafts[1].start == datetime(2026, 11, 24, 16, 0, tzinfo=PACIFIC)

    def test_instances_of_an_import_come_first(self) -> None:
        instances = [span("2026-11-18T00:00", "2026-11-18T01:00"), span("2026-11-19T00:00")]
        drafts = parse_nodes(node(field_hs_event_instances=instances))
        assert [draft.native_id for draft in drafts] == ["n1/0", "n1/1"]
        same = parse_nodes(node(field_hs_event_instances=[instances[0]]))
        assert [draft.native_id for draft in same] == ["n1"]

    @pytest.mark.parametrize(
        ("value", "location"),
        [
            (
                "Hewlett Teaching Center\r\n370 Jane Stanford Way, Stanford, CA 94305\r\n201",
                "Hewlett Teaching Center, 370 Jane Stanford Way, Stanford, CA 94305, 201",
            ),
            ("Bass 122 or on Zoom \r\n", "Bass 122 or on Zoom"),
            ("  ", None),
            (None, None),
            (["Room"], None),
        ],
    )
    def test_location(self, value: Any, location: str | None) -> None:
        (draft,) = parse_nodes(node(field_hs_event_location=value))
        assert draft.location == location

    @pytest.mark.parametrize(
        ("experience", "online"),
        [("inperson", False), ("hybrid", True), ("virtual", True), (None, None)],
    )
    def test_experience(self, experience: str | None, online: bool | None) -> None:
        (draft,) = parse_nodes(node(field_hs_event_experience=experience))
        assert draft.is_online is online

    def test_links(self) -> None:
        (draft,) = parse_nodes(
            node(
                path={"alias": "/events/x"},
                field_hs_event_stream={
                    "uri": "https://stanford.zoom.us/j/1?pwd=REDACTED",
                    "title": "",
                },
            )
        )
        assert draft.url == f"{SITE}/events/x"
        assert draft.stream_url == "https://stanford.zoom.us/j/1?pwd=REDACTED"
        (plain,) = parse_nodes(node(path=None, field_hs_event_stream="javascript:alert(1)"))
        assert plain.url is None
        assert plain.stream_url is None

    def test_localist_imports_can_be_excluded(self) -> None:
        imported = node("imp", field_hs_event_link={"uri": "https://events.stanford.edu/event/x"})
        own = node("own", field_hs_event_link={"uri": "https://drive.google.com/poster"})
        kept = parse_nodes(imported, own, exclude_localist_imports=True)
        assert [draft.native_id for draft in kept] == ["own"]
        assert len(parse_nodes(imported, own)) == 2

    @pytest.mark.parametrize(
        "broken",
        [
            node(identifier=""),
            {"type": "node--hs_event", "attributes": {}},
            node(field_hs_event_date=None),
            node(field_hs_event_date={"value": 20261118}),
            node(field_hs_event_date={"value": "tomorrow"}),
            "junk",
        ],
    )
    def test_a_malformed_resource_is_skipped(self, broken: Any) -> None:
        drafts = parse_nodes(broken, node("ok"))
        assert [draft.native_id for draft in drafts] == ["ok"]

    def test_a_resource_whose_draft_cannot_be_built_is_skipped(self) -> None:
        resource = node("bad", relationships={"field_hs_event_speaker": [link(SPEAKER, "s1")]})
        drafts = parse_nodes(
            resource,
            node("ok"),
            included=[term(SPEAKER, "s1", label="Ada")],
            speaker_affiliation_attribute="x",
        )
        assert [draft.native_id for draft in drafts] == ["bad", "ok"]
        broken = node("bad", body={"value": 5}, path={"alias": 7})
        assert [draft.url for draft in parse_nodes(broken)] == [None]

    def test_a_validation_error_skips_the_resource(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from seminars.sources.generic import drupal_jsonapi

        real = drupal_jsonapi.EventDraft

        def picky(**values: Any) -> EventDraft:
            if values.get("title") == "bad":
                raise ValueError("refused")
            return real(**values)

        monkeypatch.setattr(drupal_jsonapi, "EventDraft", picky)
        drafts = parse_nodes(node("a", title="bad"), node("b"))
        assert [draft.native_id for draft in drafts] == ["b"]

    def test_non_ascii_text_is_kept(self) -> None:
        (draft,) = parse_nodes(
            node(title="Campana\u2013P\u0103un I", field_hs_event_location="Caf\u00e9")
        )
        assert draft.title == "Campana\u2013P\u0103un I"
        assert draft.location == "Caf\u00e9"

    def test_html_entities_are_left_to_normalization(self) -> None:
        (draft,) = parse_nodes(node(title="Q&amp;A", body={"value": "<p>&nbsp;x</p>"}))
        assert draft.title == "Q&amp;A"
        assert draft.abstract_html == "<p>&nbsp;x</p>"


# --- params --------------------------------------------------------------------------------


class TestParams:
    @pytest.mark.parametrize(
        ("params", "named"),
        [
            ({"page_size": 50}, "params.page_size"),
            ({"page_limit": 51}, "params.page_limit"),
            (
                {"window_filter": {"path": "x", "value_format": "days"}},
                "params.window_filter.value_format",
            ),
            ({"window_filter": {"path": "x", "unit": "s"}}, "params.window_filter.unit"),
            ({"title_patterns": ["(unclosed"]}, "params.title_patterns.0"),
            ({"title_patterns": ["(?P<room>x)"]}, "params.title_patterns"),
            ({"status_names": {"live": "running"}}, "params.status_names.live"),
            ({"exclude_localist_imports": True, "localist_link_prefix": None}, "params"),
            ({"event_type_names": {"Party": "party"}}, "params.event_type_names.Party"),
        ],
    )
    def test_a_bad_or_unknown_key_is_named(self, params: dict[str, Any], named: str) -> None:
        source = example_source(**params)
        with pytest.raises(ConfigError, match=named.replace(".", r"\.")):
            drupal_params(source)
        with pytest.raises(ConfigError, match="sites-history"):
            DrupalJsonApiAdapter().fetch(FakeHttpClient(), source, window=make_window())
        with pytest.raises(ConfigError):
            DrupalJsonApiAdapter().parse(make_payload(source_id=source.id), source)

    def test_extra_hosts_is_left_to_the_shared_helpers(self) -> None:
        assert drupal_params(example_source(extra_hosts=["a.example.org"])).page_limit == 50

    def test_defaults(self) -> None:
        params = drupal_params(make_source(adapter="drupal_jsonapi"))
        assert (params.page_limit, params.max_pages, params.window_filter) == (50, 4, None)


def test_the_adapter_keeps_no_state() -> None:
    assert vars(DrupalJsonApiAdapter()) == {}
