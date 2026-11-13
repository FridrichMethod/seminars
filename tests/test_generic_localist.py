"""The localist adapter: requests, pagination and the mapping of a record to a draft."""

from __future__ import annotations

import functools
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit
from zoneinfo import ZoneInfo

import pytest

from factories import make_document, make_payload, make_source, make_window
from fakes import FakeHttpClient
from seminars.config import ConfigError
from seminars.config.loader import load_sources
from seminars.http import HttpStatusError, OfflineHttpClient, offline_client
from seminars.models import EventDraft, SourceConfig, TimeWindow
from seminars.sources import base
from seminars.sources.base import AdapterError, ParseError
from seminars.sources.generic import localist
from seminars.sources.generic.localist import (
    MAX_PAGES_PER_QUERY,
    LocalistAdapter,
    LocalistQuery,
    localist_params,
    query_url,
    window_days,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "localist-stanford"
PACIFIC = ZoneInfo("America/Los_Angeles")
RECORDED = datetime(2026, 11, 11, 15, 49, 33, tzinfo=UTC)
"""When the event pages of the fixture were recorded."""

ENDPOINT = "https://calendar.example.edu/api/2/events"


@functools.cache
def stanford_source() -> SourceConfig:
    sources = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
    return sources["localist-stanford"]


def example_source(**params: Any) -> SourceConfig:
    return make_source(
        id="localist-example",
        name="Example calendar",
        group="localist",
        adapter="localist",
        url=ENDPOINT,
        host_org="Example University",
        default_fields=[],
        params=params,
    )


def record(
    instance_id: int = 1, *, instance: dict[str, Any] | None = None, **fields: Any
) -> dict[str, Any]:
    """A Localist record (``events[].event``) of a one-hour talk on 2026-11-17."""
    occurrence = {
        "id": instance_id,
        "event_id": 10,
        "start": "2026-11-17T16:00:00-08:00",
        "end": "2026-11-17T17:00:00-08:00",
        "all_day": False,
        **(instance or {}),
    }
    return {
        "id": 10,
        "title": "A talk",
        "localist_url": "https://calendar.example.edu/event/a-talk",
        "event_instances": [{"event_instance": occurrence}],
        "location_name": "Clark Center",
        "room_number": "S360",
        "experience": "inperson",
        "status": "live",
        **fields,
    }


def page(records: list[Any], *, current: int = 1, next_page: Any = None) -> str:
    events = [
        item if not isinstance(item, dict) or "event" in item else {"event": item}
        for item in records
    ]
    return json.dumps({"events": events, "page": {"current": current, "next_page": next_page}})


def parse_records(records: list[Any], source: SourceConfig | None = None) -> list[EventDraft]:
    document = make_document(
        url=f"{ENDPOINT}?page=1", text=page(records), content_type="application/json"
    )
    source = source or example_source()
    return LocalistAdapter().parse(make_payload(document, source_id=source.id), source)


def parse_one(**fields: Any) -> EventDraft:
    (draft,) = parse_records([record(**fields)])
    return draft


# --- registration --------------------------------------------------------------------------


def test_the_module_registers_the_localist_adapter() -> None:
    assert isinstance(base.get_adapter("localist"), LocalistAdapter)


# --- a complete run on the recorded pages --------------------------------------------------


@pytest.fixture(scope="module")
def recorded_run() -> tuple[OfflineHttpClient, list[EventDraft]]:
    source = stanford_source()
    http = offline_client("localist-stanford")
    adapter = LocalistAdapter()
    payload = adapter.fetch(http, source, window=make_window(RECORDED))
    return http, adapter.parse(payload, source)


@pytest.fixture(scope="module")
def drafts_by_id(recorded_run: tuple[OfflineHttpClient, list[EventDraft]]) -> dict[str, EventDraft]:
    return {draft.native_id or "": draft for draft in recorded_run[1]}


def recorded_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(FIXTURE_DIR.glob("events-*-page*.json")):
        records += [
            item["event"] for item in json.loads(path.read_text(encoding="utf-8"))["events"]
        ]
    return records


class TestRecordedRun:
    def test_requests_follow_next_page_of_both_queries(
        self, recorded_run: tuple[OfflineHttpClient, list[EventDraft]]
    ) -> None:
        http, _ = recorded_run
        queries = [parse_qs(urlsplit(url).query) for url in http.requests]
        assert [query["page"] for query in queries] == [["1"], ["2"], ["3"], ["4"], ["1"]]
        assert ["type[]" in query for query in queries] == [True] * 4 + [False]
        assert "exclude_type[]" in queries[4]

    def test_every_request_asks_for_100_records_and_never_for_distinct_or_match(
        self, recorded_run: tuple[OfflineHttpClient, list[EventDraft]]
    ) -> None:
        http, _ = recorded_run
        window_start, window_end = window_days(make_window(RECORDED), "America/Los_Angeles")
        for url in http.requests:
            query = parse_qs(urlsplit(url).query)
            assert query["pp"] == ["100"], url
            assert query["start"] == [window_start.isoformat()]
            assert query["end"] == [window_end.isoformat()]
            assert "distinct" not in query
            assert "match" not in query
            assert url.startswith("https://events.stanford.edu/api/2/events?")

    def test_one_draft_per_recorded_instance(
        self, recorded_run: tuple[OfflineHttpClient, list[EventDraft]]
    ) -> None:
        _, drafts = recorded_run
        ids = [
            record["event_instances"][0]["event_instance"]["id"] for record in recorded_records()
        ]
        assert sorted(draft.native_id or "" for draft in drafts) == sorted(map(str, ids))
        assert {draft.source_id for draft in drafts} == {"localist-stanford"}
        assert {draft.extraction for draft in drafts} == {"api"}

    def test_a_colloquium_without_an_end(self, drafts_by_id: dict[str, EventDraft]) -> None:
        draft = drafts_by_id["54426626599231"]
        assert draft.title is not None
        assert draft.title.startswith(
            'Applied Physics/Physics Colloquium: Dmitry Abanin - "Quantum'
        )
        assert draft.start == datetime(2026, 11, 10, 15, 30, tzinfo=PACIFIC)
        assert isinstance(draft.start, datetime)
        assert draft.start.utcoffset() == timedelta(hours=-8)
        assert draft.end is None
        assert not draft.all_day
        assert draft.location == "Hewlett Teaching Center, 201"
        assert draft.is_online is False
        # Localist records name no speakers; normalization reads them from the title.
        assert draft.speakers == []
        assert draft.speaker_text is None
        assert draft.field_hints == ["physics"]
        assert draft.event_type_hint == "lecture"
        assert draft.url is not None
        assert draft.url.startswith("https://events.stanford.edu/event/")

    def test_a_hybrid_seminar(self, drafts_by_id: dict[str, EventDraft]) -> None:
        draft = drafts_by_id["51288621937120"]
        assert draft.start == datetime(2026, 11, 10, 16, 0, tzinfo=PACIFIC)
        assert draft.end == datetime(2026, 11, 10, 17, 0, tzinfo=PACIFIC)
        assert draft.location == "Stanford ChEM-H Building, Gunn Rotunda"
        assert draft.is_online is True
        assert draft.field_hints == ["bio", "med"]
        assert draft.event_type_hint == "seminar"

    def test_a_virtual_event_has_no_location(self, drafts_by_id: dict[str, EventDraft]) -> None:
        draft = drafts_by_id["53809595903126"]
        assert draft.location is None
        assert draft.is_online is True

    def test_all_day_events_are_dates(self, drafts_by_id: dict[str, EventDraft]) -> None:
        deadline = drafts_by_id["54064922323927"]
        assert deadline.start == date(2026, 11, 15)
        assert deadline.end is None
        assert deadline.all_day
        assert deadline.is_all_day
        program = drafts_by_id["54225562985263"]
        assert program.start == date(2026, 11, 18)
        assert program.field_hints == ["med"]
        assert {"School of Medicine", "Stanford Center for Continuing Medical Education"} <= set(
            program.categories
        )

    def test_the_first_matching_event_type_of_the_list_wins(
        self, drafts_by_id: dict[str, EventDraft]
    ) -> None:
        assert drafts_by_id["54410800155205"].event_type_hint == "defense"
        two_types = drafts_by_id["53700432901792"]
        assert {"Class/Seminar", "Lecture/Presentation/Talk"} <= set(two_types.categories)
        assert two_types.event_type_hint == "seminar"
        assert drafts_by_id["54064922323927"].event_type_hint == "conference"
        assert drafts_by_id["53985402088649"].event_type_hint is None

    def test_departments_with_different_fields_are_joined(
        self, drafts_by_id: dict[str, EventDraft]
    ) -> None:
        draft = drafts_by_id["54047808526631"]
        assert draft.field_hints == ["ai-ml", "bio"]
        assert "Wu Tsai Neurosciences Institute" in draft.categories

    def test_unmapped_and_missing_departments_give_no_hint(
        self, drafts_by_id: dict[str, EventDraft]
    ) -> None:
        assert drafts_by_id["54037513552790"].field_hints == []
        assert drafts_by_id["53809587119189"].field_hints == []

    def test_the_first_department_hosts_and_none_leaves_the_host_of_the_source(
        self, drafts_by_id: dict[str, EventDraft]
    ) -> None:
        assert drafts_by_id["54047808526631"].host_org == "Wu Tsai Neurosciences Institute"
        assert (
            drafts_by_id["54037513552790"].host_org == "Department of Aeronautics and Astronautics"
        )
        assert drafts_by_id["53809587119189"].host_org is None

    def test_groups_are_categories(self, drafts_by_id: dict[str, EventDraft]) -> None:
        draft = drafts_by_id["54307369440521"]
        assert {"Cardinal Courage", "Stanford Chapter of AAUP"} <= set(draft.categories)
        assert draft.stream_url is not None
        assert draft.is_online is True

    def test_a_stream_url_with_a_password_is_passed_on_for_normalization(
        self, drafts_by_id: dict[str, EventDraft]
    ) -> None:
        assert drafts_by_id["54004063149104"].stream_url == (
            "https://stanford.zoom.us/j/97375161734?pwd=REDACTED"
        )

    def test_a_cancellation_in_the_title_is_left_to_normalization(
        self, drafts_by_id: dict[str, EventDraft]
    ) -> None:
        draft = drafts_by_id["53741880932247"]
        assert draft.title is not None
        assert draft.title.startswith("CANCELED:")
        assert draft.status_hint is None

    def test_non_ascii_names_are_kept(self, drafts_by_id: dict[str, EventDraft]) -> None:
        assert (
            drafts_by_id["54294379845960"].title
            == "E-IPER Dissertation Defense: Ana\u00efs Vo\u015fki"
        )

    def test_stream_info_is_never_copied(self, drafts_by_id: dict[str, EventDraft]) -> None:
        for item in recorded_records():
            info = (item.get("stream_info") or "").strip()
            if not info:
                continue
            draft = drafts_by_id[str(item["event_instances"][0]["event_instance"]["id"])]
            dumped = draft.model_dump(mode="json")
            assert info not in json.dumps(dumped, ensure_ascii=False)
            texts = [value for value in dumped.values() if isinstance(value, str)]
            assert all(info not in text for text in texts)

    def test_abstract_is_the_html_description(self, drafts_by_id: dict[str, EventDraft]) -> None:
        draft = drafts_by_id["51288621937120"]
        assert draft.abstract is None
        assert (draft.abstract_html or "").startswith("<p>The <a href=")


# --- the source entry and the reference lists of the calendar ------------------------------


def _reference(prefix: str, key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(FIXTURE_DIR.glob(f"{prefix}-page*.json")):
        items += [item[key] for item in json.loads(path.read_text(encoding="utf-8"))[f"{key}s"]]
    return items


class TestSourceEntryAgainstTheCalendar:
    """What the research recorded can be checked against the source entry."""

    def test_every_department_key_names_a_department(self) -> None:
        names = {item["name"].strip() for item in _reference("departments", "department")}
        assert len(_reference("departments", "department")) == 736
        keys = {key.strip() for key in stanford_source().params["department_fields"]}
        assert keys <= names, sorted(keys - names)

    def test_every_group_key_names_a_group(self) -> None:
        names = {item["name"].strip() for item in _reference("groups", "group")}
        assert len(_reference("groups", "group")) == 400
        keys = {key.strip() for key in stanford_source().params["group_fields"]}
        assert keys <= names, sorted(keys - names)

    def test_every_type_id_exists_and_the_untyped_query_excludes_all_types(self) -> None:
        filters = json.loads((FIXTURE_DIR / "events-filters.json").read_text(encoding="utf-8"))
        known = {item["id"] for family in filters.values() for item in family}
        event_types = {item["id"] for item in filters["event_types"]}
        params = localist_params(stanford_source())
        for query in params.queries:
            assert set(query.type_ids) <= known, query.label
            assert set(query.exclude_type_ids) <= known, query.label
        assert {entry.id for entry in params.event_types} <= event_types
        (untyped,) = [query for query in params.queries if query.exclude_type_ids]
        assert sorted(untyped.exclude_type_ids) == sorted(event_types)
        assert len(untyped.exclude_type_ids) == len(set(untyped.exclude_type_ids)) == 17

    def test_the_source_params_are_valid(self) -> None:
        params = localist_params(stanford_source())
        assert params.per_page == 100
        assert [query.label for query in params.queries] == ["talk-types", "untyped"]


# --- requests ------------------------------------------------------------------------------


class TestRequests:
    def test_window_days_are_local_and_the_end_is_exclusive(self) -> None:
        assert window_days(make_window(), "America/Los_Angeles") == (
            date(2026, 10, 27),
            date(2027, 3, 11),
        )

    def test_window_days_across_the_change_to_standard_time(self) -> None:
        window = TimeWindow(
            start=datetime(2026, 10, 28, 6, 30, tzinfo=UTC),  # 23:30 PDT on 27 October
            end=datetime(2026, 11, 2, 8, 0, tzinfo=UTC),  # midnight PST on 2 November
        )
        assert window_days(window, "America/Los_Angeles") == (date(2026, 10, 27), date(2026, 11, 2))
        assert window_days(window, "UTC") == (date(2026, 10, 28), date(2026, 11, 3))

    def test_query_url_puts_the_parameters_in_order(self) -> None:
        query = LocalistQuery(label="q", type_ids=[1, 2], exclude_type_ids=[3], group_ids=[4, 5])
        url = query_url(
            ENDPOINT, query, days=(date(2026, 11, 1), date(2026, 11, 2)), per_page=100, page=3
        )
        assert url == (
            f"{ENDPOINT}?type[]=1&type[]=2&exclude_type[]=3&group_id[]=4&group_id[]=5"
            "&start=2026-11-01&end=2026-11-02&pp=100&page=3"
        )
        assert query_url(
            f"{ENDPOINT}?x=1",
            LocalistQuery(label="q"),
            days=(date(2026, 1, 1), date(2026, 1, 2)),
            per_page=5,
            page=1,
        ).startswith(f"{ENDPOINT}?x=1&start=")


def fake_pages(pages: dict[int, str], *, query: LocalistQuery | None = None) -> FakeHttpClient:
    http = FakeHttpClient()
    days = window_days(make_window(), "America/Los_Angeles")
    for number, text in pages.items():
        url = query_url(
            ENDPOINT, query or LocalistQuery(label="all"), days=days, per_page=100, page=number
        )
        http.add(url, text, content_type="application/json")
    return http


class TestPagination:
    def test_next_page_is_followed_until_it_is_null(self) -> None:
        http = fake_pages(
            {1: page([record(1)], next_page=2), 2: page([record(2)], current=2, next_page=None)}
        )
        source = example_source()
        adapter = LocalistAdapter()
        payload = adapter.fetch(http, source, window=make_window())
        assert [dict(parse_qsl(urlsplit(url).query))["page"] for url in http.requests] == ["1", "2"]
        assert [document.label for document in payload.documents] == ["all", "all"]
        assert [draft.native_id for draft in adapter.parse(payload, source)] == ["1", "2"]

    def test_a_next_page_that_goes_back_is_an_error(self) -> None:
        http = fake_pages({1: page([], next_page=2), 2: page([], current=2, next_page=2)})
        with pytest.raises(ParseError, match="names page 2"):
            LocalistAdapter().fetch(http, example_source(), window=make_window())

    def test_pagination_is_bounded(self) -> None:
        pages = {n: page([], current=n, next_page=n + 1) for n in range(1, MAX_PAGES_PER_QUERY + 2)}
        http = fake_pages(pages)
        with pytest.raises(AdapterError, match=f"more than {MAX_PAGES_PER_QUERY} pages"):
            LocalistAdapter().fetch(http, example_source(), window=make_window())
        assert len(http.requests) == MAX_PAGES_PER_QUERY

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("<html>maintenance</html>", "not JSON"),
            (json.dumps({"page": {}}), "no list of events"),
            (json.dumps(["events"]), "no list of events"),
            (json.dumps({"events": []}), "no page object"),
            (json.dumps({"events": [], "page": {"next_page": "2"}}), "next_page is not a number"),
            (json.dumps({"events": [], "page": {"next_page": True}}), "next_page is not a number"),
        ],
    )
    def test_an_answer_that_is_no_event_list_is_a_parse_error(
        self, text: str, message: str
    ) -> None:
        with pytest.raises(ParseError, match=message):
            LocalistAdapter().fetch(fake_pages({1: text}), example_source(), window=make_window())

    def test_an_http_error_fails_the_whole_source_with_url_and_status(self) -> None:
        typed = LocalistQuery(label="typed", type_ids=[1])
        untyped = LocalistQuery(label="untyped", exclude_type_ids=[1])
        source = example_source(queries=[typed.model_dump(), untyped.model_dump()])
        days = window_days(make_window(), "America/Los_Angeles")
        http = FakeHttpClient()
        http.add(query_url(ENDPOINT, typed, days=days, per_page=100, page=1), page([record(1)]))
        bad = query_url(ENDPOINT, untyped, days=days, per_page=100, page=1)
        http.add(bad, '{"error": "Unknown event filter"}', status=400)
        with pytest.raises(HttpStatusError) as caught:
            LocalistAdapter().fetch(http, source, window=make_window())
        assert caught.value.status == 400
        assert "400" in str(caught.value)
        assert "exclude_type[]=1" in str(caught.value)

    def test_a_record_seen_in_an_earlier_query_is_dropped(self) -> None:
        first = LocalistQuery(label="first", type_ids=[1])
        second = LocalistQuery(label="second", exclude_type_ids=[1])
        source = example_source(queries=[first.model_dump(), second.model_dump()])
        days = window_days(make_window(), "America/Los_Angeles")
        http = FakeHttpClient()
        http.add(
            query_url(ENDPOINT, first, days=days, per_page=100, page=1),
            page([record(1, title="Old")]),
        )
        http.add(
            query_url(ENDPOINT, second, days=days, per_page=100, page=1),
            page([record(1, title="New"), record(2)]),
        )
        adapter = LocalistAdapter()
        drafts = adapter.parse(adapter.fetch(http, source, window=make_window()), source)
        assert [(draft.native_id, draft.title) for draft in drafts] == [
            ("1", "Old"),
            ("2", "A talk"),
        ]


