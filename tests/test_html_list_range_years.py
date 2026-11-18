"""The year of one end of a range whose other end states the year.

A list that prints "Monday, Aug. 30 - Friday, Sept. 3, 2027" names the year
once, at the end. The first day takes the year of the last one (the year
before when its month comes later), not the year closest to the fetch, which
read in November 2026 would start the range on 2026-08-30 and make it a year
long. In the same way a last day without a year ("Dec. 28, 2026 - Jan. 3")
takes the year of the first (the year after when its month comes earlier),
which read in June 2026 would otherwise end the range on 2026-01-03, before
it starts. An end without any year of the range keeps the closest year.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from factories import make_source, make_window
from fakes import FakeClock, FakeHttpClient
from seminars.models import EventDraft, SourceConfig
from seminars.sources.base import get_adapter
from seminars.sources.generic.common import parse_day, parse_first_day, parse_last_day

LIST_URL = "https://talks.example.edu/workshops/"
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
FETCH_DAYS = [date(2026, 1, 15), date(2026, 11, 18), date(2027, 6, 1), date(2027, 12, 20)]
DASH = "\u2013"


@pytest.mark.parametrize("reference", FETCH_DAYS)
@pytest.mark.parametrize(
    ("first", "last", "expected"),
    [
        ("Monday, Aug. 30", "Friday, Sept. 3, 2027", date(2027, 8, 30)),
        ("Monday, Nov. 15", "Friday, Nov. 19, 2027", date(2027, 11, 15)),
        ("Monday, Nov. 30", "Friday, Dec. 4, 2026", date(2026, 11, 30)),
        ("Monday, Dec. 28", "Sunday, Jan. 3, 2027", date(2026, 12, 28)),
        ("Feb. 29", "Mar. 2, 2028", date(2028, 2, 29)),
        ("2027-08-30", "Friday, Sept. 3, 2027", date(2027, 8, 30)),
        ("Dec. 28, 2025", "Jan. 3, 2027", date(2025, 12, 28)),  # its own year stands
    ],
)
def test_the_first_day_takes_the_year_of_the_last(
    first: str, last: str, expected: date, reference: date
) -> None:
    assert parse_first_day(first, last, None, reference) == expected


@pytest.mark.parametrize("reference", FETCH_DAYS)
@pytest.mark.parametrize(
    ("first", "last", "fmt"),
    [
        ("Monday, Aug. 30", None, None),
        ("Monday, Aug. 30", "Friday, Sept. 3", None),  # no year in the range at all
        ("Monday, Aug. 30", "TBA", None),
        ("Monday, Aug. 30", "not a date", None),
        ("August 30", "September 3", "%B %d"),
        ("August 30, 2027", "September 3, 2027", "%B %d, %Y"),
        ("Feb. 29", "Mar. 2, 2027", None),  # no February 29 in 2027: the closest year
        ("TBA", "Friday, Sept. 3, 2027", None),
        ("", "Friday, Sept. 3, 2027", None),
    ],
)
def test_without_a_year_at_the_end_the_first_day_is_read_as_any_day(
    first: str, last: str | None, fmt: str | None, reference: date
) -> None:
    assert parse_first_day(first, last, fmt, reference) == parse_day(first, fmt, reference)


@pytest.mark.parametrize("reference", FETCH_DAYS)
@pytest.mark.parametrize(
    ("last", "first", "expected"),
    [
        ("Sunday, Jan. 3", "Monday, Dec. 28, 2026", date(2027, 1, 3)),
        ("Friday, Sept. 3", "Monday, Aug. 30, 2027", date(2027, 9, 3)),
        ("Friday, Dec. 4", "Monday, Nov. 30, 2026", date(2026, 12, 4)),
        ("Feb. 29", "Feb. 26, 2028", date(2028, 2, 29)),
        ("Mar. 2", "2028-02-28", date(2028, 3, 2)),
        ("Jan. 3, 2029", "Dec. 28, 2026", date(2029, 1, 3)),  # its own year stands
    ],
)
def test_the_last_day_takes_the_year_of_the_first(
    last: str, first: str, expected: date, reference: date
) -> None:
    assert parse_last_day(last, first, None, reference) == expected


@pytest.mark.parametrize("reference", FETCH_DAYS)
@pytest.mark.parametrize(
    ("last", "first", "fmt"),
    [
        ("Sunday, Jan. 3", None, None),
        ("Sunday, Jan. 3", "Monday, Dec. 28", None),  # no year in the range at all
        ("Sunday, Jan. 3", "TBA", None),
        ("Sunday, Jan. 3", "not a date", None),
        ("January 3", "December 28", "%B %d"),
        ("January 3, 2027", "December 28, 2026", "%B %d, %Y"),
        ("Feb. 29", "Feb. 26, 2027", None),  # no February 29 in 2027: the closest year
        ("TBA", "Monday, Dec. 28, 2026", None),
        ("", "Monday, Dec. 28, 2026", None),
    ],
)
def test_without_a_year_at_the_start_the_last_day_is_read_as_any_day(
    last: str, first: str | None, fmt: str | None, reference: date
) -> None:
    assert parse_last_day(last, first, fmt, reference) == parse_day(last, fmt, reference)


def card(when: str, title: str) -> str:
    return f"<li><a href='/w/{len(title)}'>{title}</a><span class='when'>{when}</span></li>"


def source(**params: Any) -> SourceConfig:
    base: dict[str, Any] = {
        "selectors": {"item": "ul.cards > li", "title": "a", "link": "a", "date": ".when"},
        "link_attribute": "href",
        "date_pattern": (
            rf"^(?P<date>[^{DASH}|]+?)(?:\s*{DASH}\s*(?P<end_date>[^|]+?))?"
            rf"(?:\s*\|\s*(?P<start>[^{DASH}]+?)\s*{DASH}\s*(?P<end>.+))?$"
        ),
    }
    return make_source(
        id="bayarea-example",
        group="bayarea",
        adapter="html_list",
        url=LIST_URL,
        params={**base, **params},
    )


def run(config: SourceConfig, *cards: str, now: datetime = NOW) -> list[EventDraft]:
    http = FakeHttpClient(clock=FakeClock(now))
    http.add(LIST_URL, "<html><body><ul class='cards'>" + "".join(cards) + "</ul></body></html>")
    adapter = get_adapter(config.adapter)
    window = make_window(now, days_ahead=365)
    return adapter.parse(adapter.fetch(http, config, window=window), config)


def test_a_range_of_next_year_read_in_november_starts_next_year() -> None:
    drafts = run(
        source(),
        card(f"Monday, Aug. 30 {DASH} Friday, Sept. 3, 2027", "Boot Camp"),
        card(f"Monday, Nov. 30 {DASH} Friday, Dec. 4, 2026", "Expansion"),
        card(f"Monday, Dec. 28 {DASH} Sunday, Jan. 3, 2027", "Winter School"),
    )
    assert [(d.title, d.start, d.end) for d in drafts] == [
        ("Boot Camp", date(2027, 8, 30), date(2027, 9, 3)),
        ("Expansion", date(2026, 11, 30), date(2026, 12, 4)),
        ("Winter School", date(2026, 12, 28), date(2027, 1, 3)),
    ]
    assert all(draft.is_all_day for draft in drafts)


def test_a_single_day_with_times_keeps_its_own_year() -> None:
    [draft] = run(source(), card(f"Wednesday, Nov. 18, 2026 | 9 a.m. {DASH} 5 p.m.", "Day"))
    assert (draft.start, draft.end) == (datetime(2026, 11, 18, 9), datetime(2026, 11, 18, 17))


def test_a_range_over_new_year_read_in_june_ends_next_year() -> None:
    # Read on 2026-06-01, "Jan. 3" alone is closest in 2026, before the start.
    june = datetime(2026, 6, 1, 17, 0, tzinfo=UTC)
    drafts = run(
        source(),
        card(f"Monday, Dec. 28, 2026 {DASH} Sunday, Jan. 3", "Winter School"),
        card(f"Monday, Aug. 30, 2026 {DASH} Friday, Sept. 3", "Boot Camp"),
        now=june,
    )
    assert [(d.title, d.start, d.end) for d in drafts] == [
        ("Winter School", date(2026, 12, 28), date(2027, 1, 3)),
        ("Boot Camp", date(2026, 8, 30), date(2026, 9, 3)),
    ]
