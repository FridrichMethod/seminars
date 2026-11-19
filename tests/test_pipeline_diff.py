"""Tests for bringing the stored events up to date: changes, absences, removal, the archive."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from factories import PACIFIC, REFERENCE_NOW, make_event, make_window
from seminars.models import Event
from seminars.pipeline.diff import (
    TRACKED_FIELDS,
    UNTRACKED_FIELDS,
    Reconciled,
    reconcile,
    visible_content,
)

WINDOW = make_window()
FIRST_SEEN = REFERENCE_NOW - timedelta(days=5)
HEALTHY = frozenset({"sites-x", "localist-stanford"})


def stored(uid: str = "e1", **overrides: Any) -> Event:
    values: dict[str, Any] = {
        "uid": uid,
        "source_ids": [f"sites-x:{uid}"],
        "first_seen": FIRST_SEEN,
        "last_changed": FIRST_SEEN,
        "sequence": 2,
    }
    return make_event(**{**values, **overrides})


def run(
    previous: list[Event],
    fresh: list[Event],
    *,
    now: datetime = REFERENCE_NOW,
    healthy: frozenset[str] = HEALTHY,
    **options: Any,
) -> Reconciled:
    return reconcile(
        previous,
        fresh,
        now=now,
        window=make_window(now),
        healthy_sources=healthy,
        delete_after_missing_runs=3,
        **options,
    )


def later(runs: int) -> datetime:
    """The time of a run ``runs`` days after the reference."""
    return REFERENCE_NOW + timedelta(days=runs)


class TestDelivered:
    def test_a_new_event_is_added(self) -> None:
        fresh = stored(first_seen=later(9), last_changed=later(9), sequence=7, missing_runs=2)
        result = run([], [fresh])
        (event,) = result.events
        assert (result.added, result.changed) == (1, 0)
        assert event.first_seen == event.last_changed == REFERENCE_NOW
        assert (event.sequence, event.missing_runs) == (0, 0)

    def test_an_unchanged_event_keeps_its_bookkeeping(self) -> None:
        before = stored()
        result = run([before], [stored(first_seen=REFERENCE_NOW, last_changed=REFERENCE_NOW)])
        assert result.events == (before,)
        assert (result.added, result.changed, result.missing, result.removed) == (0, 0, 0, 0)

    def test_a_title_that_is_announced_keeps_the_uid_and_raises_the_sequence(self) -> None:
        before = stored(title="TBA")
        result = run([before], [stored(title="Protein design with diffusion models")])
        (event,) = result.events
        assert result.changed == 1
        assert event.uid == before.uid
        assert event.title == "Protein design with diffusion models"
        assert event.sequence == before.sequence + 1
        assert event.last_changed == REFERENCE_NOW
        assert event.first_seen == FIRST_SEEN

    @pytest.mark.parametrize(
        "change",
        [
            {"start": datetime(2026, 11, 17, 15, 0, tzinfo=PACIFIC)},
            {"end": None},
            {"location": "Hall B"},
            {"status": "cancelled"},
            {"abstract": "New abstract."},
            {"speakers": []},
            {"url": "https://example.edu/moved"},
            {"source_urls": ["https://example.edu/moved"]},
            {"fields": ["bio", "chem"]},
            {"picks_reasons": ["keyword: protein design"]},
            {"is_online": True},
            {"series": "Another Series"},
            {"all_day": True},
        ],
    )
    def test_every_visible_change_counts(self, change: dict[str, Any]) -> None:
        result = run([stored()], [stored(**change)])
        assert result.changed == 1
        assert result.events[0].sequence == 3

    @pytest.mark.parametrize(
        "change",
        [
            {"source_ids": ["localist-stanford:9", "sites-x:e1"]},
            {"field_tag_method": "llm"},
            {"picks_score": 4.5},
            {"extraction": "api"},
        ],
    )
    def test_internal_values_are_updated_without_counting_as_a_change(
        self, change: dict[str, Any]
    ) -> None:
        result = run([stored()], [stored(**change)])
        (event,) = result.events
        assert result.changed == 0
        assert (event.sequence, event.last_changed) == (2, FIRST_SEEN)
        (name,) = change
        assert getattr(event, name) == getattr(stored(**change), name)

    def test_a_delivered_event_is_no_longer_missing(self) -> None:
        result = run([stored(missing_runs=2)], [stored()])
        assert result.events[0].missing_runs == 0
        assert (result.changed, result.missing) == (0, 0)

    def test_the_fields_that_count_are_the_ones_a_reader_sees(self) -> None:
        assert set(Event.model_fields) == TRACKED_FIELDS | UNTRACKED_FIELDS
        assert not TRACKED_FIELDS & UNTRACKED_FIELDS
        assert set(visible_content(stored())) == TRACKED_FIELDS
        assert {"title", "start", "status", "fields", "picks_reasons", "source_urls"} <= (
            TRACKED_FIELDS
        )


class TestMissingRuns:
    def test_a_future_event_is_removed_after_three_healthy_runs_without_it(self) -> None:
        events = [stored()]
        history = []
        for number in range(1, 4):
            result = run(events, [], now=later(number))
            history.append((len(result.events), result.missing, result.removed))
            events = list(result.events)
        assert history == [(1, 1, 0), (1, 1, 0), (0, 0, 1)]

    def test_missing_runs_counts_up(self) -> None:
        first = run([stored()], [])
        assert first.events[0].missing_runs == 1
        second = run(list(first.events), [])
        assert second.events[0].missing_runs == 2
        assert second.events[0].evolve(missing_runs=0) == stored()

    def test_reappearing_resets_the_count(self) -> None:
        absent = run([stored()], []).events
        absent = run(list(absent), []).events
        assert absent[0].missing_runs == 2
        back = run(list(absent), [stored()])
        assert back.events[0].missing_runs == 0
        again = run(list(back.events), [])
        assert again.events[0].missing_runs == 1
        assert again.removed == 0

    @pytest.mark.parametrize("healthy", [frozenset(), frozenset({"localist-stanford"})])
    def test_an_unhealthy_source_says_nothing_about_its_events(
        self, healthy: frozenset[str]
    ) -> None:
        events = [stored(missing_runs=2)]
        for number in range(1, 6):
            result = run(events, [], now=later(number), healthy=healthy)
            assert result.events == (stored(missing_runs=2),)
            assert (result.missing, result.removed) == (1, 0)
            events = list(result.events)

    def test_every_source_of_a_merged_event_must_be_healthy(self) -> None:
        merged = stored(source_ids=["localist-stanford:9", "sites-x:e1"])
        one_down = run([merged], [], healthy=frozenset({"sites-x"}))
        assert one_down.events[0].missing_runs == 0
        both_up = run([merged], [])
        assert both_up.events[0].missing_runs == 1

    def test_skipped_days_change_nothing(self) -> None:
        """Only runs count: a week without a run is one run, not seven."""
        result = run([stored(start=datetime(2026, 12, 15, 16, 0, tzinfo=PACIFIC), end=None)], [])
        after_a_week = run(list(result.events), [], now=later(7))
        assert after_a_week.events[0].missing_runs == 2
        assert after_a_week.removed == 0

    def test_the_threshold_is_configurable(self) -> None:
        result = reconcile(
            [stored()],
            [],
            now=REFERENCE_NOW,
            window=WINDOW,
            healthy_sources=HEALTHY,
            delete_after_missing_runs=1,
        )
        assert (result.events, result.removed) == ((), 1)


class TestCancelledAndPast:
    def test_a_cancelled_event_is_kept_until_its_time_has_passed(self) -> None:
        cancelled = stored(status="cancelled")
        events = [cancelled]
        for number in range(1, 6):
            result = run(events, [], now=later(number))
            assert result.events == (cancelled,)
            assert (result.missing, result.removed) == (0, 0)
            events = list(result.events)
        after = run(events, [], now=later(10))
        assert after.events == (cancelled,)
        assert after.archived == ()

    def test_a_cancellation_reported_by_the_source_is_a_change(self) -> None:
        result = run([stored()], [stored(status="cancelled")])
        (event,) = result.events
        assert (event.status, event.sequence, result.changed) == ("cancelled", 3, 1)

    def test_a_cancelled_event_can_be_confirmed_again(self) -> None:
        result = run([stored(status="cancelled")], [stored()])
        assert (result.events[0].status, result.changed) == ("confirmed", 1)

    def test_an_event_that_has_started_is_not_missing(self) -> None:
        running = stored(start=REFERENCE_NOW - timedelta(minutes=10), end=None)
        past = stored("e2", start=REFERENCE_NOW - timedelta(days=3), end=None, missing_runs=1)
        result = run([running, past], [])
        assert result.events == (running, past)
        assert (result.missing, result.removed) == (0, 0)


class TestExcludedAndAbsorbed:
    def test_an_event_the_run_excluded_is_removed_at_once(self) -> None:
        result = run([stored(), stored("e2")], [], excluded_uids={"e1"})
        assert [event.uid for event in result.events] == ["e2"]
        assert (result.removed, result.missing) == (1, 1)

    def test_an_absorbed_uid_disappears_without_being_counted(self) -> None:
        survivor = stored("e2", source_ids=["sites-x:e1", "sites-x:e2"])
        result = run([stored(), stored("e2")], [survivor], absorbed={"e1": "e2"})
        assert [event.uid for event in result.events] == ["e2"]
        assert (result.removed, result.missing, result.added) == (0, 0, 0)


class TestArchive:
    def test_events_before_the_window_move_to_the_archive(self) -> None:
        old = stored("old", start=WINDOW.start - timedelta(minutes=1), end=None)
        edge = stored("edge", start=WINDOW.start, end=None)
        result = run([old, edge], [])
        assert result.archived == (old,)
        assert result.events == (edge,)
        assert result.removed == 0

    def test_an_event_leaves_the_window_fourteen_days_after_it_took_place(self) -> None:
        event = stored()
        assert run([event], [], now=later(21)).archived == ()
        assert run([event], [], now=later(22)).archived == (event,)

    def test_a_cancelled_event_is_archived_like_any_other(self) -> None:
        cancelled = stored(status="cancelled")
        assert run([cancelled], [], now=later(30)).archived == (cancelled,)

    def test_a_meeting_that_is_still_running_stays(self) -> None:
        # A workshop of three weeks that began twenty days before the window starts.
        start = WINDOW.start - timedelta(days=20)
        running = stored(
            "running",
            event_type="workshop",
            all_day=True,
            start=start,
            end=start + timedelta(days=21),
        )
        result = run([running], [])
        assert running.is_meeting
        assert (result.archived, result.events, result.missing) == ((), (running,), 0)

    def test_an_event_that_ended_before_the_window_starts_moves_to_the_archive(self) -> None:
        start = WINDOW.start - timedelta(days=21)
        ended = stored("ended", event_type="workshop", all_day=True, start=start, end=WINDOW.start)
        late_talk = stored(
            "late", start=WINDOW.start - timedelta(hours=2), end=WINDOW.start - timedelta(hours=1)
        )
        result = run([ended, late_talk], [])
        assert result.archived == (ended, late_talk)  # sorted by uid
        assert result.events == ()

    def test_the_end_decides_for_a_talk_as_well(self) -> None:
        # A talk that runs past the midnight at which the window starts.
        night = stored(
            "night", start=WINDOW.start - timedelta(hours=1), end=WINDOW.start + timedelta(hours=1)
        )
        assert run([night], []).events == (night,)

    def test_an_event_without_an_end_is_judged_by_its_start(self) -> None:
        day = WINDOW.start - timedelta(days=1)
        before = stored("before", all_day=True, start=day, end=None)
        first = stored("first", all_day=True, start=WINDOW.start, end=None)
        result = run([before, first], [])
        assert (result.archived, result.events) == ((before,), (first,))


class TestResult:
    def test_events_are_sorted_by_uid(self) -> None:
        result = run([stored("b"), stored("d")], [stored("c"), stored("a")])
        assert [event.uid for event in result.events] == ["a", "b", "c", "d"]
        assert (result.added, result.missing) == (2, 2)

    def test_running_twice_with_the_same_input_changes_nothing(self) -> None:
        fresh = [stored("a"), stored("b", title="TBA")]
        first = run([], fresh)
        second = run(list(first.events), fresh, now=later(1))
        assert second.events == first.events
        assert (second.added, second.changed) == (0, 0)

    def test_nothing_at_all(self) -> None:
        assert run([], []) == Reconciled()