# --- mapping of one record -----------------------------------------------------------------


class TestRecordMapping:
    def test_a_timed_event(self) -> None:
        draft = parse_one()
        assert draft.native_id == "1"
        assert draft.title == "A talk"
        assert draft.start == datetime(2026, 11, 17, 16, 0, tzinfo=PACIFIC)
        assert draft.end == datetime(2026, 11, 17, 17, 0, tzinfo=PACIFIC)
        assert draft.url == "https://calendar.example.edu/event/a-talk"
        assert draft.location == "Clark Center, S360"
        assert draft.timezone is None

    @pytest.mark.parametrize(
        ("instance", "start", "end"),
        [
            (
                {"start": "2026-11-15T00:00:00-08:00", "end": None, "all_day": True},
                date(2026, 11, 15),
                None,
            ),
            (
                {
                    "start": "2026-11-15T00:00:00-08:00",
                    "end": "2026-11-15T23:59:00-08:00",
                    "all_day": True,
                },
                date(2026, 11, 15),
                None,
            ),
            (
                {
                    "start": "2026-11-15T00:00:00-08:00",
                    "end": "2026-11-16T00:00:00-08:00",
                    "all_day": True,
                },
                date(2026, 11, 15),
                None,
            ),
            (
                {
                    "start": "2026-11-15T00:00:00-08:00",
                    "end": "2026-11-17T23:59:00-08:00",
                    "all_day": True,
                },
                date(2026, 11, 15),
                date(2026, 11, 17),
            ),
            (
                {"start": "2026-11-15T08:00:00+00:00", "end": None, "all_day": True},
                date(2026, 11, 15),
                None,
            ),
            (
                {"start": "2026-11-15T00:00:00", "end": None, "all_day": True},
                date(2026, 11, 15),
                None,
            ),
        ],
    )
    def test_all_day_events(self, instance: dict[str, Any], start: date, end: date | None) -> None:
        draft = parse_one(instance=instance)
        assert (draft.start, draft.end, draft.all_day) == (start, end, True)

    def test_a_timed_event_without_an_end_and_a_naive_time(self) -> None:
        draft = parse_one(instance={"start": "2026-11-17T16:00:00", "end": None})
        assert draft.start == datetime(2026, 11, 17, 16, 0)
        assert draft.end is None
        assert not draft.is_all_day

    def test_a_multi_day_event_keeps_its_end(self) -> None:
        draft = parse_one(
            instance={"start": "2026-11-17T09:00:00-08:00", "end": "2026-11-19T17:00:00-08:00"}
        )
        assert draft.end == datetime(2026, 11, 19, 17, 0, tzinfo=PACIFIC)

    @pytest.mark.parametrize(
        ("experience", "online"),
        [
            ("inperson", False),
            ("hybrid", True),
            ("virtual", True),
            ("elsewhere", None),
            (None, None),
        ],
    )
    def test_experience_says_whether_it_is_online(
        self, experience: str | None, online: bool | None
    ) -> None:
        assert parse_one(experience=experience).is_online is online

    @pytest.mark.parametrize(
        ("status", "hint"),
        [
            ("live", None),
            ("canceled", "cancelled"),
            ("Cancelled", "cancelled"),
            ("postponed", "cancelled"),
            ("soldout", None),
            (None, None),
        ],
    )
    def test_status(self, status: str | None, hint: str | None) -> None:
        assert parse_one(status=status).status_hint == hint

    @pytest.mark.parametrize(
        ("fields", "location"),
        [
            ({}, "Clark Center, S360"),
            ({"room_number": None}, "Clark Center"),
            ({"location_name": "", "location": "Packard 101", "room_number": None}, "Packard 101"),
            (
                {"location_name": "", "room_number": "", "address": "450 Serra Mall"},
                "450 Serra Mall",
            ),
            ({"location_name": None, "room_number": None, "address": ""}, None),
            ({"location_name": "  Gates  ", "room_number": " B03 "}, "Gates, B03"),
        ],
    )
    def test_location(self, fields: dict[str, Any], location: str | None) -> None:
        assert parse_one(**fields).location == location

    def test_text_is_copied_as_it_is(self) -> None:
        draft = parse_one(
            title="Ren&#039;s seminar &amp; reception",
            description_text="Abstract by Fran\u00e7ois",
            description="<p>Abstract</p>",
        )
        assert draft.title == "Ren&#039;s seminar &amp; reception"
        assert draft.abstract is None
        assert draft.abstract_html == "<p>Abstract</p>"

    def test_html_description_when_there_is_no_plain_text(self) -> None:
        draft = parse_one(description_text="  ", description="<p>Abstract &amp; more</p>")
        assert draft.abstract is None
        assert draft.abstract_html == "<p>Abstract &amp; more</p>"

    def test_urls_are_absolute_or_none(self) -> None:
        draft = parse_one(localist_url="/event/relative", stream_url="mailto:someone@example.edu")
        assert draft.url == "https://calendar.example.edu/event/relative"
        assert draft.stream_url is None
        assert parse_one(localist_url=None).url is None

    def test_stream_info_is_not_read(self) -> None:
        draft = parse_one(stream_info="<p>Passcode: 123456</p>", stream_url=None)
        assert "123456" not in draft.model_dump_json()

    def test_categories_and_hints(self) -> None:
        source = example_source(
            department_fields={" Department of Mathematics ": ["math"], "Bioengineering": ["bio"]},
            group_fields={"Robotics Club": ["cs", "ai-ml"]},
            event_types=[{"id": 7, "name": "Seminar", "event_type": "seminar"}],
        )
        item = record(
            filters={
                "event_types": [{"id": 7, "name": "Seminar"}],
                "event_subject": [{"id": 8, "name": "Science"}],
                "event_audience": [{"id": 9, "name": "Faculty/Staff"}, "junk"],
            },
            departments=[
                {"id": 1, "name": "Department of Mathematics "},
                {"id": 2, "name": "Bioengineering"},
                {"id": 3},
            ],
            groups=[{"id": 4, "name": "Robotics Club"}],
            tags=["Robotics", " ", 3, "Robotics"],
        )
        (draft,) = parse_records([item], source)
        assert draft.field_hints == ["math", "cs", "ai-ml", "bio"]
        assert draft.categories == [
            "Seminar",
            "Science",
            "Faculty/Staff",
            "Department of Mathematics",
            "Bioengineering",
            "Robotics Club",
        ]
        assert draft.tags == ["Robotics"]
        assert draft.event_type_hint == "seminar"

    def test_the_only_department_is_the_host(self) -> None:
        draft = parse_one(departments=[{"id": 1, "name": " Department of Chemistry "}])
        assert draft.host_org == "Department of Chemistry"

    def test_the_first_of_several_departments_is_the_host(self) -> None:
        draft = parse_one(
            departments=[
                {"id": 3},
                {"id": 1, "name": "Wu Tsai Neurosciences Institute"},
                {"id": 2, "name": "Department of Neurobiology"},
            ],
            groups=[{"id": 4, "name": "Robotics Club"}],
        )
        assert draft.host_org == "Wu Tsai Neurosciences Institute"

    @pytest.mark.parametrize(
        "departments", [None, [], [{"id": 1}, {"id": 2, "name": " "}], "Biology"]
    )
    def test_without_a_named_department_the_host_of_the_source_stays(
        self, departments: Any
    ) -> None:
        draft = parse_one(departments=departments, groups=[{"id": 4, "name": "Robotics Club"}])
        assert draft.host_org is None

    def test_odd_shapes_of_lists_give_nothing(self) -> None:
        draft = parse_one(departments="Biology", groups=None, filters=["x"], tags="tag")
        assert draft.field_hints == []
        assert draft.categories == []
        assert draft.tags == []
        assert draft.event_type_hint is None
        assert parse_one(filters={"event_types": "Seminar"}).event_type_hint is None

    @pytest.mark.parametrize(
        "broken",
        [
            record(event_instances=[]),
            record(event_instances=None),
            record(event_instances=["x"]),
            record(instance={"id": None}),
            record(instance={"id": True}),
            record(instance={"id": ""}),
            record(instance={"start": None}),
            record(instance={"start": "next Tuesday"}),
            record(instance={"end": 5}),
            "not an object",
            {"event": "not an object"},
        ],
    )
    def test_a_malformed_record_is_skipped(self, broken: Any) -> None:
        drafts = parse_records([broken, record(99)])
        assert [draft.native_id for draft in drafts] == ["99"]

    def test_unusable_links_are_dropped_but_the_record_is_kept(self) -> None:
        draft = parse_one(
            localist_url="https://user:secret@calendar.example.edu/x", stream_url=["x"]
        )
        assert draft.url is None
        assert draft.stream_url is None


