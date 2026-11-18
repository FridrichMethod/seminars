"""Tests for meetings: what counts as one, the type a title gives, and the window of a meeting."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import get_args

import pytest

from conftest import REPO_ROOT
from factories import PACIFIC, REFERENCE_NOW, make_event, make_source, make_window
from seminars.config import load_settings
from seminars.models import MEETING_MIN_TIMED_HOURS, EventType, is_meeting_span
from seminars.pipeline.meetings import (
    CONFERENCE_WORDS,
    WORKSHOP_WORDS,
    has_not_ended_before,
    in_window,
    meeting_type,
)
from seminars.pipeline.times import EventTimes

SETTINGS = load_settings(REPO_ROOT / "config" / "settings.yaml")
WINDOW = make_window()
MEETING_WINDOW = SETTINGS.meeting_window_at(REFERENCE_NOW)
MORNING = datetime(2026, 12, 2, 9, 0, tzinfo=PACIFIC)
MIDNIGHT = datetime(2026, 12, 2, tzinfo=PACIFIC)
TALK_TYPES = ("seminar", "colloquium", "lecture", "defense", "other")


def timed(hours: float | None, start: datetime = MORNING) -> EventTimes:
    """Times of day from ``start`` for ``hours``; no end when ``hours`` is None."""
    end = None if hours is None else start + timedelta(hours=hours)
    return EventTimes(start=start, end=end, all_day=False)


def all_day(days: int, start: datetime = MIDNIGHT) -> EventTimes:
    """``days`` whole days from ``start``, as ``Event`` stores them (one day has no end)."""
    end = None if days == 1 else start + timedelta(days=days)
    return EventTimes(start=start, end=end, all_day=True)


def span(event_type: EventType, times: EventTimes) -> bool:
    return is_meeting_span(event_type, times.start, times.end, all_day=times.all_day)


class TestIsMeetingSpan:
    @pytest.mark.parametrize(
        "times",
        [timed(1), timed(None), all_day(1), all_day(3)],
        ids=["an hour", "no end", "one day", "three days"],
    )
    def test_a_conference_is_always_a_meeting(self, times: EventTimes) -> None:
        assert span("conference", times)

    def test_a_workshop_over_several_days_is_a_meeting(self) -> None:
        assert span("workshop", all_day(2))
        assert span("workshop", all_day(21))

    def test_a_workshop_of_one_whole_day_is_not(self) -> None:
        # One day of a hackathon that a calendar lists day by day (Big Earth Hackathon).
        assert not span("workshop", all_day(1))

    @pytest.mark.parametrize(
        ("hours", "meeting"),
        [(MEETING_MIN_TIMED_HOURS, True), (8, True), (32, True), (2, False), (2.99, False)],
    )
    def test_a_timed_workshop_is_a_meeting_from_three_hours_on(
        self, hours: float, meeting: bool
    ) -> None:
        assert span("workshop", timed(hours)) is meeting

    def test_a_timed_workshop_without_an_end_is_not(self) -> None:
        assert not span("workshop", timed(None))

    @pytest.mark.parametrize("event_type", TALK_TYPES)
    @pytest.mark.parametrize("times", [timed(8), all_day(3)], ids=["timed", "all day"])
    def test_no_other_type_is_a_meeting(self, event_type: EventType, times: EventTimes) -> None:
        assert not span(event_type, times)

    def test_every_type_is_covered(self) -> None:
        assert {*TALK_TYPES, "conference", "workshop"} == set(get_args(EventType))

    def test_the_event_property_applies_the_rule(self) -> None:
        start = datetime(2026, 12, 2, tzinfo=PACIFIC)
        week = make_event(
            event_type="workshop", all_day=True, start=start, end=start + timedelta(days=5)
        )
        day = make_event(event_type="workshop", all_day=True, start=start, end=None)
        assert week.is_meeting
        assert not day.is_meeting
        assert make_event(event_type="conference").is_meeting
        assert not make_event().is_meeting  # a seminar of an hour


class TestMeetingType:
    @pytest.mark.parametrize(
        "title",
        [
            "Stanford Drug Discovery Symposium 2027",
            "Symposia on protein design",
            "Annual Conference on Machine Learning",
            "Bay Area Biotech Summit",
            "Global and Planetary Health Research Convening",
            "International Congress of Basic Science",
            "Wu Tsai Neurosciences Institute Retreat",
            "AIMI Symposium + Summit Series 2027",
            "SYMPOSIUM: Quantum materials",
            "Symp\u00f3sium on catalysis",
        ],
    )
    @pytest.mark.parametrize("event_type", ["seminar", "lecture", "other"])
    @pytest.mark.parametrize(
        "times", [all_day(1), timed(3), timed(9)], ids=["one day", "three hours", "nine hours"]
    )
    def test_a_long_talk_named_a_conference_becomes_one(
        self, title: str, event_type: EventType, times: EventTimes
    ) -> None:
        assert meeting_type(event_type, title, times) == "conference"

    @pytest.mark.parametrize(
        "title",
        [
            "Workshop on generative models",
            "Single-cell workshops",
            "Python Boot Camp",
            "Diffusion bootcamp",
            "Summer School in Statistics",
            "Winter School on Mechanistic Estimation",
            "Spring School: Number theory",
        ],
    )
    @pytest.mark.parametrize("event_type", ["seminar", "lecture", "other"])
    @pytest.mark.parametrize("times", [all_day(3), timed(4)], ids=["three days", "four hours"])
    def test_a_long_talk_named_a_workshop_becomes_one(
        self, title: str, event_type: EventType, times: EventTimes
    ) -> None:
        assert meeting_type(event_type, title, times) == "workshop"

    def test_a_conference_word_wins_over_a_workshop_word(self) -> None:
        assert meeting_type("seminar", "Symposium and Workshop on RNA", timed(8)) == "conference"

    @pytest.mark.parametrize("hours", [1, 2, 2.99, None])
    def test_a_short_talk_keeps_its_type(self, hours: float | None) -> None:
        # "Symposium Lecture" from 16:00 to 17:00 is a lecture.
        times = timed(hours, datetime(2026, 12, 2, 16, 0, tzinfo=PACIFIC))
        assert meeting_type("lecture", "Symposium Lecture", times) == "lecture"
        assert meeting_type("seminar", "Workshop on single-cell methods", times) == "seminar"

    @pytest.mark.parametrize(
        ("event_type", "title"),
        [
            ("colloquium", "Physics Symposium Colloquium"),
            ("defense", "PhD defense: a workshop on proofs"),
            ("conference", "Workshop on kernels"),
            ("workshop", "Symposium on kernels"),
        ],
    )
    def test_other_types_are_never_changed(self, event_type: EventType, title: str) -> None:
        for times in (all_day(3), timed(8)):
            assert meeting_type(event_type, title, times) == event_type

    @pytest.mark.parametrize("event_type", ["conference", "workshop"])
    def test_a_meeting_is_never_turned_into_a_talk(self, event_type: EventType) -> None:
        for times in (timed(1), timed(None), all_day(1)):
            assert meeting_type(event_type, "Lunch talk", times) == event_type

    @pytest.mark.parametrize(
        "title",
        [
            "Conferencing tools for remote teams",
            "Congressional testimony on AI",
            "Retreating glaciers",
            "Workshopping a paper",
            "Medical School Grand Rounds",
            "Summitville geology",
        ],
    )
    def test_only_whole_words_count(self, title: str) -> None:
        assert meeting_type("seminar", title, all_day(1)) == "seminar"

    @pytest.mark.parametrize("title", [None, ""])
    def test_no_title_changes_nothing(self, title: str | None) -> None:
        assert meeting_type("seminar", title, all_day(3)) == "seminar"

    def test_the_word_lists(self) -> None:
        assert set(CONFERENCE_WORDS) >= {"symposium", "conference", "summit", "convening"}
        assert set(CONFERENCE_WORDS) >= {"congress", "retreat"}
        assert set(WORKSHOP_WORDS) >= {"workshop", "boot camp", "summer school", "winter school"}
        assert all(word == word.lower() for word in (*CONFERENCE_WORDS, *WORKSHOP_WORDS))


class TestEndedBefore:
    MOMENT = datetime(2026, 12, 2, 12, 0, tzinfo=PACIFIC)

    def test_an_event_with_an_end_runs_until_that_end(self) -> None:
        start = self.MOMENT - timedelta(days=10)
        assert has_not_ended_before(start, self.MOMENT + timedelta(seconds=1), self.MOMENT)
        assert not has_not_ended_before(start, self.MOMENT, self.MOMENT)  # the end is exclusive
        assert not has_not_ended_before(start, self.MOMENT - timedelta(hours=1), self.MOMENT)

    def test_an_event_without_an_end_is_judged_by_its_start(self) -> None:
        assert has_not_ended_before(self.MOMENT, None, self.MOMENT)
        assert not has_not_ended_before(self.MOMENT - timedelta(seconds=1), None, self.MOMENT)


class TestInWindow:
    FAR = REFERENCE_NOW + timedelta(days=200)

    @staticmethod
    def inside(times: EventTimes, *, meeting: bool, flagged: bool) -> bool:
        return in_window(
            times,
            meeting=meeting,
            source=make_source(meeting_horizon=flagged),
            window=WINDOW,
            meeting_window=MEETING_WINDOW,
        )

    @pytest.mark.parametrize("flagged", [False, True])
    def test_a_talk_keeps_the_window_of_the_run(self, flagged: bool) -> None:
        assert self.inside(timed(1, WINDOW.start), meeting=False, flagged=flagged)
        assert self.inside(
            timed(1, WINDOW.end - timedelta(hours=1)), meeting=False, flagged=flagged
        )
        assert not self.inside(timed(1, WINDOW.end), meeting=False, flagged=flagged)
        assert not self.inside(timed(1, self.FAR), meeting=False, flagged=flagged)

    def test_a_meeting_of_a_flagged_source_has_the_meeting_window(self) -> None:
        assert self.inside(timed(8, self.FAR), meeting=True, flagged=True)
        assert self.inside(
            timed(None, MEETING_WINDOW.end - timedelta(hours=1)), meeting=True, flagged=True
        )
        assert not self.inside(timed(None, MEETING_WINDOW.end), meeting=True, flagged=True)

    def test_a_meeting_of_another_source_has_the_window_of_the_run(self) -> None:
        assert not self.inside(timed(8, self.FAR), meeting=True, flagged=False)
        assert self.inside(timed(8, WINDOW.end - timedelta(hours=9)), meeting=True, flagged=False)

    @pytest.mark.parametrize(("meeting", "flagged"), [(False, False), (True, True)])
    def test_an_event_counts_until_its_end(self, meeting: bool, flagged: bool) -> None:
        # A workshop of three weeks that began twenty days before the window: still on.
        begun = all_day(21, WINDOW.start - timedelta(days=20))
        over = all_day(21, WINDOW.start - timedelta(days=21))
        assert self.inside(begun, meeting=meeting, flagged=flagged)
        assert not self.inside(over, meeting=meeting, flagged=flagged)
        assert not self.inside(
            timed(None, WINDOW.start - timedelta(hours=1)), meeting=meeting, flagged=flagged
        )

    def test_the_meeting_window_starts_with_the_window_of_the_run(self) -> None:
        assert MEETING_WINDOW.start == WINDOW.start
        assert MEETING_WINDOW.end > WINDOW.end
