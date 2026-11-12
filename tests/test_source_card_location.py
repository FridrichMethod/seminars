"""The room of an ``rss`` card that only its free-text subheadline names.

Biochemistry writes "12/9/26, @MUNZER Auditorium, 4PM" into the subheadline
of a card and has no location element. ``card_fields`` maps the part to
``location`` and ``location_pattern`` takes the room out of it; a mapped part
is not the abstract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from factories import make_window
from seminars.config import load_sources
from seminars.http import offline_client
from seminars.models import EventDraft, SourceConfig
from seminars.sources.base import get_adapter
from seminars.sources.generic.common import AdapterConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCES = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
BIOCHEMISTRY = SOURCES["sites-biochemistry"]
WINDOW = make_window(datetime(2026, 11, 11, 20, 0, tzinfo=UTC))
PST = timezone(timedelta(hours=-8))


def run(source: SourceConfig) -> list[EventDraft]:
    adapter = get_adapter(source.adapter)
    payload = adapter.fetch(offline_client(source.id), source, window=WINDOW)
    return adapter.parse(payload, source)


def with_params(source: SourceConfig, **changes: Any) -> SourceConfig:
    params = {
        key: value for key, value in {**source.params, **changes}.items() if value is not None
    }
    return source.evolve(params=params)


def at(drafts: list[EventDraft], month: int, day: int) -> EventDraft:
    [draft] = [
        d
        for d in drafts
        if isinstance(d.start, datetime) and d.start == datetime(2026, month, day, 16, tzinfo=PST)
    ]
    return draft


class TestBiochemistry:
    def test_every_card_has_the_room_of_its_subheadline(self) -> None:
        drafts = run(BIOCHEMISTRY)
        rooms = {
            (d.start.month, d.start.day): d.location
            for d in drafts
            if isinstance(d.start, datetime)
        }
        assert rooms == {
            (11, 11): "Clark Auditorium",
            (11, 18): "Clark Auditorium",
            (12, 2): "Clark Auditorium",
            (12, 9): "MUNZER Auditorium",
        }

    def test_the_subheadline_is_no_longer_the_abstract(self) -> None:
        talk = at(run(BIOCHEMISTRY), 12, 9)
        assert talk.abstract is None
        assert talk.abstract_html is None

    def test_without_a_pattern_the_whole_part_is_the_location(self) -> None:
        talk = at(run(with_params(BIOCHEMISTRY, location_pattern=None)), 12, 9)
        assert talk.location == "12/9/26, @MUNZER Auditorium, 4PM"

    def test_a_pattern_that_does_not_match_gives_no_location_and_no_abstract(self) -> None:
        talk = at(run(with_params(BIOCHEMISTRY, location_pattern=r"Room (?P<location>\d+)")), 12, 9)
        assert (talk.location, talk.abstract) == (None, None)

    def test_without_the_mapping_the_part_stays_the_abstract(self) -> None:
        card_fields = {
            key: value
            for key, value in BIOCHEMISTRY.params["card_fields"].items()
            if key != "location"
        }
        source = with_params(BIOCHEMISTRY, card_fields=card_fields, location_pattern=None)
        talk = at(run(source), 12, 9)
        assert (talk.location, talk.abstract) == (None, "12/9/26, @MUNZER Auditorium, 4PM")

    def test_a_location_element_of_the_card_comes_first(self) -> None:
        list_page = {
            **BIOCHEMISTRY.params["list_page"],
            "location": ".event-list-item__subheadline",
        }
        talk = at(run(with_params(BIOCHEMISTRY, list_page=list_page)), 12, 9)
        assert talk.location == "12/9/26, @MUNZER Auditorium, 4PM"

    def test_the_location_pattern_needs_its_group(self) -> None:
        source = with_params(BIOCHEMISTRY, location_pattern=r"@\s*(?P<room>[^,]+)")
        with pytest.raises(AdapterConfigError, match="location_pattern needs the group"):
            run(source)
