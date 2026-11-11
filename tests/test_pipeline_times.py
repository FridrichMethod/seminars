"""Tests for the time rules of normalization: zones, DST, all-day events, midnight."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from factories import REFERENCE_NOW
from seminars.pipeline.times import EventTimes, TimeError, parse_moment, resolve_times

LA = "America/Los_Angeles"
PST = timezone(timedelta(hours=-8))
PDT = timezone(timedelta(hours=-7))


def resolve(
    start: datetime | date | str,
    end: datetime | date | str | None = None,
    *,
    all_day: bool = False,
    source_zone: str = LA,
) -> EventTimes:
    return resolve_times(
        start,
        end,
        all_day=all_day,
        source_zone=source_zone,
        project_zone=LA,
        reference=REFERENCE_NOW,
    )


def iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


class TestNaiveTimes:
    def test_a_naive_time_is_wall_time_of_the_source(self) -> None:
        times = resolve(datetime(2026, 11, 17, 16, 0), datetime(2026, 11, 17, 17, 0))
        assert iso(times.start) == "2026-11-17T16:00:00-08:00"
        assert iso(times.end) == "2026-11-17T17:00:00-08:00"
        assert times.all_day is False

    def test_the_result_has_a_fixed_offset(self) -> None:
        times = resolve(datetime(2026, 7, 1, 9, 30))
        assert times.start.tzinfo == PDT
        assert times.start.utcoffset() == timedelta(hours=-7)

    def test_a_naive_time_of_a_utc_source(self) -> None:
        times = resolve(datetime(2026, 11, 18, 2, 0), source_zone="UTC")
        assert iso(times.start) == "2026-11-17T18:00:00-08:00"

    def test_an_iso_string_without_zone_is_the_same_as_a_naive_datetime(self) -> None:
        assert resolve("2026-11-17T16:00") == resolve(datetime(2026, 11, 17, 16, 0))


class TestDaylightSavingTime:
    """The clocks go back on 2026-11-01 and forward on 2027-03-14 in America/Los_Angeles."""

    @pytest.mark.parametrize(
        ("naive", "expected"),
        [
            (datetime(2026, 10, 31, 16, 0), "2026-10-31T16:00:00-07:00"),
            (datetime(2026, 11, 1, 0, 30), "2026-11-01T00:30:00-07:00"),
            (datetime(2026, 11, 1, 1, 30), "2026-11-01T01:30:00-07:00"),
            (datetime(2026, 11, 1, 2, 0), "2026-11-01T02:00:00-08:00"),
            (datetime(2026, 11, 1, 16, 0), "2026-11-01T16:00:00-08:00"),
            (datetime(2026, 11, 2, 16, 0), "2026-11-02T16:00:00-08:00"),
        ],
    )
    def test_around_the_day_the_clocks_go_back(self, naive: datetime, expected: str) -> None:
        assert iso(resolve(naive).start) == expected

    @pytest.mark.parametrize(
        ("naive", "expected"),
        [
            (datetime(2027, 3, 13, 16, 0), "2027-03-13T16:00:00-08:00"),
            (datetime(2027, 3, 14, 1, 59), "2027-03-14T01:59:00-08:00"),
            (datetime(2027, 3, 14, 2, 30), "2027-03-14T03:30:00-07:00"),
            (datetime(2027, 3, 14, 3, 0), "2027-03-14T03:00:00-07:00"),
            (datetime(2027, 3, 14, 16, 0), "2027-03-14T16:00:00-07:00"),
            (datetime(2027, 3, 15, 16, 0), "2027-03-15T16:00:00-07:00"),
        ],
    )
    def test_around_the_day_the_clocks_go_forward(self, naive: datetime, expected: str) -> None:
        assert iso(resolve(naive).start) == expected

    def test_both_occurrences_of_the_repeated_hour_can_be_stated_in_utc(self) -> None:
        first = resolve(datetime(2026, 11, 1, 8, 30, tzinfo=UTC))
        second = resolve(datetime(2026, 11, 1, 9, 30, tzinfo=UTC))
        assert iso(first.start) == "2026-11-01T01:30:00-07:00"
        assert iso(second.start) == "2026-11-01T01:30:00-08:00"
        assert second.start - first.start == timedelta(hours=1)

    def test_a_talk_across_the_change_keeps_its_real_length(self) -> None:
        times = resolve(datetime(2026, 10, 31, 23, 0), datetime(2026, 11, 1, 3, 0))
        assert iso(times.start) == "2026-10-31T23:00:00-07:00"
        assert iso(times.end) == "2026-11-01T03:00:00-08:00"
        assert times.end is not None
        assert times.end - times.start == timedelta(hours=5)

    def test_a_weekly_talk_keeps_its_wall_time_across_both_changes(self) -> None:
        for day in (date(2026, 10, 27), date(2026, 11, 3), date(2027, 3, 9), date(2027, 3, 16)):
            start = resolve(datetime(day.year, day.month, day.day, 16, 15)).start
            assert (start.hour, start.minute) == (16, 15)


class TestUtcInput:
    def test_utc_times_become_local_wall_time(self) -> None:
        times = resolve("2026-11-18T00:00:00+00:00", "2026-11-18T01:00:00Z")
        assert iso(times.start) == "2026-11-17T16:00:00-08:00"
        assert iso(times.end) == "2026-11-17T17:00:00-08:00"

    def test_the_local_day_can_differ_from_the_utc_day(self) -> None:
        times = resolve(datetime(2026, 7, 2, 1, 0, tzinfo=UTC))
        assert iso(times.start) == "2026-07-01T18:00:00-07:00"

    def test_any_offset_is_converted(self) -> None:
        offset = timezone(timedelta(hours=5, minutes=30))
        times = resolve(datetime(2026, 11, 18, 5, 30, tzinfo=offset))
        assert iso(times.start) == "2026-11-17T16:00:00-08:00"

    def test_microseconds_are_dropped(self) -> None:
        times = resolve(datetime(2026, 11, 17, 16, 0, 5, 999, tzinfo=PST))
        assert times.start == datetime(2026, 11, 17, 16, 0, 5, tzinfo=PST)


class TestMidnight:
    def test_a_talk_that_runs_past_midnight(self) -> None:
        times = resolve(datetime(2026, 11, 17, 23, 30), datetime(2026, 11, 18, 0, 30))
        assert iso(times.start) == "2026-11-17T23:30:00-08:00"
        assert iso(times.end) == "2026-11-18T00:30:00-08:00"

    def test_an_end_time_next_to_the_start_date_moves_to_the_next_day(self) -> None:
        times = resolve(datetime(2026, 11, 17, 23, 0), datetime(2026, 11, 17, 1, 0))
        assert iso(times.end) == "2026-11-18T01:00:00-08:00"

    def test_an_implausible_end_is_dropped(self) -> None:
        assert resolve(datetime(2026, 11, 17, 16, 0), datetime(2026, 11, 17, 3, 0)).end is None
        assert resolve(datetime(2026, 11, 17, 16, 0), datetime(2026, 11, 10, 17, 0)).end is None
        assert resolve(datetime(2026, 11, 17, 16, 0), datetime(2026, 11, 17, 16, 0)).end is None

    def test_utc_midnight_is_not_local_midnight(self) -> None:
        times = resolve(
            datetime(2026, 11, 18, 7, 30, tzinfo=UTC), datetime(2026, 11, 18, 8, 30, tzinfo=UTC)
        )
        assert iso(times.start) == "2026-11-17T23:30:00-08:00"
        assert iso(times.end) == "2026-11-18T00:30:00-08:00"

    def test_a_date_as_the_end_of_a_timed_event_means_the_end_of_that_day(self) -> None:
        times = resolve(datetime(2026, 11, 17, 9, 0), date(2026, 11, 19))
        assert iso(times.end) == "2026-11-20T00:00:00-08:00"
        assert times.all_day is False


class TestAllDay:
    def test_a_date_makes_an_all_day_event_at_local_midnight(self) -> None:
        times = resolve(date(2026, 11, 17))
        assert times == EventTimes(
            start=datetime(2026, 11, 17, 0, 0, tzinfo=PST), end=None, all_day=True
        )

    def test_a_date_only_string_is_all_day_and_never_midnight_of_a_timed_event(self) -> None:
        assert resolve("2026-11-17").all_day is True
        assert resolve("November 17, 2026").all_day is True
        assert resolve("November 17, 2026 4:00 PM").all_day is False

    def test_the_inclusive_last_day_becomes_an_exclusive_end(self) -> None:
        times = resolve(date(2026, 11, 17), date(2026, 11, 19))
        assert iso(times.start) == "2026-11-17T00:00:00-08:00"
        assert iso(times.end) == "2026-11-20T00:00:00-08:00"

    def test_the_start_day_as_the_end_means_one_day(self) -> None:
        assert resolve(date(2026, 11, 17), date(2026, 11, 17)).end is None
        assert resolve(date(2026, 11, 17), date(2026, 11, 1)).end is None

    @pytest.mark.parametrize("end", [datetime(2026, 11, 20, 0, 0), datetime(2026, 11, 19, 23, 59)])
    def test_a_datetime_end_names_the_day_just_before_it(self, end: datetime) -> None:
        times = resolve(date(2026, 11, 17), end)
        assert iso(times.end) == "2026-11-20T00:00:00-08:00"

    def test_the_flag_with_a_timed_start_uses_the_day_of_the_source(self) -> None:
        flagged = resolve(datetime(2026, 11, 17, 0, 0, tzinfo=PST), all_day=True)
        assert iso(flagged.start) == "2026-11-17T00:00:00-08:00"
        from_utc = resolve(
            datetime(2026, 11, 17, 0, 0, tzinfo=UTC), all_day=True, source_zone="UTC"
        )
        assert iso(from_utc.start) == "2026-11-17T00:00:00-08:00"

    def test_all_day_events_across_the_dst_changes_start_at_local_midnight(self) -> None:
        autumn = resolve(date(2026, 10, 31), date(2026, 11, 2))
        assert iso(autumn.start) == "2026-10-31T00:00:00-07:00"
        assert iso(autumn.end) == "2026-11-03T00:00:00-08:00"
        spring = resolve(date(2027, 3, 14))
        assert iso(spring.start) == "2027-03-14T00:00:00-08:00"


class TestFreeText:
    def test_common_spellings(self) -> None:
        assert parse_moment("Nov 17, 2026 4:00 PM", reference=REFERENCE_NOW) == datetime(
            2026, 11, 17, 16, 0
        )
        assert parse_moment("Tuesday, November 17, 2026", reference=REFERENCE_NOW) == date(
            2026, 11, 17
        )

    def test_the_reference_supplies_a_missing_year(self) -> None:
        assert parse_moment("Nov 17 4pm", reference=REFERENCE_NOW) == datetime(2026, 11, 17, 16)

    def test_zone_abbreviations(self) -> None:
        times = resolve("November 17, 2026 at 7:00 pm ET")
        assert iso(times.start) == "2026-11-17T16:00:00-08:00"
        assert iso(resolve("2026-07-01 16:00 PST").start) == "2026-07-01T16:00:00-07:00"

    @pytest.mark.parametrize(
        "text", ["garbage", "Thursdays at noon-ish maybe", "9" * 200, "31/31/2026"]
    )
    def test_an_unusable_start_is_an_error(self, text: str) -> None:
        with pytest.raises(TimeError):
            resolve(text)

    def test_an_unusable_end_is_dropped(self) -> None:
        assert resolve(datetime(2026, 11, 17, 16, 0), "whenever").end is None

    def test_times_at_the_edge_of_the_calendar_are_an_error_not_a_crash(self) -> None:
        with pytest.raises(TimeError):
            resolve(datetime(1, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=14))))
        with pytest.raises(TimeError):
            resolve(date(9999, 12, 30), date(9999, 12, 31))
