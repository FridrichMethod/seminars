"""The generic ``jsonld`` adapter: JSON-LD blocks of recorded pages and hand-written ones."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest

from factories import make_source, make_window
from fakes import FakeHttpClient
from seminars.http import offline_client
from seminars.models import EventDraft, SourceConfig
from seminars.sources.base import ParseError, get_adapter
from seminars.sources.generic.html_list import AdapterConfigError
from seminars.sources.generic.jsonld import JsonLdAdapter, decode, find_events

WINDOW = make_window(datetime(2026, 11, 11, 20, 0, tzinfo=UTC))
PST = timezone(timedelta(hours=-8))
PAGE_URL = "https://center.example.edu/events/"


def run(source: SourceConfig, http: Any) -> list[EventDraft]:
    adapter = get_adapter("jsonld")
    return adapter.parse(adapter.fetch(http, source, window=WINDOW), source)


def jsonld_source(url: str = PAGE_URL, **overrides: Any) -> SourceConfig:
    defaults: dict[str, Any] = {
        "id": "custom-jsonld",
        "group": "stanford-custom",
        "adapter": "jsonld",
        "url": url,
    }
    return make_source(**{**defaults, **overrides})


def page(*blocks: Any) -> str:
    scripts = "".join(
        '<script type="application/ld+json">'
        + (block if isinstance(block, str) else json.dumps(block))
        + "</script>"
        for block in blocks
    )
    return f"<html><head>{scripts}</head><body><p>Events</p></body></html>"


def parse_page(*blocks: Any) -> list[EventDraft]:
    http = FakeHttpClient()
    http.add(PAGE_URL, page(*blocks))
    return run(jsonld_source(), http)


def event(**fields: Any) -> dict[str, Any]:
    return {"@type": "Event", "name": "A talk", "startDate": "2026-11-17T16:00:00-08:00", **fields}


# --- recorded pages ------------------------------------------------------------------------


class TestRecordedPages:
    def test_the_adapter_is_registered_under_its_module_name(self) -> None:
        assert isinstance(get_adapter("jsonld"), JsonLdAdapter)

    def test_the_event_of_a_campus_calendar_page(self) -> None:
        url = (
            "https://bioengineering.stanford.edu/events/lecturepresentationtalk/"
            "stanford-energy-seminar-electrified-metamaterial-reactors-process"
        )
        source = jsonld_source(url, id="sites-bioengineering", group="stanford-sites")
        [draft] = run(source, offline_client("sites-bioengineering"))
        assert draft.title == (
            "Stanford Energy Seminar | Electrified metamaterial reactors for process "
            "intensified thermochemistry | Jonathan Fan, Stanford University"
        )
        assert draft.start == datetime(2026, 11, 16, 16, 30, tzinfo=PST)
        assert draft.end == datetime(2026, 11, 16, 17, 20, tzinfo=PST)
        assert draft.location == "Shriram Center, 443 Via Ortega, Stanford, CA 94305"
        assert draft.url == "https://events.stanford.edu/event/stanford-energy-seminar-11162026"
        assert draft.status_hint == "confirmed"
        assert draft.extraction == "jsonld"
        assert (draft.abstract or "").startswith("The electrification of thermochemical")
        assert draft.speakers == []

    @pytest.mark.parametrize(
        ("source_id", "url", "group"),
        [
            (
                "bayarea-biohub",
                "https://biohub.org/events/webinar-esmfold2-structure-prediction-long-context/",
                "bayarea",
            ),
            ("custom-sail-events", "https://ai.stanford.edu/events/", "stanford-custom"),
        ],
    )
    def test_pages_whose_json_ld_describes_no_event(
        self, source_id: str, url: str, group: str
    ) -> None:
        source = jsonld_source(url, id=source_id, group=group)
        assert run(source, offline_client(source_id)) == []


# --- hand-written blocks -------------------------------------------------------------------


class TestContainers:
    def test_graph_arrays_and_item_lists(self) -> None:
        item_list = {
            "@context": "https://schema.org",
            "@type": "ItemList",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "item": event(name="In a list")},
                {"@type": "ListItem", "position": 2, "item": {"@type": "Thing", "name": "x"}},
            ],
        }
        graph = {
            "@context": "https://schema.org",
            "@graph": [{"@type": "WebPage"}, event(name="In a graph")],
        }
        array = [event(name="In an array"), {"@type": "Organization", "name": "Lab"}]
        drafts = parse_page(item_list, graph, array)
        assert [draft.title for draft in drafts] == ["In a list", "In a graph", "In an array"]

    def test_sub_events_and_event_subtypes(self) -> None:
        conference = event(
            **{"@type": ["EducationEvent"]},
            name="Symposium",
            subEvent=[event(name="Session 1", **{"@type": "http://schema.org/SocialEvent"})],
        )
        assert [draft.title for draft in parse_page(conference)] == ["Symposium", "Session 1"]

    def test_one_invalid_block_does_not_lose_the_others(self) -> None:
        drafts = parse_page('{"@type": "Event", "name": broken', event(name="Kept"))
        assert [draft.title for draft in drafts] == ["Kept"]

    def test_only_invalid_blocks_are_a_parse_error(self) -> None:
        with pytest.raises(ParseError, match="is valid JSON"):
            parse_page("{not json")

    def test_a_page_without_json_ld_is_a_parse_error(self) -> None:
        http = FakeHttpClient()
        http.add(PAGE_URL, "<html><body>No data</body></html>")
        with pytest.raises(ParseError, match="no application/ld"):
            run(jsonld_source(), http)

    def test_json_ld_without_events_is_no_event(self) -> None:
        assert parse_page({"@type": "Organization", "name": "Lab"}) == []

    def test_control_characters_inside_strings_are_accepted(self) -> None:
        # A raw tab and a raw line break inside JSON strings (strict JSON forbids them).
        block = '{"@type": "Event", "name": "A\ttalk", "startDate": "2026-11-17",'
        block += ' "description": "Line one\nline two"}'
        [draft] = parse_page(block)
        assert draft.title == "A talk"
        assert draft.abstract == "Line one line two"

    def test_deep_nesting_is_not_json_here(self) -> None:
        assert decode("[" * 100_000 + "]" * 100_000) is None

    @pytest.mark.parametrize("key", ["name", "description", "location", "performer"])
    def test_a_deeply_nested_value_does_not_lose_the_other_events(self, key: str) -> None:
        deep: Any = "Deep text"
        for _ in range(600):
            deep = [deep]
        drafts = parse_page([event(name="Kept"), {**event(name="Deep"), key: deep}])
        assert len(drafts) == 2
        assert drafts[0].title == "Kept"
        assert drafts[1].abstract is None
        assert drafts[1].location is None
        assert drafts[1].speakers == []

    def test_wide_and_moderately_deep_values(self) -> None:
        wide = [*([{}] * 50), "Too late"]
        [draft] = parse_page(event(name=[[["Found"]]], description=wide))
        assert draft.title == "Found"
        assert draft.abstract is None

    def test_references_by_id_are_resolved(self) -> None:
        venue = {"@type": "Place", "@id": "#venue", "name": "Room 9"}
        talk = event(location={"@id": "#venue"}, performer={"@id": "#jane"})
        jane = {"@type": "Person", "@id": "#jane", "name": "Jane Doe"}
        [draft] = parse_page({"@graph": [talk, venue, jane]})
        assert draft.location == "Room 9"
        assert [speaker.name for speaker in draft.speakers] == ["Jane Doe"]

    def test_the_walk_is_bounded(self) -> None:
        wide = [[{"x": index}] for index in range(200_000)] + [event(name="Too far")]
        assert find_events(wide) == []


class TestFields:
    @pytest.mark.parametrize(
        ("start", "end", "expected"),
        [
            ("2026-11-17T16:00:00-08:00", None, (datetime(2026, 11, 17, 16, tzinfo=PST), None)),
            (
                "2026-11-17T16:00:00",
                "2026-11-17T17:00",
                (datetime(2026, 11, 17, 16), datetime(2026, 11, 17, 17)),
            ),
            ("2026-11-17", "2026-11-19", (date(2026, 11, 17), date(2026, 11, 19))),
            ("Nov 17, 2026 4pm", None, ("Nov 17, 2026 4pm", None)),
        ],
    )
    def test_dates(self, start: str, end: str | None, expected: tuple[object, object]) -> None:
        [draft] = parse_page(event(startDate=start, endDate=end))
        assert (draft.start, draft.end) == expected

    def test_a_date_only_start_is_an_all_day_event(self) -> None:
        [draft] = parse_page(event(startDate="2026-11-17", endDate="2026-11-19"))
        assert draft.is_all_day

    def test_an_event_without_a_start_is_skipped(self) -> None:
        drafts = parse_page([event(name="Kept"), event(name="No start", startDate=None)])
        assert [draft.title for draft in drafts] == ["Kept"]

    @pytest.mark.parametrize(
        ("location", "text", "online", "stream"),
        [
            ("Gates 104", "Gates 104", None, None),
            (
                {
                    "@type": "Place",
                    "name": "Clark Center",
                    "address": {
                        "@type": "PostalAddress",
                        "streetAddress": "318 Campus Drive",
                        "addressLocality": "Stanford",
                        "addressCountry": {"@type": "Country", "name": "US"},
                    },
                },
                "Clark Center, 318 Campus Drive, Stanford, US",
                None,
                None,
            ),
            (
                {"@type": "PostalAddress", "streetAddress": "1 Main St", "addressRegion": "CA"},
                "1 Main St, CA",
                None,
                None,
            ),
            (
                {"@type": "VirtualLocation", "url": "https://zoom.us/j/1"},
                None,
                True,
                "https://zoom.us/j/1",
            ),
            (
                [
                    {"@type": "Place", "name": "Room 9"},
                    {"@type": "VirtualLocation", "url": "/live"},
                ],
                "Room 9",
                True,
                "https://center.example.edu/live",
            ),
        ],
    )
    def test_locations(
        self, location: Any, text: str | None, online: bool | None, stream: str | None
    ) -> None:
        [draft] = parse_page(event(location=location))
        assert (draft.location, draft.is_online, draft.stream_url) == (text, online, stream)

    @pytest.mark.parametrize(
        ("mode", "online"),
        [
            ("https://schema.org/OnlineEventAttendanceMode", True),
            ("MixedEventAttendanceMode", True),
            ("OfflineEventAttendanceMode", False),
            ("SomethingElse", None),
        ],
    )
    def test_attendance_modes(self, mode: str, online: bool | None) -> None:
        [draft] = parse_page(event(eventAttendanceMode=mode))
        assert draft.is_online is online

    @pytest.mark.parametrize(
        ("status", "hint", "online"),
        [
            ("https://schema.org/EventCancelled", "cancelled", None),
            ("EventPostponed", "cancelled", None),
            ("EventRescheduled", "confirmed", None),
            ("EventMovedOnline", "confirmed", True),
            ("EventScheduled", "confirmed", None),
            (None, None, None),
        ],
    )
    def test_statuses(self, status: str | None, hint: str | None, online: bool | None) -> None:
        [draft] = parse_page(event(eventStatus=status))
        assert (draft.status_hint, draft.is_online) == (hint, online)

    @pytest.mark.parametrize(
        ("status", "mode", "hint", "online"),
        [
            ({"@id": "https://schema.org/EventCancelled"}, None, "cancelled", None),
            ({"@id": "schema:EventMovedOnline"}, None, "confirmed", True),
            (None, {"@id": "https://schema.org/OfflineEventAttendanceMode"}, None, False),
            (None, [{"@id": "https://schema.org/MixedEventAttendanceMode"}], None, True),
            ({"@value": "EventPostponed"}, {"@id": 7}, "cancelled", None),
        ],
    )
    def test_statuses_and_modes_as_node_references(
        self, status: Any, mode: Any, hint: str | None, online: bool | None
    ) -> None:
        [draft] = parse_page(event(eventStatus=status, eventAttendanceMode=mode))
        assert (draft.status_hint, draft.is_online) == (hint, online)

    def test_performers_organizers_and_series(self) -> None:
        talk = event(
            performer=[
                {
                    "@type": "Person",
                    "name": "Ang\u00e8le Christin",
                    "affiliation": {"@type": "Organization", "name": "Stanford"},
                    "url": "/people/ac",
                },
                {
                    "@type": "Person",
                    "name": "Jo Li",
                    "worksFor": [{"@type": "Organization", "name": "MIT"}],
                },
                "Pat Doe",
                {"@type": "Person"},
            ],
            organizer={"@type": "Organization", "name": "Data Lab"},
            superEvent={"@type": "EventSeries", "name": "Data Seminar"},
            keywords="data, statistics",
        )
        [draft] = parse_page(talk)
        assert [(s.name, s.affiliation, s.url) for s in draft.speakers] == [
            ("Ang\u00e8le Christin", "Stanford", "https://center.example.edu/people/ac"),
            ("Jo Li", "MIT", None),
            ("Pat Doe", None, None),
        ]
        assert (draft.host_org, draft.series) == ("Data Lab", "Data Seminar")
        assert draft.categories == ["data", "statistics"]

    def test_text_values_are_copied_as_the_source_writes_them(self) -> None:
        talk = event(
            name="Q&amp;A on L\u00e9vy flights",
            description="<p>Abstract with <b>markup</b></p>",
            keywords=["a", {"@value": "b"}, 3],
            **{"@id": "https://center.example.edu/events/1#event"},
        )
        [draft] = parse_page(talk)
        assert draft.title == "Q&amp;A on L\u00e9vy flights"  # entities are left to normalization
        assert draft.abstract_html == "<p>Abstract with <b>markup</b></p>"
        assert draft.abstract is None
        assert draft.native_id == "https://center.example.edu/events/1#event"
        assert draft.categories == ["a", "b", "3"]

    def test_escapes_in_json_and_lone_surrogates(self) -> None:
        [draft] = parse_page(
            '{"@type": "Event", "name": "Poincar\\u00e9 \\ud83d", "startDate": "2026-11-17"}'
        )
        assert draft.title == "Poincar\u00e9"

    def test_url_falls_back_to_the_page_for_a_single_event(self) -> None:
        [alone] = parse_page(event())
        assert alone.url == PAGE_URL
        drafts = parse_page([event(), event(name="B", url="mailto:x@example.edu")])
        assert [draft.url for draft in drafts] == [None, None]

    def test_events_without_a_usable_start_are_skipped(self) -> None:
        blank = event(name="Blank", startDate="   ")
        odd = event(name="Odd", startDate={"@type": "Date"})
        drafts = parse_page([event(name="Kept"), blank, odd])
        assert [draft.title for draft in drafts] == ["Kept"]


class TestParams:
    def test_an_unknown_key_is_named(self) -> None:
        http = FakeHttpClient()
        http.add(PAGE_URL, page(event()))
        with pytest.raises(AdapterConfigError, match=r"params\.selectors: unknown key"):
            run(jsonld_source(params={"selectors": {}}), http)

    def test_extra_hosts_is_allowed(self) -> None:
        http = FakeHttpClient()
        http.add(PAGE_URL, page(event()))
        assert run(jsonld_source(params={"extra_hosts": ["cdn.example.edu"]}), http)
