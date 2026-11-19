"""Browser tests of how the talk list counts calendar days.

An all-day event is listed until its last day is over in the time zone of the payload,
also on the two days a year when that day is 23 or 25 hours long. An event that began
before today is listed once, under Ongoing above today. The server, the
browser and the sessions are those of test_site_browser.py; its fixtures are bound to
names of this module, so pytest finds them here as well. Marked ``browser`` like that
module: ``uv run pytest -m browser``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

import test_site_browser as harness
from test_site_browser import Session, sample, serve_json

pytestmark = pytest.mark.browser

site_dir = harness.site_dir
base_url = harness.base_url
browser = harness.browser
session = harness.session

SPRING = "2027-03-14T00:00:00-08:00"  # clocks go forward at 2:00: a 23 hour day
AUTUMN = "2026-11-01T00:00:00-07:00"  # clocks go back at 2:00: a 25 hour day


def all_day_payload(starts: dict[str, tuple[str, str | None]]) -> dict[str, Any]:
    """The sample events.json with only all-day copies of one sample talk."""
    payload = sample("events.json")
    arc = next(event for event in payload["events"] if event["uid"] == "sample-arc")
    payload["events"] = [
        {**arc, "uid": uid, "all_day": True, "start": start, "end": end}
        for uid, (start, end) in starts.items()
    ]
    return payload


def open_at(session: Callable[..., Session], now: str, payload: dict[str, Any]) -> Session:
    current = session(now=now)
    serve_json(current, "events.json", payload)
    return current.open("index.html")


def days_shown(current: Session) -> list[str]:
    return current.page.eval_on_selector_all(
        "#events section", "es => es.map((e) => e.dataset.day)"
    )


class TestAllDayEvents:
    @pytest.mark.parametrize(
        ("start", "day", "last_minute", "next_midnight"),
        [
            (SPRING, "2027-03-14", "2027-03-15T06:59:00Z", "2027-03-15T07:00:00Z"),
            (AUTUMN, "2026-11-01", "2026-11-02T07:59:00Z", "2026-11-02T08:00:00Z"),
        ],
    )
    def test_a_one_day_event_lasts_exactly_its_calendar_day(
        self,
        session: Callable[..., Session],
        start: str,
        day: str,
        last_minute: str,
        next_midnight: str,
    ) -> None:
        payload = all_day_payload({"whole-day": (start, None)})
        sessions = [
            open_at(session, moment, payload) for moment in (start, last_minute, next_midnight)
        ]
        for current in sessions[:2]:
            assert current.shown() == ["whole-day"]
            assert days_shown(current) == [day]
            article = current.page.locator('article[data-uid="whole-day"]')
            assert article.locator(".event-when").text_content() == "All day"
            assert article.locator(".event-until").count() == 0
        assert sessions[2].shown() == []
        for current in sessions:
            current.assert_clean()

    def test_the_day_before_lists_the_event_under_its_own_day(
        self, session: Callable[..., Session]
    ) -> None:
        payload = all_day_payload({"whole-day": (SPRING, None)})
        current = open_at(session, "2027-03-13T20:00:00Z", payload)
        assert days_shown(current) == ["2027-03-14"]
        current.page.locator("#range").select_option("today")
        assert current.shown() == []
        current.assert_clean()

    def test_a_multi_day_event_over_the_clock_change_ends_with_its_last_day(
        self, session: Callable[..., Session]
    ) -> None:
        payload = all_day_payload({"workshop": (SPRING, "2027-03-16T00:00:00-07:00")})
        during = open_at(session, "2027-03-15T07:00:00Z", payload)
        assert days_shown(during) == ["ongoing"]
        until = during.page.locator('article[data-uid="workshop"] .event-until').text_content()
        assert until == "Until Monday, March 15"
        assert open_at(session, "2027-03-16T06:59:00Z", payload).shown() == ["workshop"]
        assert open_at(session, "2027-03-16T07:00:00Z", payload).shown() == []
        during.assert_clean()


def retreat_payload() -> dict[str, Any]:
    """The sample events.json with a retreat of five days that began two days before NOW."""
    payload = sample("events.json")
    symposium = next(event for event in payload["events"] if event["uid"] == "sample-all-day")
    retreat = {
        **symposium,
        "uid": "retreat",
        "title": "Winter retreat on protein design",
        "start": "2026-11-08T00:00:00-08:00",
        "end": "2026-11-13T00:00:00-08:00",
    }
    payload["events"] = [retreat, *payload["events"]]
    return payload


class TestOngoingEvents:
    def test_an_event_that_began_before_today_is_listed_once_under_ongoing(
        self, session: Callable[..., Session]
    ) -> None:
        current = open_at(session, harness.NOW, retreat_payload())
        page = current.page
        assert current.shown()[:2] == ["retreat", "sample-xss"]
        assert days_shown(current)[:2] == ["ongoing", "2026-11-12"]
        ongoing = page.locator('#events section[data-day="ongoing"]')
        assert ongoing.locator("h2").text_content() == "Ongoing"
        assert ongoing.locator("article").count() == 1
        until = ongoing.locator(".event-until").text_content()
        assert until == "Until Thursday, November 12"
        name = "Select Winter retreat on protein design Ongoing All day"
        assert page.get_by_role("checkbox", name=name, exact=True).count() == 1
        page.locator("#range").select_option("today")
        assert current.shown() == ["retreat"]
        assert days_shown(current) == ["ongoing"]
        page.locator("#kind").select_option("talks")
        assert current.shown() == []
        current.assert_clean()

    def test_a_timed_event_under_ongoing_shows_the_day_it_began(
        self, session: Callable[..., Session]
    ) -> None:
        payload = sample("events.json")
        arc = next(event for event in payload["events"] if event["uid"] == "sample-arc")
        meeting = {
            **arc,
            "uid": "meeting",
            "title": "Two-day meeting on X-ray science",
            "start": "2026-11-09T08:30:00-08:00",
            "end": "2026-11-10T17:00:00-08:00",
        }
        payload["events"] = [meeting, *payload["events"]]
        current = open_at(session, harness.NOW, payload)
        assert days_shown(current)[:2] == ["ongoing", "2026-11-12"]
        article = current.page.locator('#events section[data-day="ongoing"] article')
        assert article.evaluate_all("es => es.map((e) => e.dataset.uid)") == ["meeting"]
        span = "Nov 9, 2026, 8:30 AM PST - Nov 10, 2026, 5:00 PM PST"
        assert article.locator(".event-when").text_content() == span
        name = f"Select Two-day meeting on X-ray science Ongoing {span}"
        assert current.page.get_by_role("checkbox", name=name, exact=True).count() == 1
        later = current.page.locator('article[data-uid="sample-xss"] .event-when')
        assert later.text_content() == "12:00 PM - 1:00 PM"  # a day of its own: times only
        current.assert_clean()

    def test_the_heading_is_translated(self, session: Callable[..., Session]) -> None:
        current = session()
        serve_json(current, "events.json", retreat_payload())
        current.open("index.html?lang=zh")
        heading = current.page.locator('#events section[data-day="ongoing"] h2')
        assert heading.text_content() == "\u6b63\u5728\u8fdb\u884c"
        current.assert_clean()
