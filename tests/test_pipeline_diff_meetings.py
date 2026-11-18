"""Tests for a merged meeting when one of its listings is missing from a run.

De-duplication joins the whole days of a meeting (a curated list, the
registry) with a listing of the hours of one of its days on the same page,
and the merged meeting keeps its days. When the listing of the whole days
is missing, because its source failed or no longer lists the meeting, the
listing of the hours must not shrink the meeting to that day, and the
meeting must not change back when the listing returns
(``seminars.pipeline.diff``).
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from factories import PACIFIC, REFERENCE_NOW, make_draft, make_event, make_window
from fakes import FakeAdapter
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig
from seminars.models import Event, EventDraft
from seminars.pipeline.diff import Reconciled, reconcile
from test_pipeline_run import run, services, source, stored

FIRST_SEEN = REFERENCE_NOW - timedelta(days=5)
DAY = datetime(2026, 12, 1, tzinfo=PACIFIC)
MORNING = DAY + timedelta(hours=9)
PAGE = "https://qbi.example.edu/symposium-2026"
TITLE = "QBI Symposium 2026"
ENTRY = "conf-registry:qbi-2026"
HOURS = "sites-qbi:q1"
ENABLED = frozenset({"conf-registry", "sites-qbi"})


def event(**overrides: Any) -> Event:
    values: dict[str, Any] = {
        "uid": "conf-registry.qbi-2026",
        "title": TITLE,
        "speakers": [],
        "event_type": "conference",
        "url": PAGE,
        "source_urls": [PAGE],
        "first_seen": FIRST_SEEN,
        "last_changed": FIRST_SEEN,
        "sequence": 2,
    }
    return make_event(**{**values, **overrides})


def meeting(**overrides: Any) -> Event:
    """The meeting as stored: the three days of the registry, with the keys of both listings."""
    values: dict[str, Any] = {
        "all_day": True,
        "start": DAY,
        "end": DAY + timedelta(days=3),
        "source_ids": [ENTRY, HOURS],
    }
    return event(**{**values, **overrides})


def hours(day: int = 0, **overrides: Any) -> Event:
    """The calendar's listing of one day of the meeting, 9:00 to 17:00, as a seminar."""
    values: dict[str, Any] = {
        "title": f"{TITLE} - Day {day + 1}",
        "event_type": "seminar",
        "start": MORNING + timedelta(days=day),
        "end": MORNING + timedelta(days=day, hours=8),
        "source_ids": [HOURS],
        "first_seen": REFERENCE_NOW,
        "last_changed": REFERENCE_NOW,
        "sequence": 0,
    }
    return event(**{**values, **overrides})


def entry(**overrides: Any) -> Event:
    """The registry's listing of the meeting alone: its whole days."""
    values: dict[str, Any] = {
        "source_ids": [ENTRY],
        "first_seen": REFERENCE_NOW,
        "last_changed": REFERENCE_NOW,
        "sequence": 0,
    }
    return meeting(**{**values, **overrides})


def reconciled(
    previous: list[Event],
    fresh: list[Event],
    *,
    healthy: frozenset[str] = ENABLED,
    runs: int = 0,
) -> Reconciled:
    now = REFERENCE_NOW + timedelta(days=runs)
    return reconcile(
        previous,
        fresh,
        now=now,
        window=make_window(now),
        healthy_sources=healthy,
        enabled_sources=ENABLED,
        delete_after_missing_runs=3,
    )


def times(event: Event) -> tuple[datetime, datetime | None, bool]:
    return event.start, event.end, event.all_day


class TestTheSourceOfTheWholeDaysSaysNothing:
    @pytest.mark.parametrize("day", [0, 1, 2])
    def test_the_hours_of_one_day_do_not_shorten_the_meeting(self, day: int) -> None:
        before = meeting()
        result = reconciled([before], [hours(day)], healthy=frozenset({"sites-qbi"}))
        assert (result.events, result.changed) == ((before,), 0)

    def test_failure_and_recovery_change_nothing_a_subscriber_sees(self) -> None:
        down = reconciled([meeting()], [hours()], healthy=frozenset({"sites-qbi"}), runs=1)
        back = reconciled(list(down.events), [meeting()], runs=2)
        assert (down.changed, back.changed) == (0, 0)
        assert back.events == (meeting(),)

    def test_the_whole_days_do_not_replace_the_hours_of_a_meeting_stored_with_them(
        self,
    ) -> None:
        # The host leads the merge with its hours over the two days of the registry entry;
        # while the host fails, the entry alone gives the same days without hours.
        span = {"start": MORNING, "end": MORNING + timedelta(days=1, hours=8), "all_day": False}
        before = meeting(**span)
        whole = entry(end=DAY + timedelta(days=2))
        down = reconciled([before], [whole], healthy=frozenset({"conf-registry"}), runs=1)
        assert (down.events, down.changed) == ((before,), 0)
        back = reconciled(list(down.events), [meeting(**span)], runs=2)
        assert (back.events, back.changed) == ((before,), 0)

    @pytest.mark.parametrize(
        ("start", "end"),
        [
            (MORNING, MORNING + timedelta(days=4, hours=8)),  # beyond the last day
            (MORNING + timedelta(days=3), MORNING + timedelta(days=3, hours=8)),  # the day after
            (MORNING, MORNING + timedelta(hours=2)),  # two hours, no day of the meeting
        ],
    )
    def test_other_hours_are_a_new_time(self, start: datetime, end: datetime) -> None:
        result = reconciled(
            [meeting()], [hours(start=start, end=end)], healthy=frozenset({"sites-qbi"})
        )
        (found,) = result.events
        assert times(found) == (start, end, False)
        assert (found.title, found.event_type, result.changed) == (TITLE, "conference", 1)


