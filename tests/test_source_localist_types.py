"""The event type of a Localist record that carries several types.

Organizers tag weekly one-hour seminars "Conference/Symposium" next to
"Lecture/Presentation/Talk". An entry of ``event_types`` with ``min_hours``
gives its type only to an instance that lasts that long (or a whole day, or
has no end); a shorter one takes the next type of the list it carries.
"""

from __future__ import annotations

import functools
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from factories import make_document, make_payload, make_source, make_window
from seminars.config import ConfigError
from seminars.config.loader import load_sources
from seminars.http import offline_client
from seminars.models import EventDraft, SourceConfig
from seminars.sources.generic.localist import LocalistAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]
RECORDED = datetime(2026, 11, 11, 15, 49, 33, tzinfo=UTC)
ENDPOINT = "https://calendar.example.edu/api/2/events"
CONFERENCE, SEMINAR, LECTURE = 1, 2, 3
TYPES = [
    {"id": CONFERENCE, "name": "Conference/Symposium", "event_type": "conference", "min_hours": 3},
    {"id": SEMINAR, "name": "Class/Seminar", "event_type": "seminar"},
    {"id": LECTURE, "name": "Lecture/Presentation/Talk", "event_type": "lecture"},
]


@functools.cache
def recorded() -> dict[str, EventDraft]:
    sources = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
    source = sources["localist-stanford"]
    adapter = LocalistAdapter()
    payload = adapter.fetch(
        offline_client("localist-stanford"), source, window=make_window(RECORDED)
    )
    return {draft.native_id or "": draft for draft in adapter.parse(payload, source)}


def example_source(event_types: list[dict[str, Any]]) -> SourceConfig:
    return make_source(
        id="localist-example",
        group="localist",
        adapter="localist",
        url=ENDPOINT,
        default_fields=[],
        params={"event_types": event_types},
    )


def parse(types: list[int], *, end: str | None, all_day: bool = False) -> EventDraft:
    instance = {
        "id": 1,
        "start": "2026-11-17T16:00:00-08:00",
        "end": end,
        "all_day": all_day,
    }
    item = {
        "id": 10,
        "title": "A talk",
        "event_instances": [{"event_instance": instance}],
        "filters": {"event_types": [{"id": value} for value in types]},
    }
    text = json.dumps({"events": [{"event": item}], "page": {"current": 1, "next_page": None}})
    document = make_document(url=f"{ENDPOINT}?page=1", text=text, content_type="application/json")
    source = example_source(TYPES)
    (draft,) = LocalistAdapter().parse(make_payload(document, source_id=source.id), source)
    return draft


class TestRecordedCalendar:
    def test_a_seminar_of_an_hour_tagged_as_a_conference_is_a_seminar(self) -> None:
        # "Environmental Behavioral Sciences Seminar", 75 minutes: seminar, conference, talk.
        assert recorded()["53624740189331"].event_type_hint == "seminar"

    def test_a_panel_shorter_than_the_threshold_is_a_lecture(self) -> None:
        # "Delivering Humanitarian Aid in a Contested World", 2.5 hours: conference, talk.
        assert recorded()["54161263754671"].event_type_hint == "lecture"

    @pytest.mark.parametrize("native_id", ["54110219615265", "53886818518423", "54064922323927"])
    def test_long_and_all_day_conferences_stay_conferences(self, native_id: str) -> None:
        assert recorded()[native_id].event_type_hint == "conference"


class TestMinHours:
    @pytest.mark.parametrize(
        ("types", "end", "all_day", "expected"),
        [
            ([CONFERENCE, LECTURE], "2026-11-17T17:00:00-08:00", False, "lecture"),
            ([CONFERENCE, SEMINAR, LECTURE], "2026-11-17T17:00:00-08:00", False, "seminar"),
            ([CONFERENCE, LECTURE], "2026-11-17T19:00:00-08:00", False, "conference"),
            ([CONFERENCE, LECTURE], "2026-11-18T10:00:00-08:00", False, "conference"),
            ([CONFERENCE, LECTURE], None, False, "conference"),
            ([CONFERENCE, LECTURE], "2026-11-17T23:59:00-08:00", True, "conference"),
            ([CONFERENCE], "2026-11-17T17:00:00-08:00", False, None),
            # An end without an offset cannot be compared with the start: length unknown.
            ([CONFERENCE, LECTURE], "2026-11-17T17:00:00", False, "conference"),
        ],
    )
    def test_the_entry_needs_its_length(
        self, types: list[int], end: str | None, all_day: bool, expected: str | None
    ) -> None:
        assert parse(types, end=end, all_day=all_day).event_type_hint == expected

    @pytest.mark.parametrize("value", [0, -1])
    def test_min_hours_is_positive(self, value: float) -> None:
        source = example_source([{"id": 1, "event_type": "conference", "min_hours": value}])
        document = make_document(
            url=f"{ENDPOINT}?page=1",
            text=json.dumps({"events": [], "page": {"current": 1, "next_page": None}}),
            content_type="application/json",
        )
        with pytest.raises(ConfigError, match="min_hours"):
            LocalistAdapter().parse(make_payload(document, source_id=source.id), source)
