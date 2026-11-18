"""Tests for the time rules of normalization: zones, DST, all-day events, midnight."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from factories import REFERENCE_NOW
from seminars.pipeline.times import (
    MAX_TIMED_LENGTH,
    EventTimes,
    TimeError,
    parse_moment,
    resolve_times,
)

LA = "America/Los_Angeles"
PST = timezone(timedelta(hours=-8))
PDT = timezone(timedelta(hours=-7))
AOE = timezone(timedelta(hours=-12))


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


class TestLength:
    """An end at the same wall time on a later day is a slip of the source, not a long talk."""

    def test_an_end_at_the_same_time_on_the_next_day_is_dropped(self) -> None:
        # The Wu Tsai Neurosciences Seminar on the campus calendar: 12pm to 12pm next day.
        times = resolve(
            datetime(2026, 12, 3, 12, 0, tzinfo=PST), datetime(2026, 12, 4, 12, 0, tzinfo=PST)
        )
        assert (iso(times.start), times.end, times.all_day) == (
            "2026-12-03T12:00:00-08:00",
            None,
            False,
        )
        assert times.end_dropped_as_too_long

    @pytest.mark.parametrize(
        ("start", "end"),
        [
            # Across the change to daylight saving time the same wall time is 23 hours later.
            (datetime(2027, 3, 13, 12, 0), datetime(2027, 3, 14, 12, 0)),
            # Across the change back it is 25 hours later.
            (datetime(2026, 10, 31, 16, 0), datetime(2026, 11, 1, 16, 0)),
            (datetime(2026, 11, 17, 16, 0), datetime(2026, 11, 19, 16, 0)),
        ],
    )
    def test_whole_days_are_dropped_also_across_the_clock_changes(
        self, start: datetime, end: datetime
    ) -> None:
        times = resolve(start, end)
        assert times.end is None
        assert times.end_dropped_as_too_long

    def test_a_timed_event_over_several_days_keeps_its_end(self) -> None:
        times = resolve(datetime(2026, 12, 3, 9, 0), datetime(2026, 12, 5, 17, 0))
        assert iso(times.end) == "2026-12-05T17:00:00-08:00"
        assert not times.end_dropped_as_too_long

    def test_a_timed_span_of_days_from_midnight_keeps_its_end(self) -> None:
        times = resolve(datetime(2026, 12, 3, 0, 0), datetime(2026, 12, 4, 0, 0))
        assert iso(times.end) == "2026-12-04T00:00:00-08:00"

    def test_an_end_more_than_the_longest_length_after_the_start_is_dropped(self) -> None:
        start = datetime(2026, 12, 3, 9, 0)
        longest = start + MAX_TIMED_LENGTH
        assert iso(resolve(start, longest - timedelta(hours=1)).end) == "2026-12-10T08:00:00-08:00"
        times = resolve(start, longest + timedelta(hours=8))
        assert times.end is None
        assert times.end_dropped_as_too_long
        assert resolve(start, date(2026, 12, 20)).end is None

    def test_an_ordinary_end_is_not_marked(self) -> None:
        times = resolve(datetime(2026, 11, 17, 16, 0), datetime(2026, 11, 17, 17, 0))
        assert not times.end_dropped_as_too_long
        assert not resolve(datetime(2026, 11, 17, 16, 0), "never").end_dropped_as_too_long

    def test_all_day_events_over_several_days_keep_their_end(self) -> None:
        times = resolve(date(2026, 12, 3), date(2026, 12, 4))
        assert iso(times.end) == "2026-12-05T00:00:00-08:00"
        times = resolve(datetime(2026, 12, 3, 0, 0), datetime(2026, 12, 5, 0, 0), all_day=True)
        assert iso(times.end) == "2026-12-05T00:00:00-08:00"
        assert not times.end_dropped_as_too_long


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

    @pytest.mark.parametrize(
        ("text", "reference", "expected"),
        [
            (
                "Jan 15, 4:00 PM",
                datetime(2026, 12, 10, 9, tzinfo=PST),
                datetime(2027, 1, 15, 16),
            ),
            ("Dec 30 4pm", datetime(2027, 1, 4, 9, tzinfo=PST), datetime(2026, 12, 30, 16)),
            ("June 3 at 2 pm", datetime(2026, 11, 11, 9, tzinfo=PST), datetime(2026, 6, 3, 14)),
            ("June 3 at 2 pm", datetime(2027, 1, 20, 9, tzinfo=PST), datetime(2027, 6, 3, 14)),
        ],
    )
    def test_a_missing_year_is_the_one_closest_to_the_reference(
        self, text: str, reference: datetime, expected: datetime
    ) -> None:
        assert parse_moment(text, reference=reference) == expected

    def test_a_missing_year_of_a_date_without_time(self) -> None:
        december = datetime(2026, 12, 20, 9, tzinfo=PST)
        assert parse_moment("Thursday, January 7", reference=december) == date(2027, 1, 7)

    def test_a_given_year_is_kept_however_far_away(self) -> None:
        december = datetime(2026, 12, 20, 9, tzinfo=PST)
        assert parse_moment("Jan 7, 2026 4 PM", reference=december) == datetime(2026, 1, 7, 16)

    def test_february_29_without_a_year_needs_a_leap_year_nearby(self) -> None:
        winter = datetime(2027, 12, 1, 9, tzinfo=PST)
        assert parse_moment("Feb 29 4pm", reference=winter) == datetime(2028, 2, 29, 16)
        with pytest.raises(TimeError):
            parse_moment("Feb 29 4pm", reference=datetime(2026, 6, 1, 9, tzinfo=PST))

    @pytest.mark.parametrize("text", ["4pm", "Thursday 4pm", "March 2026", "the 15th at noon"])
    def test_a_text_without_month_and_day_is_an_error(self, text: str) -> None:
        with pytest.raises(TimeError):
            parse_moment(text, reference=REFERENCE_NOW)

    def test_an_end_with_only_a_time_is_dropped(self) -> None:
        times = resolve("Jan 1, 2027 4pm", "5pm")
        assert iso(times.start) == "2027-01-01T16:00:00-08:00"
        assert times.end is None

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


class TestAnywhereOnEarth:
    """AoE is UTC-12 all year: 23:59 AoE is 11:59 UTC of the next day."""

    @pytest.mark.parametrize(
        "text",
        [
            "Jan 29, 2027 23:59 AoE",
            "Jan 29, 2027 23:59 AOE",
            "Jan 29, 2027 23:59 aoe",
            "Jan 29, 2027 23:59 (AoE)",
            "Jan 29, 2027 23:59 ( AoE )",
            "Jan 29, 2027 23:59 Anywhere on Earth",
            "Jan 29, 2027 23:59 (Anywhere on Earth)",
            "Jan 29, 2027 23:59 (anywhere on earth)",
            "Jan 29, 2027 23:59 ANYWHERE ON EARTH",
            "Jan 29, 2027 23:59 Anywhere-on-Earth",
            "Jan 29, 2027 23:59 Anywhere on Earth (AoE)",
            "Jan 29, 2027 23:59 AoE (UTC-12)",
            "Jan 29, 2027 23:59 (Anywhere on Earth, UTC-12)",
            "Jan 29, 2027 23:59 (AoE/UTC-12)",
            "Jan 29, 2027 23:59 -12:00 AoE",
            "January 29, 2027, 11:59 pm AoE",
            "11:59 PM AoE, Friday, January 29, 2027",
            "Jan 29 23:59 AoE",
        ],
    )
    def test_spellings(self, text: str) -> None:
        moment = parse_moment(text, reference=REFERENCE_NOW)
        assert moment == datetime(2027, 1, 29, 23, 59, tzinfo=AOE)
        assert isinstance(moment, datetime)
        assert moment.utcoffset() == timedelta(hours=-12)

    def test_a_deadline_at_midnight_is_on_the_next_day_in_pacific_time(self) -> None:
        assert iso(resolve("Jan 29, 2027 23:59 AoE").start) == "2027-01-30T03:59:00-08:00"
        assert iso(resolve("Jan 29, 2027 0:00 AoE").start) == "2027-01-29T04:00:00-08:00"
        assert iso(resolve("Jan 29, 2027 19:59 AoE").start) == "2027-01-29T23:59:00-08:00"
        assert iso(resolve("Jan 29, 2027 20:00 AoE").start) == "2027-01-30T00:00:00-08:00"

    def test_the_zone_of_the_source_does_not_matter(self) -> None:
        for zone in (LA, "UTC", "Europe/Berlin", "Asia/Tokyo"):
            times = resolve("Jan 29, 2027 23:59 AoE", source_zone=zone)
            assert iso(times.start) == "2027-01-30T03:59:00-08:00"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # The clocks go forward at 10:00 UTC on 2027-03-14 in America/Los_Angeles.
            ("Mar 13, 2027 21:59 AoE", "2027-03-14T01:59:00-08:00"),
            ("Mar 13, 2027 22:00 AoE", "2027-03-14T03:00:00-07:00"),
            ("Mar 13, 2027 23:59 AoE", "2027-03-14T04:59:00-07:00"),
            # They go back at 09:00 UTC on 2027-11-07: 01:00 comes twice.
            ("Nov 6, 2027 20:00 AoE", "2027-11-07T01:00:00-07:00"),
            ("Nov 6, 2027 21:00 AoE", "2027-11-07T01:00:00-08:00"),
            ("Nov 6, 2027 23:59 AoE", "2027-11-07T03:59:00-08:00"),
        ],
    )
    def test_across_the_dst_changes_of_the_source(self, text: str, expected: str) -> None:
        assert iso(resolve(text).start) == expected

    def test_an_end_in_aoe_keeps_the_length(self) -> None:
        times = resolve("Mar 13, 2027 21:00 AoE", "Mar 13, 2027 23:00 AoE")
        assert iso(times.start) == "2027-03-14T01:00:00-08:00"
        assert iso(times.end) == "2027-03-14T04:00:00-07:00"
        assert times.end is not None
        assert times.end - times.start == timedelta(hours=2)

    def test_a_date_in_aoe_is_that_day(self) -> None:
        assert parse_moment("Jan 29, 2027 AoE", reference=REFERENCE_NOW) == date(2027, 1, 29)
        assert parse_moment("January 29 (AoE)", reference=REFERENCE_NOW) == date(2027, 1, 29)
        times = resolve("Jan 29, 2027 (Anywhere on Earth)")
        assert times.all_day is True
        assert iso(times.start) == "2027-01-29T00:00:00-08:00"

    @pytest.mark.parametrize("text", ["AoE", "(Anywhere on Earth)", "23:59 AoE"])
    def test_a_zone_without_a_day_is_an_error(self, text: str) -> None:
        with pytest.raises(TimeError):
            parse_moment(text, reference=REFERENCE_NOW)

    @pytest.mark.parametrize(
        "text", ["Jan 29, 2027 23:59 AoEx", "Jan 29, 2027 23:59 xAoE", "Jan 29, 2027 23:59 AoE2"]
    )
    def test_aoe_is_a_word_of_its_own(self, text: str) -> None:
        with pytest.raises(TimeError):
            parse_moment(text, reference=REFERENCE_NOW)


class TestOffsetsFromUtc:
    """ "UTC-8" is eight hours behind UTC, not the POSIX "UTC-8" of dateutil, eight ahead."""

    @pytest.mark.parametrize(
        ("zone", "hours", "minutes"),
        [
            ("UTC-8", -8, 0),
            ("UTC+8", 8, 0),
            ("GMT-8", -8, 0),
            ("GMT+1", 1, 0),
            ("UTC-12", -12, 0),
            ("GMT-12", -12, 0),
            ("UTC+14", 14, 0),
            ("UTC-08", -8, 0),
            ("UTC-08:00", -8, 0),
            ("GMT-0800", -8, 0),
            ("UTC+5:30", 5, 30),
            ("UTC+05:30", 5, 30),
            ("UTC+0530", 5, 30),
            ("GMT+0545", 5, 45),
            ("UTC-9:30", -9, -30),
            ("UTC+12:45", 12, 45),
            ("UTC+0", 0, 0),
            ("GMT-0", 0, 0),
            ("UTC+00:00", 0, 0),
            ("UTC -8", -8, 0),
            ("GMT +01:00", 1, 0),
            ("UTC  +0530", 5, 30),
            ("UTC\u22128", -8, 0),
            ("utc-8", -8, 0),
            ("Gmt+1", 1, 0),
            ("(UTC-8)", -8, 0),
            ("( GMT+5:30 )", 5, 30),
            ("UTC-8.", -8, 0),
        ],
    )
    def test_the_offset_as_people_write_it(self, zone: str, hours: int, minutes: int) -> None:
        moment = parse_moment(f"Jan 29, 2027 23:59 {zone}", reference=REFERENCE_NOW)
        assert isinstance(moment, datetime)
        assert (moment.hour, moment.minute) == (23, 59)
        assert moment.utcoffset() == timedelta(hours=hours, minutes=minutes)

    def test_through_the_rules_of_normalization(self) -> None:
        assert iso(resolve("Jan 29, 2027 23:59 UTC-12").start) == "2027-01-30T03:59:00-08:00"
        assert iso(resolve("Jan 29, 2027 9:00 GMT+1").start) == "2027-01-29T00:00:00-08:00"
        assert iso(resolve("Jul 1, 2027 9:00 UTC+8").start) == "2027-06-30T18:00:00-07:00"
        assert iso(resolve("Jul 1, 2027 23:30 UTC+5:30").start) == "2027-07-01T11:00:00-07:00"

    def test_an_offset_before_the_date(self) -> None:
        moment = parse_moment("11:59 PM UTC-12, January 29, 2027", reference=REFERENCE_NOW)
        assert moment == datetime(2027, 1, 29, 23, 59, tzinfo=AOE)

    def test_the_time_that_javascript_writes(self) -> None:
        # Date.prototype.toString, without the name of the zone in parentheses
        moment = parse_moment("Fri Jan 29 2027 16:00:00 GMT-0800", reference=REFERENCE_NOW)
        assert moment == datetime(2027, 1, 29, 16, 0, tzinfo=PST)
        assert isinstance(moment, datetime)
        assert moment.utcoffset() == timedelta(hours=-8)

    def test_a_date_with_an_offset_is_that_day(self) -> None:
        assert parse_moment("Jan 29, 2027 UTC+8", reference=REFERENCE_NOW) == date(2027, 1, 29)

    @pytest.mark.parametrize(
        "zone",
        [
            "UTC-13",  # no zone is that far behind
            "UTC+15",
            "UTC-24",
            "GMT+2400",
            "UTC+5:60",
            "UTC+0575",
        ],
    )
    def test_an_offset_that_no_time_zone_has_is_an_error(self, zone: str) -> None:
        with pytest.raises(TimeError, match="no time zone has"):
            parse_moment(f"Jan 29, 2027 23:59 {zone}", reference=REFERENCE_NOW)

    @pytest.mark.parametrize(
        "zone",
        [
            "UTC - 8",  # a sign between spaces: a range as often as an offset
            "UTC + 8",
            "UTC- 8",
            "UTC+530",  # three digits
            "UTC+5:3",
            "UTC-12h",
            "UTC+8.5",
            "UTC-8 pm",
        ],
    )
    def test_an_offset_that_cannot_be_read_is_an_error_not_a_guess(self, zone: str) -> None:
        with pytest.raises(TimeError, match="cannot be read"):
            parse_moment(f"Jan 29, 2027 23:59 {zone}", reference=REFERENCE_NOW)

    @pytest.mark.parametrize(
        "text",
        [
            "Jan 29, 2027 10:00 GMT - 11:00",
            "Jan 29, 2027 10:00 GMT - 11:00 GMT",
            "Jan 29, 2027 4 PM GMT-5 PM",
        ],
    )
    def test_a_range_is_not_read_as_an_offset(self, text: str) -> None:
        with pytest.raises(TimeError):
            parse_moment(text, reference=REFERENCE_NOW)

    def test_an_iana_name_is_not_an_offset(self) -> None:
        # Etc/GMT+12 is UTC-12 (the POSIX sign); it is no "GMT+12" of a page.
        with pytest.raises(TimeError, match="cannot be parsed"):
            parse_moment("Jan 29, 2027 23:59 Etc/GMT+12", reference=REFERENCE_NOW)

    def test_numeric_offsets_and_utc_keep_what_dateutil_gives(self) -> None:
        for text, offset in [
            ("Jan 29, 2027 23:59 -12:00", timedelta(hours=-12)),
            ("Jan 29, 2027 23:59 -0800", timedelta(hours=-8)),
            ("Jan 29, 2027 23:59 +0100", timedelta(hours=1)),
            ("Jan 29, 2027 23:59 UTC", timedelta(0)),
            ("Jan 29, 2027 23:59 GMT", timedelta(0)),
            ("Jan 29, 2027 23:59 Z", timedelta(0)),
        ]:
            moment = parse_moment(text, reference=REFERENCE_NOW)
            assert isinstance(moment, datetime)
            assert moment.utcoffset() == offset


class TestTwoZones:
    @pytest.mark.parametrize(
        ("text", "offset"),
        [
            ("Jan 29, 2027 4:00 PM PST (UTC-8)", -8),
            ("Jul 1, 2027 4:00 PM PDT (UTC-7)", -7),
            ("Jan 29, 2027 16:00 UTC (GMT+0)", 0),
            ("Jan 29, 2027 23:59 AoE (UTC-12)", -12),
            ("Jan 29, 2027 23:59 UTC-8 (PST)", -8),
            ("Jan 29, 2027 23:59 UTC-8 ( PT )", -8),
        ],
    )
    def test_two_zones_that_agree(self, text: str, offset: int) -> None:
        moment = parse_moment(text, reference=REFERENCE_NOW)
        assert isinstance(moment, datetime)
        assert moment.utcoffset() == timedelta(hours=offset)

    def test_an_unknown_name_next_to_an_offset_is_ignored(self) -> None:
        moment = parse_moment("Jan 29, 2027 23:59 UTC+5:30 (IST)", reference=REFERENCE_NOW)
        assert moment == datetime(2027, 1, 29, 23, 59, tzinfo=timezone(timedelta(hours=5.5)))

    def test_a_name_in_parentheses_without_an_offset_is_left_to_dateutil(self) -> None:
        with pytest.raises(TimeError, match="cannot be parsed"):
            parse_moment("Jan 29, 2027 23:59 (PST)", reference=REFERENCE_NOW)

    @pytest.mark.parametrize(
        "text",
        [
            "Jan 29, 2027 23:59 PST AoE",
            "Jan 29, 2027 23:59 AoE (UTC+8)",
            "Jan 29, 2027 23:59 UTC-8 (UTC-7)",
            "Jul 1, 2027 4:00 PM PST (UTC-8)",  # PST in July is the time of Los Angeles, UTC-7
            "Jul 1, 2027 4:00 PM UTC-8 (PST)",
            "Jan 29, 2027 23:59 AoE (UTC)",
            "Jan 29, 2027 23:59 -0800 (UTC-12)",
        ],
    )
    def test_two_zones_that_differ_are_an_error(self, text: str) -> None:
        with pytest.raises(TimeError, match="different"):
            parse_moment(text, reference=REFERENCE_NOW)
