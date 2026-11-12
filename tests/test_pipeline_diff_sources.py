"""Tests for what the sources of a stored event say about it when they are not all there.

* A source that is disabled or removed from the configuration says the
  same as a deleted source: its keys leave the stored events, and an
  event that has no key left leaves the feeds at once.
* A source that is configured but was not read in full in a run (it
  failed, came back empty, was not part of the run, or delivered only
  part of its listing) says nothing: an event that it shares with
  another source keeps what it showed until the source is back.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from factories import REFERENCE_NOW, make_event, make_window
from seminars.models import Event
from seminars.pipeline.diff import Reconciled, reconcile

FIRST_SEEN = REFERENCE_NOW - timedelta(days=5)
MATH = "https://mathematics.example.edu/events/probability-seminar-8"
STATS = "https://statistics.example.edu/events/title-come-9"
ENABLED = frozenset({"sites-math", "sites-stats", "localist-stanford"})


def stored(uid: str = "e1", **overrides: Any) -> Event:
    values: dict[str, Any] = {
        "uid": uid,
        "source_ids": [f"sites-math:{uid}"],
        "first_seen": FIRST_SEEN,
        "last_changed": FIRST_SEEN,
        "sequence": 2,
    }
    return make_event(**{**values, **overrides})


def merged(**overrides: Any) -> Event:
    """The talk as both departments list it, merged into one event."""
    values: dict[str, Any] = {
        "title": "Probability Seminar",
        "location": "Sloan Hall 380",
        "abstract": "A long abstract from the mathematics page.",
        "url": MATH,
        "source_urls": [MATH, STATS],
        "source_ids": ["sites-math:e1", "sites-stats:9"],
        "fields": ["math", "ai-ml"],
        "is_online": True,
    }
    return stored(**{**values, **overrides})


def stats_only(**overrides: Any) -> Event:
    """The same talk as the statistics listing alone gives it."""
    values: dict[str, Any] = {
        "title": "TBA",
        "location": None,
        "abstract": None,
        "url": STATS,
        "source_urls": [STATS],
        "source_ids": ["sites-stats:9"],
        "fields": ["math"],
        "is_online": False,
        "first_seen": REFERENCE_NOW,
        "last_changed": REFERENCE_NOW,
        "sequence": 0,
    }
    return stored(**{**values, **overrides})


def run(
    previous: list[Event],
    fresh: list[Event],
    *,
    healthy: frozenset[str] = ENABLED,
    enabled: frozenset[str] | None = ENABLED,
    now: datetime = REFERENCE_NOW,
) -> Reconciled:
    return reconcile(
        previous,
        fresh,
        now=now,
        window=make_window(now),
        healthy_sources=healthy,
        enabled_sources=enabled,
        delete_after_missing_runs=3,
    )


def later(runs: int) -> datetime:
    return REFERENCE_NOW + timedelta(days=runs)


class TestAMergedEventWhileOneSourceIsDown:
    def test_the_stored_version_stands_in_for_the_listing_that_is_missing(self) -> None:
        before = merged()
        result = run([before], [stats_only()], healthy=frozenset({"sites-stats"}))
        (event,) = result.events
        assert event == before
        assert (result.changed, result.added) == (0, 0)

    def test_failure_and_recovery_change_nothing_a_subscriber_sees(self) -> None:
        events = [merged()]
        down = run(events, [stats_only()], healthy=frozenset({"sites-stats"}), now=later(1))
        back = run(list(down.events), [merged()], now=later(2))
        (event,) = back.events
        assert event == merged()
        assert (down.changed, back.changed) == (0, 0)

    def test_new_keys_of_the_run_are_taken_up(self) -> None:
        fresh = stats_only(source_ids=["localist-stanford:77", "sites-stats:9"])
        result = run([merged()], [fresh], healthy=frozenset({"sites-stats", "localist-stanford"}))
        (event,) = result.events
        assert sorted(event.source_ids) == [
            "localist-stanford:77",
            "sites-math:e1",
            "sites-stats:9",
        ]
        assert event.title == "Probability Seminar"

    def test_a_cancellation_still_goes_through(self) -> None:
        result = run(
            [merged()], [stats_only(status="cancelled")], healthy=frozenset({"sites-stats"})
        )
        (event,) = result.events
        assert (event.status, event.title, event.location) == (
            "cancelled",
            "Probability Seminar",
            "Sloan Hall 380",
        )
        assert (event.sequence, event.last_changed, result.changed) == (3, REFERENCE_NOW, 1)

    def test_a_started_talk_keeps_its_content_as_well(self) -> None:
        start = REFERENCE_NOW - timedelta(minutes=30)
        before = merged(start=start, end=None)
        result = run([before], [stats_only(start=start, end=None)], healthy=frozenset())
        assert result.events == (before,)

    def test_a_source_that_read_in_full_and_dropped_the_talk_counts(self) -> None:
        result = run([merged()], [stats_only()])
        (event,) = result.events
        assert (event.title, str(event.url), result.changed) == ("TBA", STATS, 1)
        assert event.sequence == 3


class TestDisabledOrRemovedSources:
    def test_an_event_of_a_removed_source_leaves_at_once(self) -> None:
        gone = stored("b2", source_ids=["sites-gone:b2"])
        result = run([gone, stored()], [], enabled=ENABLED)
        assert [event.uid for event in result.events] == ["e1"]
        assert (result.removed, result.missing) == (1, 1)

    def test_a_cancelled_event_of_a_removed_source_leaves_too(self) -> None:
        cancelled = stored("c1", source_ids=["sites-gone:c1"], status="cancelled")
        result = run([cancelled], [], enabled=ENABLED)
        assert (result.events, result.archived, result.removed) == ((), (), 1)

    def test_a_started_event_of_a_removed_source_leaves_the_feeds_for_the_archive(self) -> None:
        started = stored(
            "c2", source_ids=["sites-gone:c2"], start=REFERENCE_NOW - timedelta(hours=1), end=None
        )
        result = run([started], [], enabled=ENABLED)
        assert (result.events, result.archived, result.removed) == ((), (started,), 0)

    def test_a_started_cancelled_event_of_a_removed_source_is_archived_as_it_is(self) -> None:
        started = stored(
            "c3",
            source_ids=["sites-gone:c3"],
            start=REFERENCE_NOW - timedelta(days=2),
            end=None,
            status="cancelled",
        )
        result = run([started], [], enabled=ENABLED)
        assert (result.events, result.archived, result.removed) == ((), (started,), 0)

    def test_a_past_event_of_a_removed_source_is_still_archived(self) -> None:
        old = stored(
            "old", source_ids=["sites-gone:old"], start=REFERENCE_NOW - timedelta(days=20), end=None
        )
        result = run([old], [], enabled=ENABLED)
        assert (result.events, result.archived, result.removed) == ((), (old,), 0)

    def test_the_keys_of_a_removed_source_are_dropped(self) -> None:
        both = stored(source_ids=["sites-gone:1", "sites-math:e1"])
        result = run([both], [], healthy=frozenset(), enabled=ENABLED)
        (event,) = result.events
        assert event.source_ids == ["sites-math:e1"]
        assert (event.missing_runs, event.sequence) == (0, 2)

    def test_a_merged_talk_that_the_other_source_dropped_is_removed(self) -> None:
        both = stored(source_ids=["sites-gone:1", "sites-math:e1"])
        events = [both]
        history = []
        for number in range(1, 4):
            result = run(events, [], now=later(number))
            history.append((len(result.events), result.removed))
            events = list(result.events)
        assert history == [(1, 0), (1, 0), (0, 1)]

    def test_a_delivered_event_loses_the_content_of_a_removed_source(self) -> None:
        before = merged(source_ids=["sites-gone:e1", "sites-stats:9"])
        result = run([before], [stats_only()])
        (event,) = result.events
        assert (event.title, event.source_ids, result.changed) == ("TBA", ["sites-stats:9"], 1)

    def test_without_the_list_every_source_counts_as_configured(self) -> None:
        gone = stored("b2", source_ids=["sites-gone:b2"])
        result = run([gone], [], healthy=frozenset(), enabled=None)
        assert result.events == (gone,)
