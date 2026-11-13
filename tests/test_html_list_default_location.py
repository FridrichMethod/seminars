"""The place of rows that name none: ``default_location`` of the ``html_list`` adapter.

Some pages state the room of every talk once, in their heading, and never in a
row. The source entry then names that room; a row that names a place of its
own, or whose detail page does, keeps it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from factories import make_source, make_window
from fakes import FakeHttpClient
from seminars.models import EventDraft, SourceConfig
from seminars.sources.base import get_adapter
from seminars.sources.generic.common import AdapterConfigError

LIST_URL = "https://talks.example.edu/seminar/"
TALK_URL = "https://talks.example.edu/talks/one"
WINDOW = make_window(datetime(2026, 11, 11, 20, 0, tzinfo=UTC))
ROOM = "LKSC 120 (Li Ka Shing Center)"


def row(title: str, place: str | None = None) -> str:
    where = f"<span class='where'>{place}</span>" if place is not None else ""
    return (
        f"<li><h3><a href='/talks/one'>{title}</a></h3>"
        f"<span class='date'>2026-11-17</span>{where}</li>"
    )


def source(**params: Any) -> SourceConfig:
    base: dict[str, Any] = {
        "selectors": {
            "item": "ul.talks > li",
            "title": "h3",
            "link": "h3 a",
            "date": ".date",
            "location": ".where",
        },
        "link_attribute": "href",
    }
    return make_source(
        id="custom-example",
        group="stanford-custom",
        adapter="html_list",
        url=LIST_URL,
        params={**base, **params},
    )


def run(config: SourceConfig, *rows: str, detail: str | None = None) -> list[EventDraft]:
    http = FakeHttpClient()
    http.add(LIST_URL, "<html><body><ul class='talks'>" + "".join(rows) + "</ul></body></html>")
    if detail is not None:
        http.add(TALK_URL, detail)
    adapter = get_adapter(config.adapter)
    return adapter.parse(adapter.fetch(http, config, window=WINDOW), config)


def test_a_row_without_a_place_gets_the_default_location() -> None:
    [draft] = run(source(default_location=ROOM), row("One"))
    assert draft.location == ROOM


def test_a_row_with_a_place_keeps_its_own() -> None:
    [draft] = run(source(default_location=ROOM), row("One", place="Gates 104"))
    assert draft.location == "Gates 104"


def test_the_place_of_the_detail_page_comes_before_the_default() -> None:
    detail = "<html><body><p class='place'>Room 1<br>Main Street</p></body></html>"
    config = source(default_location=ROOM, detail_page={"location": ".place"})
    [draft] = run(config, row("One"), detail=detail)
    assert draft.location == "Room 1, Main Street"


def test_without_the_param_a_row_without_a_place_has_none() -> None:
    [draft] = run(source(), row("One"))
    assert draft.location is None


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_default_location_is_an_error(value: str) -> None:
    with pytest.raises(AdapterConfigError, match=r"params\.default_location"):
        run(source(default_location=value), row("One"))
