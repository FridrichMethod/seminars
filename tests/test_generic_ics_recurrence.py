"""The ics adapter: recurring events, their overrides and the caps of the expansion."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import icalendar
import pytest

from factories import make_document
from seminars.models import TimeWindow
from seminars.sources.base import ParseError
from seminars.sources.generic import ics, ics_rules
from seminars.sources.generic.ics import IcsCalendar, instance_key, occurrences, read_calendar
from seminars.sources.generic.ics_rules import MAX_OCCURRENCES, check_rule
from test_generic_ics import FEED, LA, PACIFIC, WINDOW, calendar, expand, parse, vevent

# --- recurring events ----------------------------------------------------------------------

WEEK = [datetime(2026, 10, 20, 23, 0, tzinfo=UTC) + timedelta(weeks=n) for n in range(4)]


class TestRecurrence:
    def test_a_weekly_rule_keeps_the_local_time_across_the_change_of_offset(self) -> None:
        drafts = expand(
            vevent(
                "DTEND;TZID=America/Los_Angeles:20260901T170000",
                "RRULE:FREQ=WEEKLY;BYDAY=TU",
                "SUMMARY:Weekly seminar",
                start="DTSTART;TZID=America/Los_Angeles:20260901T160000",
            )
        )
        starts = [draft.start for draft in drafts]
        assert starts == [datetime(2026, 10, day, 16, 0, tzinfo=PACIFIC) for day in (20, 27)] + [
            datetime(2026, 11, day, 16, 0, tzinfo=PACIFIC) for day in (3, 10)
        ]
        assert [draft.native_id for draft in drafts] == [
            "talk-1/20261020T230000Z",
            "talk-1/20261027T230000Z",
            "talk-1/20261104T000000Z",
            "talk-1/20261111T000000Z",
        ]
        assert all(draft.end == draft.start + timedelta(hours=1) for draft in drafts)  # type: ignore[operator]

    def test_exdate_rdate_and_a_moved_occurrence(self) -> None:
        drafts = expand(
            vevent(
                "RRULE:FREQ=WEEKLY;COUNT=10",
                "EXDATE:20261027T230000Z",
                "RDATE:20261029T230000Z",
                "SUMMARY:Weekly",
                start="DTSTART:20261013T230000Z",
            ),
            vevent(
                "RECURRENCE-ID:20261103T230000Z",
                "DTSTART:20261105T230000Z",
                "SUMMARY:Moved to Thursday",
                start=None,
            ),
            vevent(
                "RECURRENCE-ID:20261110T230000Z",
                "DTSTART:20261110T230000Z",
                "STATUS:CANCELLED",
                "SUMMARY:Weekly",
                start=None,
            ),
        )
        found = [(draft.native_id, draft.start, draft.title, draft.status_hint) for draft in drafts]
        assert found == [
            ("talk-1/20261020T230000Z", datetime(2026, 10, 20, 23, 0, tzinfo=UTC), "Weekly", None),
            ("talk-1/20261029T230000Z", datetime(2026, 10, 29, 23, 0, tzinfo=UTC), "Weekly", None),
            (
                "talk-1/20261103T230000Z",
                datetime(2026, 11, 5, 23, 0, tzinfo=UTC),
                "Moved to Thursday",
                None,
            ),
            (
                "talk-1/20261110T230000Z",
                datetime(2026, 11, 10, 23, 0, tzinfo=UTC),
                "Weekly",
                "cancelled",
            ),
        ]

    def test_an_override_without_its_occurrence_stands_alone_inside_the_window(self) -> None:
        drafts = expand(
            vevent(
                "RECURRENCE-ID:20260101T230000Z",
                "DTSTART:20261104T230000Z",
                "SUMMARY:moved in",
                start=None,
                uid="gone",
            ),
            vevent(
                "RECURRENCE-ID:20260108T230000Z",
                "DTSTART:20270104T230000Z",
                "SUMMARY:outside",
                start=None,
                uid="gone",
            ),
            vevent("RECURRENCE-ID:20260115T230000Z", "SUMMARY:no start", start=None, uid="gone"),
            vevent("RECURRENCE-ID:20261111T230000Z", "SUMMARY:kept", start=None, uid="gone"),
        )
        assert [(draft.native_id, draft.title, draft.start) for draft in drafts] == [
            ("gone/20260101T230000Z", "moved in", datetime(2026, 11, 4, 23, 0, tzinfo=UTC)),
            ("gone/20261111T230000Z", "kept", datetime(2026, 11, 11, 23, 0, tzinfo=UTC)),
        ]

    def test_an_override_of_an_event_without_recurrence_replaces_it(self) -> None:
        drafts = expand(
            vevent("SUMMARY:Talk", uid="a", start=f"DTSTART;{LA}:20261103T090000"),
            vevent(
                "RECURRENCE-ID:20261103T170000Z",
                "DTSTART:20261104T170000Z",
                "SUMMARY:Moved talk",
                uid="a",
                start=None,
            ),
            vevent("SUMMARY:Other", uid="b", start="DTSTART:20261105T170000Z"),
            vevent("RECURRENCE-ID:20261101T170000Z", "SUMMARY:Elsewhere", uid="b", start=None),
        )
        assert [(draft.native_id, draft.start, draft.title) for draft in drafts] == [
            ("a", datetime(2026, 11, 4, 17, 0, tzinfo=UTC), "Moved talk"),
            ("b", datetime(2026, 11, 5, 17, 0, tzinfo=UTC), "Other"),
            ("b/20261101T170000Z", datetime(2026, 11, 1, 17, 0, tzinfo=UTC), "Elsewhere"),
        ]

    def test_an_override_without_usable_times_spares_its_series(self) -> None:
        drafts = expand(
            vevent(
                "DTEND:20261021T000000Z",
                "RRULE:FREQ=WEEKLY;COUNT=4",
                "SUMMARY:Weekly",
                start="DTSTART:20261020T230000Z",
            ),
            vevent("RECURRENCE-ID:20261027T230000Z", "STATUS:CANCELLED", start=None),
            vevent("RECURRENCE-ID:20261103T230000Z", "SUMMARY:broken", start="DTSTART:garbage"),
            vevent("RECURRENCE-ID:20261110T230000Z", "DURATION:PT2H", "SUMMARY:Longer", start=None),
        )
        found = [(d.native_id, d.start, d.end, d.title, d.status_hint) for d in drafts]
        hour = timedelta(hours=1)
        assert found == [
            ("talk-1/20261020T230000Z", WEEK[0], WEEK[0] + hour, "Weekly", None),
            ("talk-1/20261027T230000Z", WEEK[1], WEEK[1] + hour, None, "cancelled"),
            ("talk-1/20261103T230000Z", WEEK[2], WEEK[2] + hour, "Weekly", None),
            ("talk-1/20261110T230000Z", WEEK[3], WEEK[3] + 2 * hour, "Longer", None),
        ]

    def test_until_in_utc_with_a_floating_start_and_until_as_a_date(self) -> None:
        floating = expand(
            vevent("RRULE:FREQ=WEEKLY;UNTIL=20261028T000000Z", start="DTSTART:20261013T160000")
        )
        # UNTIL is 2026-10-27 17:00 PDT, so the occurrence at 16:00 that day is the last one
        assert [draft.start for draft in floating] == [
            datetime(2026, 10, 20, 16, 0),
            datetime(2026, 10, 27, 16, 0),
        ]
        by_date = expand(
            vevent(
                "RRULE:FREQ=WEEKLY;UNTIL=20261027",
                start="DTSTART;TZID=America/Los_Angeles:20261013T160000",
            )
        )
        assert [draft.start for draft in by_date] == [
            datetime(2026, 10, 20, 16, 0, tzinfo=PACIFIC),
            datetime(2026, 10, 27, 16, 0, tzinfo=PACIFIC),
        ]
        naive_until = expand(
            vevent("RRULE:FREQ=WEEKLY;UNTIL=20261021T000000", start="DTSTART:20261013T230000Z")
        )
        assert [draft.start for draft in naive_until] == [datetime(2026, 10, 20, 23, 0, tzinfo=UTC)]

    def test_floating_recurrences_use_the_zone_of_the_source(self) -> None:
        drafts = expand(vevent("RRULE:FREQ=DAILY;COUNT=40", start="DTSTART:20261016T230000"))
        # 23:00 on 19 October is before midnight PDT of the 20th, the first day of the window
        assert drafts[0].start == datetime(2026, 10, 20, 23, 0)
        assert drafts[-1].start == datetime(2026, 11, 16, 23, 0)
        assert [draft.native_id for draft in drafts][:1] == ["talk-1/20261020T230000"]

    def test_all_day_recurrences_are_dates(self) -> None:
        drafts = parse(
            calendar(
                vevent(
                    "DTEND;VALUE=DATE:20261003",
                    "RRULE:FREQ=MONTHLY;BYMONTHDAY=1",
                    "EXDATE;VALUE=DATE:20261101",
                    start="DTSTART;VALUE=DATE:20261001",
                )
            ),
            window=TimeWindow(
                start=datetime(2026, 9, 1, 7, tzinfo=UTC), end=datetime(2027, 1, 2, 8, tzinfo=UTC)
            ),
        )
        assert [(draft.start, draft.end, draft.native_id) for draft in drafts] == [
            (date(2026, 10, 1), date(2026, 10, 2), "talk-1/20261001"),
            (date(2026, 12, 1), date(2026, 12, 2), "talk-1/20261201"),
            (date(2027, 1, 1), date(2027, 1, 2), "talk-1/20270101"),
        ]

    def test_rdate_alone_and_periods_are_read(self) -> None:
        drafts = expand(
            vevent(
                "RDATE:20261021T230000Z,20261028T230000Z",
                "RDATE;VALUE=PERIOD:20261030T230000Z/PT1H",
                start="DTSTART:20261014T230000Z",
            )
        )
        assert [draft.start for draft in drafts] == [
            datetime(2026, 10, 21, 23, 0, tzinfo=UTC),
            datetime(2026, 10, 28, 23, 0, tzinfo=UTC),
        ]

    def test_expansion_is_capped(self) -> None:
        two_years = TimeWindow(start=WINDOW.start, end=WINDOW.start + timedelta(days=730))
        daily = vevent("RRULE:FREQ=DAILY", start="DTSTART:20261020T230000Z")
        assert len(parse(calendar(daily), window=two_years)) == MAX_OCCURRENCES
        # Without COUNT the walk starts at the window, however old the series is.
        assert len(expand(vevent("RRULE:FREQ=DAILY", start="DTSTART:19000101T230000Z"))) == 28
        # With COUNT it starts at DTSTART, and from 1900 the window is more than
        # MAX_EXPANSION_STEPS steps away: the walk of that rule stops on the way.
        counted = vevent("RRULE:FREQ=DAILY;COUNT=100000", start="DTSTART:19000101T230000Z")
        assert expand(counted) == []

    def test_a_calendar_that_keeps_its_past_series(self) -> None:
        # Exports keep every series: 55 quarters of weekly talks, ended by COUNT or UNTIL.
        past = [
            vevent(
                f"RRULE:FREQ=WEEKLY;{'COUNT=10' if n % 2 else f'UNTIL={2010 + n // 4}1231'}",
                uid=f"q{n}",
                start=f"DTSTART:{2010 + n // 4}{1 + 3 * (n % 4):02d}05T230000Z",
            )
            for n in range(55)
        ]
        current = vevent("RRULE:FREQ=WEEKLY;COUNT=10", uid="now", start="DTSTART:20260922T230000Z")
        drafts = expand(*past, current)
        assert [draft.native_id for draft in drafts] == [
            f"now/2026{day}T230000Z" for day in ("1020", "1027", "1103", "1110")
        ]

    def test_series_that_ended_before_the_window_are_not_expanded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ics, "MAX_RECURRING_EVENTS", 2)
        monkeypatch.setattr(ics, "MAX_FEED_STEPS", 100)
        old = "DTSTART:19990101"
        ended = [
            vevent(f"RRULE:FREQ=DAILY;BYMONTH=1;UNTIL={2000 + n}1231", uid=f"o{n}", start=old)
            for n in range(5)
        ]
        again = vevent(
            "RRULE:FREQ=WEEKLY;UNTIL=20200101T000000Z",
            "RDATE:20261021T230000Z",
            uid="again",
            start="DTSTART:20191201T230000Z",
        )
        weekly = vevent("RRULE:FREQ=WEEKLY;COUNT=3", uid="now", start="DTSTART:20261020T230000Z")
        monthly = vevent(
            "RRULE:FREQ=MONTHLY;BYMONTHDAY=20;COUNT=2", uid="day", start="DTSTART:20261020T230000Z"
        )
        drafts = expand(*ended, again, weekly, monthly)
        assert [draft.native_id for draft in drafts] == [
            "again/20261021T230000Z",
            "now/20261020T230000Z",
            "now/20261027T230000Z",
            "now/20261103T230000Z",
            "day/20261020T230000Z",
        ]
        second = monthly.replace("UID:day", "UID:other")
        with pytest.raises(ParseError, match=r"example-talks: .*more than 2 recurrence rules"):
            expand(*ended, again, weekly, monthly, second)

    @pytest.mark.parametrize(
        "rule",
        [
            "FREQ=MONTHLY;BYMONTHDAY=15",
            "FREQ=YEARLY;BYMONTH=6",
            "FREQ=MONTHLY;BYDAY=MO;BYSETPOS=-1",
            "FREQ=MONTHLY;BYMONTHDAY=13,31;BYDAY=TU,MO;BYSETPOS=2",
        ],
    )
    def test_many_rules_that_filter_are_fine(self, rule: str) -> None:
        # The steps bound what a rule costs, whatever its filters; no cap counts filters.
        events = [
            vevent(f"RRULE:{rule}", uid=f"u{n}", start="DTSTART:20261020T230000Z")
            for n in range(150)
        ]
        assert len({draft.native_id for draft in expand(*events)}) >= 150

    def test_a_feed_whose_recurrences_need_too_many_steps_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A daily rule takes two steps per day: the day, and the occurrence it hands out.
        monkeypatch.setattr(ics, "MAX_FEED_STEPS", 24)
        expired = vevent("RRULE:FREQ=DAILY;COUNT=5", uid="old", start="DTSTART:20200107T230000Z")
        current = vevent("RRULE:FREQ=DAILY;COUNT=7", uid="now", start="DTSTART:20261020T230000Z")
        assert len(expand(expired, current)) == 7
        more = vevent("RRULE:FREQ=DAILY;COUNT=2", uid="more", start="DTSTART:20261101T230000Z")
        with pytest.raises(ParseError, match=r"more than 24 steps: https://calendar\.example"):
            expand(expired, current, more)

    def test_long_series_with_count_fit_the_steps_of_a_feed(self) -> None:
        # A rule with COUNT is walked from DTSTART, about two steps per period: a period reads
        # only the days its rule can hit, one a year here, one a week there.
        yearly = [
            vevent("RRULE:FREQ=YEARLY;COUNT=200", uid=f"y{n}", start="DTSTART:19501101T170000Z")
            for n in range(30)
        ]
        weekly = [
            vevent("RRULE:FREQ=WEEKLY;COUNT=2000", uid=f"w{n}", start="DTSTART:20000104T170000Z")
            for n in range(60)
        ]
        drafts = expand(*yearly, *weekly)
        assert len(drafts) == 30 + 60 * 4
        yearly_starts = {draft.start for draft in drafts if (draft.native_id or "")[:1] == "y"}
        assert yearly_starts == {datetime(2026, 11, 1, 17, tzinfo=UTC)}

    def test_a_feed_with_too_many_recurring_events_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Every walked rule counts, also one that ends by COUNT or starts after the window,
        # because only its walk tells; one ended by UNTIL before the window is not walked.
        monkeypatch.setattr(ics, "MAX_RECURRING_EVENTS", 2)
        ended = vevent("RRULE:FREQ=WEEKLY;UNTIL=20200101", uid="u", start="DTSTART:20191001")
        counted = vevent("RRULE:FREQ=WEEKLY;COUNT=5", uid="c", start="DTSTART:20200107T230000Z")
        later = vevent("RRULE:FREQ=WEEKLY", uid="later", start="DTSTART:20270105T230000Z")
        assert expand(ended, counted, later) == []
        current = vevent("RRULE:FREQ=WEEKLY;COUNT=2", uid="now", start="DTSTART:20261020T230000Z")
        with pytest.raises(ParseError, match=r"more than 2 recurrence rules .* that do not end"):
            expand(ended, counted, later, current)

    @pytest.mark.parametrize(
        "rule", ["FREQ=MONTHLY;BYMONTHDAY=13,31;BYDAY=TU,MO;BYSETPOS=2", "FREQ=WEEKLY;BYDAY=TU"]
    )
    def test_the_caps_count_rrule_lines_not_events(
        self, monkeypatch: pytest.MonkeyPatch, rule: str
    ) -> None:
        # One VEVENT may carry many RRULE properties, each of them walked on its own.
        monkeypatch.setattr(ics, "MAX_RECURRING_EVENTS", 3)
        lines = [f"RRULE:{rule}"] * 3
        assert [draft.start for draft in expand(vevent(*lines, start="DTSTART:20261020T230000Z"))]
        one_more = vevent(*lines, f"RRULE:{rule}", start="DTSTART:20261020T230000Z")
        with pytest.raises(ParseError, match=r"more than 3 recurrence rules .*do not end"):
            expand(one_more)
        split = [
            vevent(f"RRULE:{rule}", uid=f"u{n}", start="DTSTART:20261020T230000Z") for n in range(4)
        ]
        with pytest.raises(ParseError, match=r"more than 3 recurrence rules .*do not end"):
            expand(*split)

    def test_the_steps_of_every_rrule_line_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Lines of a rule that never matches: each costs the periods of the window.
        rule = "FREQ=MONTHLY;BYMONTHDAY=13,31;BYDAY=TU,MO;BYSETPOS=2"
        one = ics_rules.expand(
            ics_rules.RecurrenceProperties(rules=(icalendar.vRecur.from_ical(rule),)),
            datetime(2026, 10, 20, 23, tzinfo=UTC),
            WINDOW,
            "UTC",
            allowance=ics_rules.Allowance(steps=10**6, rules=10),
        )
        monkeypatch.setattr(ics, "MAX_FEED_STEPS", 3 * one.steps)
        lines = [f"RRULE:{rule}"] * 3
        assert expand(vevent(*lines, start="DTSTART:20261020T230000Z"))
        with pytest.raises(ParseError, match=rf"more than {3 * one.steps} steps"):
            expand(vevent(*lines, f"RRULE:{rule}", start="DTSTART:20261020T230000Z"))

    @pytest.mark.parametrize(
        "rule",
        [
            "FREQ=HOURLY",
            "FREQ=SECONDLY;BYMONTH=2;BYMONTHDAY=30",
            "BYDAY=MO",
            "FREQ=YEARLY;BYYEARDAY=100",
            "FREQ=YEARLY;BYWEEKNO=20",
            "FREQ=WEEKLY;BYDAY=MO;BYSETPOS=2",
            "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30",
            "FREQ=YEARLY;BYMONTH=4,6;BYMONTHDAY=31,-31",
            "FREQ=MONTHLY;BYDAY=MO;BYSETPOS=6",
            "FREQ=MONTHLY;BYMONTHDAY=1,15;BYSETPOS=3",
            "FREQ=YEARLY;BYMONTH=3;BYDAY=1MO,-1MO;BYSETPOS=-3",
            "FREQ=MONTHLY;BYDAY=6MO",
            "FREQ=YEARLY;BYDAY=54MO",
            "FREQ=YEARLY;BYMONTH=2;BYDAY=-6FR",
            "FREQ=WEEKLY;BYDAY=2MO",
            "FREQ=YEARLY;BYMONTH=13",
            "FREQ=WEEKLY;BYMONTHDAY=13",
            "FREQ=MONTHLY;BYMONTHDAY=32",
            "FREQ=MONTHLY;BYMONTHDAY=0",
            "FREQ=MONTHLY;BYDAY=-1FR;BYMONTHDAY=13",
            "FREQ=YEARLY;BYDAY=5MO;BYMONTHDAY=13",
            # RFC 5545 takes the union of plain and ordinal weekdays, the library the
            # intersection: no day at all when the weekdays differ
            "FREQ=MONTHLY;BYDAY=TH,1FR",
            "FREQ=YEARLY;BYDAY=TU,4WE",
            "FREQ=YEARLY;BYMONTH=3;BYDAY=-1MO,FR",
            "FREQ=MONTHLY;BYDAY=TH,1TH;COUNT=5",
            # a monthly period is one month, whatever BYMONTH names
            "FREQ=MONTHLY;BYMONTH=6,7,8;BYDAY=4TU;BYSETPOS=2",
            "FREQ=MONTHLY;BYMONTH=1,4,12;BYSETPOS=3",
            # without BYDAY and BYMONTHDAY a year has the day of DTSTART once
            "FREQ=YEARLY;BYSETPOS=3",
            "FREQ=YEARLY;BYDAY=20TH;BYSETPOS=5",
            # 30 and -1 are the same day of November
            "FREQ=MONTHLY;BYMONTH=11;BYMONTHDAY=30,-1;BYSETPOS=2",
        ],
    )
    def test_rules_the_expansion_does_not_support_are_skipped(self, rule: str) -> None:
        assert expand(vevent(f"RRULE:{rule}", start="DTSTART:20261020T230000Z")) == []
        with pytest.raises(ValueError, match=r"unsupported|BYSETPOS|BYMONTH|BYDAY"):
            check_rule(dict(icalendar.vRecur.from_ical(rule)))

    @pytest.mark.parametrize(
        ("rule", "refused"),
        [
            ("FREQ=DAILY;INTERVAL=7;BYDAY=TU", True),
            ("FREQ=DAILY;INTERVAL=14;BYDAY=WE,TH", True),
            ("FREQ=DAILY;INTERVAL=2;BYDAY=TU;BYMONTHDAY=13", False),  # walked, may match
            ("FREQ=MONTHLY;INTERVAL=2;BYMONTH=2", True),
            ("FREQ=MONTHLY;INTERVAL=12;BYMONTH=6,12", True),
            ("FREQ=DAILY;INTERVAL=0", True),
            ("FREQ=DAILY;INTERVAL=7;BYDAY=MO,TU", False),
            ("FREQ=DAILY;INTERVAL=3;BYDAY=TU", False),
            ("FREQ=DAILY;INTERVAL=2;BYMONTHDAY=13", False),
            ("FREQ=DAILY;BYDAY=TU;BYMONTHDAY=13", False),
            ("FREQ=MONTHLY;INTERVAL=2;BYMONTH=1,2", False),
            ("FREQ=MONTHLY;INTERVAL=3;BYMONTH=2", False),
        ],
    )
    def test_an_interval_that_never_meets_the_filters_is_refused(
        self, rule: str, refused: bool
    ) -> None:
        # DTSTART is Monday, 16 November 2026: the daily rules step on Mondays only when
        # INTERVAL is a multiple of 7, and every second month from November skips February.
        drafts = expand(vevent(f"RRULE:{rule}", start="DTSTART:20261116T090000"))
        assert (drafts == []) == refused
        parts: dict[str, list[Any]] = dict(icalendar.vRecur.from_ical(rule))
        check_rule(parts)
        if refused:
            with pytest.raises(ValueError, match=r"INTERVAL|BYDAY|BYMONTH"):
                check_rule(parts, start=date(2026, 11, 16))
        else:
            check_rule(parts, start=date(2026, 11, 16))

    @pytest.mark.parametrize(
        "rule",
        [
            "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29",
            "FREQ=MONTHLY;BYDAY=TU;BYSETPOS=-1",
            "FREQ=MONTHLY;BYMONTH=4,5;BYMONTHDAY=31",
            "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;WKST=SU",
            "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1",
            "FREQ=YEARLY;BYDAY=MO;BYSETPOS=50",
            "FREQ=MONTHLY;BYDAY=MO;BYHOUR=9,14;BYSETPOS=10",
            "FREQ=MONTHLY;BYDAY=-1FR",
            "FREQ=YEARLY;BYDAY=20TH",
            "FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=28,-1;BYSETPOS=2",
            "FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=2,-28;BYSETPOS=2",
        ],
    )
    def test_rules_of_talk_calendars_are_accepted(self, rule: str) -> None:
        check_rule(dict(icalendar.vRecur.from_ical(rule)))

    @pytest.mark.parametrize(
        ("rule", "refused"),
        [
            ("FREQ=MONTHLY;BYMONTH=6", True),
            ("FREQ=YEARLY;BYMONTH=2,9", True),
            ("FREQ=MONTHLY;INTERVAL=3;BYMONTH=4,9", True),
            ("FREQ=MONTHLY;INTERVAL=2;BYMONTH=6,7;BYMONTHDAY=31", True),
            ("FREQ=YEARLY;BYMONTH=3,4,8;BYSETPOS=-3", True),
            ("FREQ=MONTHLY", False),
            ("FREQ=MONTHLY;INTERVAL=2", False),
            ("FREQ=MONTHLY;BYMONTH=6,7", False),
            ("FREQ=YEARLY;BYMONTH=2,12", False),
            ("FREQ=MONTHLY;BYMONTH=6;BYDAY=MO", False),
            ("FREQ=YEARLY;BYMONTH=3,4,8;BYSETPOS=-2", False),
        ],
    )
    def test_the_day_of_dtstart_must_be_in_a_month_the_rule_reaches(
        self, rule: str, refused: bool
    ) -> None:
        # DTSTART is 31 October: without BYDAY and BYMONTHDAY the rule repeats the 31st, which
        # neither June nor April has; every third month from October skips September.
        drafts = expand(vevent(f"RRULE:{rule}", start="DTSTART:20261031T090000"))
        assert (drafts == []) == refused
        parts: dict[str, list[Any]] = dict(icalendar.vRecur.from_ical(rule))
        check_rule(parts)
        if refused:
            with pytest.raises(ValueError, match=r"BYMONTH|BYSETPOS"):
                check_rule(parts, start=date(2026, 10, 31))
        else:
            check_rule(parts, start=date(2026, 10, 31))

    def test_a_recurring_event_without_uid_has_no_native_id(self) -> None:
        drafts = expand(
            vevent("RRULE:FREQ=WEEKLY;COUNT=2", uid=None, start="DTSTART:20261020T230000Z")
        )
        assert [draft.native_id for draft in drafts] == [None, None]

    def test_instance_keys(self) -> None:
        assert instance_key(date(2026, 11, 17)) == "20261117"
        assert instance_key(datetime(2026, 11, 17, 16, 0)) == "20261117T160000"
        assert instance_key(datetime(2026, 11, 17, 16, 0, tzinfo=PACIFIC)) == "20261118T000000Z"

    def test_occurrences_of_a_calendar(self) -> None:
        feed = read_calendar(
            make_document(
                url=FEED,
                text=calendar(
                    vevent("RRULE:FREQ=WEEKLY;COUNT=3", start="DTSTART:20261020T230000Z")
                ),
            )
        )
        found = occurrences(feed, WINDOW, timezone="UTC")
        assert [item.native_id for item in found] == [
            "talk-1/20261020T230000Z",
            "talk-1/20261027T230000Z",
            "talk-1/20261103T230000Z",
        ]
        assert found[0].end is None
        assert isinstance(feed, IcsCalendar)