# --- params --------------------------------------------------------------------------------


class TestParams:
    def test_defaults(self) -> None:
        params = localist_params(example_source())
        assert params.per_page == 100
        assert [query.label for query in params.queries] == ["all"]

    def test_extra_hosts_is_accepted(self) -> None:
        assert localist_params(example_source(extra_hosts=["events.example.edu"])).per_page == 100

    @pytest.mark.parametrize(
        ("params", "named"),
        [
            ({"distinct": True}, "params.distinct"),
            ({"per_page": 500}, "params.per_page"),
            ({"queries": [{"label": "a", "types": [1]}]}, "params.queries.0.types"),
            ({"queries": [{"label": "a"}, {"label": "a"}]}, "params.queries"),
            ({"queries": []}, "params.queries"),
            ({"department_fields": {"Physics": ["astro"]}}, "params.department_fields.Physics"),
            (
                {"event_types": [{"id": 1, "event_type": "party"}]},
                "params.event_types.0.event_type",
            ),
        ],
    )
    def test_a_bad_or_unknown_key_is_named(self, params: dict[str, Any], named: str) -> None:
        source = example_source(**params)
        with pytest.raises(ConfigError, match=named.replace(".", r"\.")):
            localist_params(source)
        with pytest.raises(ConfigError, match="localist-example"):
            LocalistAdapter().fetch(FakeHttpClient(), source, window=make_window())
        with pytest.raises(ConfigError):
            LocalistAdapter().parse(make_payload(source_id=source.id), source)

    def test_exactly_one_url(self) -> None:
        source = example_source().evolve(urls=[f"{ENDPOINT}/other"])
        with pytest.raises(ConfigError, match="exactly one url"):
            LocalistAdapter().fetch(FakeHttpClient(), source, window=make_window())


def test_the_adapter_keeps_no_state() -> None:
    assert vars(LocalistAdapter()) == {}
    assert localist.MAX_PAGES_PER_QUERY == MAX_PAGES_PER_QUERY