class TestTheSourceOfTheWholeDaysNoLongerListsIt:
    @pytest.mark.parametrize("day", [0, 2])
    def test_the_meeting_keeps_its_days_its_type_and_its_title(self, day: int) -> None:
        before = meeting()
        result = reconciled([before], [hours(day)])
        (found,) = result.events
        assert found == before.evolve(source_ids=[HOURS])
        assert result.changed == 0

    def test_the_next_runs_change_nothing(self) -> None:
        events = [meeting()]
        changes = []
        for runs in range(1, 4):
            result = reconciled(events, [hours()], runs=runs)
            changes.append(result.changed)
            events = list(result.events)
        assert changes == [0, 0, 0]
        assert events == [meeting(source_ids=[HOURS])]

    def test_the_listing_that_returns_changes_nothing(self) -> None:
        gone = reconciled([meeting()], [hours()], runs=1)
        back = reconciled(list(gone.events), [meeting()], runs=2)
        assert (gone.changed, back.changed) == (0, 0)
        assert back.events == (meeting(),)

    def test_what_else_the_listing_says_goes_through(self) -> None:
        result = reconciled([meeting()], [hours(location="Genentech Hall")])
        (found,) = result.events
        assert (found.location, times(found), found.title) == (
            "Genentech Hall",
            times(meeting()),
            TITLE,
        )
        assert (found.sequence, result.changed) == (3, 1)

    def test_hours_over_every_day_of_the_meeting_go_through(self) -> None:
        span = MORNING, MORNING + timedelta(days=2, hours=8)
        result = reconciled([meeting()], [hours(start=span[0], end=span[1])])
        (found,) = result.events
        assert times(found) == (*span, False)
        assert result.changed == 1

    def test_the_whole_days_replace_the_hours_that_are_gone(self) -> None:
        # Only an all-day meeting keeps its days: the registry entry that is left gives
        # the days of a meeting the host listed with its hours.
        span = {"start": MORNING, "end": MORNING + timedelta(days=1, hours=8), "all_day": False}
        whole = entry(end=DAY + timedelta(days=2))
        result = reconciled([meeting(**span)], [whole])
        (found,) = result.events
        assert times(found) == (DAY, DAY + timedelta(days=2), True)
        assert result.changed == 1

    def test_an_all_day_event_that_is_no_meeting_takes_the_listing(self) -> None:
        # An exhibition of three days and a conference of a day on its page.
        before = meeting(event_type="other")
        assert not before.is_meeting
        fresh = hours(event_type="conference")
        result = reconciled([before], [fresh])
        (found,) = result.events
        assert (times(found), found.event_type) == (times(fresh), "conference")


# --- a whole run ---------------------------------------------------------------------------

REGISTRY_URL = "https://registry.example.edu/meetings"
CALENDAR_URL = "https://calendar.example.edu/events"


@pytest.fixture
def config(app_config: AppConfig) -> AppConfig:
    sources = (
        source("conf-registry", REGISTRY_URL, group="conferences", default_event_type="conference"),
        source("sites-qbi", CALENDAR_URL),
    )
    return dataclasses.replace(app_config, sources=sources)


def draft(source_id: str, native_id: str, **overrides: Any) -> EventDraft:
    values: dict[str, Any] = {
        "source_id": source_id,
        "native_id": native_id,
        "url": PAGE,
        "title": TITLE,
        "speaker_text": None,
        "abstract": None,
        "location": None,
    }
    return make_draft(**{**values, **overrides})


def drafts(*, whole: bool = True) -> list[EventDraft]:
    """The calendar's first day, 9:00 to 17:00, and the registry's three days on its page.

    The registry lists another meeting as well, so it is healthy when it
    no longer lists this one.
    """
    calendar = draft(
        "sites-qbi", "q1", title=f"{TITLE} - Day 1", start=MORNING, end=MORNING + timedelta(hours=8)
    )
    other = draft(
        "conf-registry",
        "other-2027",
        url="https://other.example.edu/meeting-2027",
        title="Other Meeting 2027",
        start=date(2027, 1, 12),
        end=date(2027, 1, 13),
        all_day=True,
    )
    days = draft(
        "conf-registry",
        "qbi-2026",
        start=date(2026, 12, 1),
        end=date(2026, 12, 3),
        all_day=True,
    )
    return [calendar, other, *([days] if whole else [])]


def the_meeting(config: AppConfig) -> Event:
    (found,) = (event for event in stored(config).values() if "sites-qbi:q1" in event.source_ids)
    return found


class TestAWholeRun:
    def test_the_meeting_keeps_its_days_while_the_registry_says_nothing_and_once_it_drops_it(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = drafts()
        run(config, services(FakeEmitters()))
        first = the_meeting(config)
        assert (first.title, first.all_day, first.end) == (TITLE, True, DAY + timedelta(days=3))
        assert sorted(first.source_ids) == ["conf-registry:qbi-2026", "sites-qbi:q1"]
        run(config, services(FakeEmitters()), days=1, source_ids=("sites-qbi",))
        assert the_meeting(config) == first
        run(config, services(FakeEmitters()), days=2)
        assert the_meeting(config) == first
        fake_adapter.drafts = drafts(whole=False)
        for runs in (3, 4):
            run(config, services(FakeEmitters()), days=runs)
            found = the_meeting(config)
            assert found.source_ids == ["sites-qbi:q1"]
            assert found.evolve(source_ids=first.source_ids) == first
