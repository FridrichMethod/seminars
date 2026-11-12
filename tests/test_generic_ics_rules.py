"""The walk of recurrence rules (ics_rules): what it gives, and what it costs."""

from __future__ import annotations

import datetime as datetime_module
import inspect
import random
import time
import types
from collections import Counter
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

import icalendar
import pytest
from dateutil import rrule as dateutil_rrule
from dateutil.rrule import rrule, rruleset, rrulestr

from seminars.models import TimeWindow
from seminars.sources.generic import ics_rules
from seminars.sources.generic.ics_rules import (
    MAX_OCCURRENCES,
    Allowance,
    Expansion,
    RecurrenceProperties,
    expand,
)

PACIFIC = ZoneInfo("America/Los_Angeles")
WINDOW = TimeWindow(
    start=datetime(2026, 10, 20, 7, 0, tzinfo=UTC),  # 2026-10-20 00:00 PDT
    end=datetime(2026, 11, 17, 8, 0, tzinfo=UTC),  # 2026-11-17 00:00 PST
)
PLENTY = Allowance(steps=10**9, rules=10**6)
START = datetime(2026, 10, 20, 23, 0, tzinfo=UTC)


def run(
    *rules: str,
    start: date = START,
    window: TimeWindow = WINDOW,
    allowance: Allowance = PLENTY,
    rdates: tuple[date, ...] = (),
    exdates: tuple[date, ...] = (),
) -> Expansion:
    recurrence = RecurrenceProperties(
        rules=tuple(icalendar.vRecur.from_ical(rule) for rule in rules),
        rdates=rdates,
        exdates=exdates,
    )
    return expand(recurrence, start, window, "America/Los_Angeles", allowance=allowance)


def days_of(window: TimeWindow) -> int:
    return (window.end - window.start).days + 1


# --- what the walk gives: the same occurrences as dateutil ---------------------------------

WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _pick(rng: random.Random, values: list[int], most: int) -> str:
    return ",".join(str(value) for value in rng.sample(values, rng.randint(1, most)))


def _days_part(rng: random.Random, frequency: str, by_month: bool) -> list[str]:
    parts: list[str] = []
    if frequency != "WEEKLY" and rng.random() < 0.35:
        days = [*range(1, 32), *range(-31, 0)]
        parts.append(f"BYMONTHDAY={_pick(rng, days, 3)}")
    if rng.random() < 0.45:
        ordinal = frequency in ("MONTHLY", "YEARLY") and not parts and rng.random() < 0.5
        if ordinal:
            largest = 4 if frequency == "MONTHLY" or by_month else 52
            ordinals = [*range(1, largest + 1), *range(-largest, 0)]
            items = {
                f"{rng.choice(ordinals)}{rng.choice(WEEKDAYS)}" for _ in range(rng.randint(1, 3))
            }
        else:
            items = set(rng.sample(WEEKDAYS, rng.randint(1, 3)))
        parts.append("BYDAY=" + ",".join(sorted(items)))
    return parts


def random_rule(rng: random.Random, start: datetime) -> str:
    """A rule of the supported kind; some never match, some end by COUNT or UNTIL."""
    frequency = rng.choice(["DAILY", "WEEKLY", "MONTHLY", "YEARLY"])
    parts = [f"FREQ={frequency}"]
    if rng.random() < 0.3:
        parts.append(f"INTERVAL={rng.choice([2, 3, 5])}")
    if rng.random() < 0.3:
        parts.append(f"WKST={rng.choice(WEEKDAYS)}")
    by_month = rng.random() < 0.3
    if by_month:
        parts.append(f"BYMONTH={_pick(rng, list(range(1, 13)), 4)}")
    parts += _days_part(rng, frequency, by_month)
    if frequency in ("MONTHLY", "YEARLY") and rng.random() < 0.3:
        parts.append(f"BYSETPOS={_pick(rng, [1, 2, 3, -1, -2], 2)}")
    if rng.random() < 0.2:
        parts.append(f"BYHOUR={_pick(rng, list(range(24)), 2)}")
    if rng.random() < 0.15:
        parts.append(f"BYMINUTE={_pick(rng, [0, 15, 30, 45], 2)}")
    if rng.random() < 0.25:
        parts.append(f"COUNT={rng.randint(1, 60)}")
    elif rng.random() < 0.25:
        until = start.replace(tzinfo=None) + timedelta(hours=rng.randint(0, 1000 * 24))
        stamp = until.strftime("%Y%m%dT%H%M%S")
        parts.append(f"UNTIL={stamp}Z" if start.tzinfo is not None else f"UNTIL={stamp}")
    return ";".join(parts)


def random_start(rng: random.Random) -> datetime:
    zone: tzinfo | None = rng.choice([UTC, PACIFIC, None])
    day = FIRST_START + timedelta(days=rng.randint(0, 1050))
    return datetime(
        day.year, day.month, day.day, rng.randint(0, 23), rng.choice([0, 30]), 0, 0, zone
    )


def random_window(rng: random.Random, start: datetime) -> TimeWindow:
    """A window from 200 days before DTSTART to 900 after, of 30 minutes to 400 days."""
    begin = start.replace(tzinfo=UTC) + timedelta(minutes=rng.randint(-200, 900) * 1440)
    begin += timedelta(minutes=rng.randint(0, 1439))
    return TimeWindow(start=begin, end=begin + timedelta(minutes=rng.randint(30, 400 * 1440)))


FIRST_START = date(2024, 1, 1)
LAST_YEAR = 2031  # after every window that random_window builds from random_start


@pytest.fixture
def bounded_dateutil(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make dateutil stop after LAST_YEAR instead of the year 9999.

    dateutil looks at UNTIL and COUNT only when it finds an occurrence, so a
    rule that has none after the window would step through every period up
    to ``datetime.MAXYEAR``, which its loop reads from the module ``datetime``.
    """
    assert "datetime.MAXYEAR" in inspect.getsource(dateutil_rrule)
    calendar = types.SimpleNamespace(**vars(datetime_module))
    calendar.MAXYEAR = LAST_YEAR
    monkeypatch.setattr(dateutil_rrule, "datetime", calendar)
    # The cap of one rule would stop some of the long walks; this test is about results.
    monkeypatch.setattr(ics_rules, "MAX_EXPANSION_STEPS", 10**8)


def bounds(window: TimeWindow, start: datetime) -> tuple[datetime, datetime]:
    """The window in the terms of ``start``: aware, or wall time of the floating zone."""
    if start.tzinfo is not None:
        return window.start, window.end
    low, high = (
        moment.astimezone(PACIFIC).replace(tzinfo=None) for moment in (window.start, window.end)
    )
    return low, high


def reference(text: str, start: datetime, low: datetime, high: datetime) -> list[datetime]:
    """The starts that dateutil gives in ``[low, high)``, DTSTART included."""
    rules = rruleset()
    parsed = rrulestr(text, dtstart=start)
    assert isinstance(parsed, rrule)
    rules.rrule(parsed)
    rules.rdate(start)
    found: list[datetime] = []
    for moment in rules:
        if moment >= high:
            break
        if moment >= low:
            found.append(moment)
    return found


@pytest.mark.usefixtures("bounded_dateutil")
def test_the_walk_gives_what_dateutil_gives() -> None:
    # Every rule is compared, the ones that never match included, each in a window of its own.
    rng = random.Random(20261111)
    compared, kinds = 0, Counter[str]()
    for _ in range(700):
        start = random_start(rng)
        text = random_rule(rng, start)
        window = random_window(rng, start)
        try:
            got = run(text, start=start, window=window).starts
        except ValueError:
            continue
        expected = reference(text, start, *bounds(window, start))[:MAX_OCCURRENCES]
        assert list(got) == expected, (text, start, window)
        compared += 1
        if len(expected) > (start in expected):
            parts = dict(part.split("=") for part in text.split(";"))
            kinds[parts["FREQ"] + (" BYSETPOS" if "BYSETPOS" in parts else "")] += 1
    assert compared >= 450, compared
    # Rules of every kind gave occurrences besides DTSTART: the comparison saw them.
    assert min(kinds[kind] for kind in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")) >= 30, kinds
    assert min(kinds["MONTHLY BYSETPOS"], kinds["YEARLY BYSETPOS"]) >= 8, kinds


@pytest.mark.usefixtures("bounded_dateutil")
def test_the_reference_is_bounded_by_the_patched_year() -> None:
    # A rule that never matches: dateutil itself stops after LAST_YEAR, without the walk.
    assert reference(f"{NEVER};BYSETPOS=2", START, START, datetime(2027, 1, 1, tzinfo=UTC)) == [
        START
    ]


def test_a_yearly_rule_with_a_set_position() -> None:
    two_years = TimeWindow(start=WINDOW.start, end=datetime(2028, 1, 1, tzinfo=UTC))
    expansion = run("FREQ=YEARLY;BYMONTH=11;BYDAY=TU;BYSETPOS=-1", window=two_years)
    assert expansion.starts == (
        START,
        datetime(2026, 11, 24, 23, tzinfo=UTC),
        datetime(2027, 11, 30, 23, tzinfo=UTC),
    )
    first = run("FREQ=YEARLY;BYDAY=TU;BYSETPOS=1,-1", window=two_years)
    assert first.starts == (
        START,
        datetime(2026, 12, 29, 23, tzinfo=UTC),
        datetime(2027, 1, 5, 23, tzinfo=UTC),
        datetime(2027, 12, 28, 23, tzinfo=UTC),
    )


# --- what the walk costs -------------------------------------------------------------------

NEVER = "FREQ=MONTHLY;BYMONTHDAY=13,31;BYDAY=TU,MO"  # the 13th and the 31st are never both
POSITIONS = ",".join(str(sign * position) for position in range(185, 367) for sign in (1, -1))
WIDE_TIMES = "BYHOUR=" + ",".join(map(str, range(23))) + ";BYMINUTE=" + ",".join(map(str, range(8)))


MONTHS = 2  # October and November 2026, the periods of a monthly rule in WINDOW


@pytest.mark.parametrize(
    ("rule", "bound"),
    [
        (f"{NEVER};BYSETPOS=2", MONTHS * (2 + 1)),
        (f"{NEVER};BYSETPOS=" + ",".join(["2"] * 200), MONTHS * (2 + 1)),
        (f"{NEVER};{WIDE_TIMES};BYSETPOS={POSITIONS}", MONTHS * (2 + 364)),
        ("FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=13,31;BYDAY=TU,MO;BYSETPOS=2", 2 + 1),
        ("FREQ=DAILY;BYMONTH=2;BYMONTHDAY=13;BYDAY=MO", days_of(WINDOW) + 1),
    ],
)
def test_a_rule_that_never_matches_costs_only_the_periods_of_the_window(
    rule: str, bound: int
) -> None:
    # Accepted rules whose filters never meet in the window, most of them never at all:
    # dateutil walked each of them to the year 9999, the one with 200 copies of a position
    # 200 times. The walk takes the periods up to the end of the window, and in each the
    # days its rule names (the 13th and the 31st) and its positions, once each.
    expansion = run(rule)
    assert expansion.starts == (START,)  # DTSTART is always an occurrence
    assert (expansion.rules, expansion.exceeded) == (1, None)
    assert expansion.steps <= bound


def test_a_rule_with_count_that_never_matches_is_walked_from_dtstart() -> None:
    # COUNT counts from DTSTART, so the walk starts there: three steps or so a month.
    expansion = run(f"{NEVER};BYSETPOS=2;COUNT=3", start=datetime(1800, 1, 1, 23, tzinfo=UTC))
    assert expansion.starts == ()
    assert expansion.steps <= 3 * 12 * (2026 - 1800 + 1)
    assert expansion.exceeded is None


def test_the_cap_of_one_rule_ends_a_long_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ics_rules, "MAX_EXPANSION_STEPS", 1000)
    expansion = run(f"{NEVER};BYSETPOS=2;COUNT=3", start=datetime(1800, 1, 1, 23, tzinfo=UTC))
    assert expansion.starts == ()
    assert 1000 <= expansion.steps <= 1000 + 31 + 1  # it may finish the period it has started
    assert expansion.exceeded is None  # one rule's cap stops it quietly


def test_a_period_costs_the_days_its_rule_can_hit() -> None:
    # A yearly rule on one day reads that day, not the 366 of its year, and a weekly one its
    # weekday, not seven: a long series with COUNT, walked from DTSTART, stays cheap.
    yearly = run("FREQ=YEARLY;COUNT=200", start=datetime(1950, 11, 1, 17, tzinfo=UTC))
    assert yearly.starts == (datetime(2026, 11, 1, 17, tzinfo=UTC),)
    assert yearly.steps <= 2 * (2026 - 1950 + 1)  # one day and one occurrence a year
    weekly = run("FREQ=WEEKLY;COUNT=2000", start=datetime(2000, 1, 4, 17, tzinfo=UTC))
    assert weekly.starts == tuple(
        datetime(2026, 10, 20, 17, tzinfo=UTC) + timedelta(weeks=n) for n in range(4)
    )
    weeks = (date(2026, 11, 17) - date(2000, 1, 4)).days // 7 + 1
    assert weekly.steps <= 2 * weeks
    third = run(
        "FREQ=YEARLY;BYMONTH=3;BYDAY=3TU;COUNT=100", start=datetime(1950, 3, 21, tzinfo=UTC)
    )
    assert third.steps <= 2 * (2026 - 1950 + 1)


def test_a_rule_without_count_is_walked_from_the_window_whatever_its_dtstart() -> None:
    expansion = run("FREQ=DAILY", start=datetime(1990, 1, 1, 23, tzinfo=UTC))
    assert expansion.starts == tuple(START + timedelta(days=n) for n in range(28))
    assert expansion.steps <= 2 * days_of(WINDOW) + 4


def test_a_rule_with_count_counts_the_occurrences_before_the_window() -> None:
    yearly = datetime(1990, 10, 25, 17, tzinfo=UTC)
    assert run("FREQ=YEARLY;COUNT=37", start=yearly).starts == (
        datetime(2026, 10, 25, 17, tzinfo=UTC),
    )
    assert run("FREQ=YEARLY;COUNT=36", start=yearly).starts == ()
    weekly = run("FREQ=WEEKLY;COUNT=5", start=datetime(2026, 9, 29, 23, tzinfo=UTC))
    assert weekly.starts == (START, START + timedelta(weeks=1))


def test_until_before_the_end_of_the_window_ends_the_walk() -> None:
    expansion = run("FREQ=DAILY;UNTIL=20261025T000000Z", start=datetime(1990, 1, 1, 23, tzinfo=UTC))
    assert expansion.starts == tuple(START + timedelta(days=n) for n in range(5))
    assert expansion.steps <= 2 * 7
    ended = run("FREQ=DAILY;UNTIL=20261019T000000Z", start=datetime(1990, 1, 1, 23, tzinfo=UTC))
    assert (ended.starts, ended.steps, ended.rules) == ((), 0, 0)


def test_an_until_before_the_end_of_the_window_bounds_a_rule_that_never_matches() -> None:
    two_years = TimeWindow(start=WINDOW.start, end=WINDOW.start + timedelta(days=730))
    unbounded = run(f"{NEVER};BYSETPOS=2", window=two_years)
    bounded = run(f"{NEVER};BYSETPOS=2;UNTIL=20261101T000000Z", window=two_years)
    assert unbounded.starts == bounded.starts == (START,)
    # Only October and November are walked, not the 25 months of the window.
    assert bounded.steps * 10 < unbounded.steps


def test_a_repeated_position_is_one_position() -> None:
    once = run("FREQ=MONTHLY;BYDAY=MO,TU;BYSETPOS=2")
    assert once.starts == (START, datetime(2026, 11, 3, 23, tzinfo=UTC))
    again = run("FREQ=MONTHLY;BYDAY=MO,TU;BYSETPOS=" + ",".join(["2"] * 200))
    assert (again.starts, again.steps) == (once.starts, once.steps)


def test_days_and_ordinals_a_period_does_not_have_cost_steps_too() -> None:
    # The fifth weekdays of February: one of them in a leap year, none in the others. Every
    # ordinal tried is a step, so the work of a period is never more than its steps.
    fifth = ",".join(f"{sign}5{day}" for sign in ("", "-") for day in WEEKDAYS)
    start = datetime(1900, 2, 1, 23, tzinfo=UTC)
    expansion = run(f"FREQ=YEARLY;BYMONTH=2;BYDAY={fifth};COUNT=1000", start=start)
    assert expansion.starts == ()
    assert expansion.steps >= len(WEEKDAYS) * 2 * (2026 - 1900 + 1)
    late = run("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29,30,31;COUNT=1000", start=start)
    assert late.steps >= 3 * (2026 - 1900 + 1)  # three days tried every year, one at most found


EVERY_SECOND = "FREQ=DAILY;BYHOUR={};BYMINUTE={};BYSECOND={}".format(
    *(",".join(map(str, range(n))) for n in (24, 60, 60))
)


def test_the_times_of_a_day_are_not_listed_before_they_are_needed() -> None:
    # 86400 times of day: the walk hands out the first MAX_OCCURRENCES and stops.
    expansion = run(EVERY_SECOND, start=datetime(2026, 10, 21, 0, 0, tzinfo=UTC))
    assert len(expansion.starts) == MAX_OCCURRENCES
    assert expansion.steps <= 2 + MAX_OCCURRENCES + 1


@pytest.mark.parametrize("zone", [UTC, ZoneInfo("UTC"), PACIFIC, None])
def test_the_times_before_the_window_cost_no_steps(zone: tzinfo | None) -> None:
    # A DTSTART long before the window: the times of the day the window starts on that lie
    # before it are left out unbuilt, where no change of UTC offset is near.
    expansion = run(EVERY_SECOND, start=datetime(2026, 1, 1, 0, 0, tzinfo=zone))
    low = WINDOW.start if zone is not None else datetime(2026, 10, 20, 0, 0)
    assert expansion.starts == tuple(low + timedelta(seconds=n) for n in range(MAX_OCCURRENCES))
    assert expansion.steps <= 2 + MAX_OCCURRENCES + 1


@pytest.mark.usefixtures("bounded_dateutil")
@pytest.mark.parametrize(
    "begin",
    [
        datetime(2026, 11, 1, 8, 30, tzinfo=UTC),  # 01:30 PDT, the hour that comes twice
        datetime(2026, 11, 1, 9, 30, tzinfo=UTC),  # 01:30 PST, its second time
        datetime(2026, 11, 1, 10, 0, tzinfo=UTC),  # 02:00 PST
        datetime(2026, 3, 8, 10, 0, tzinfo=UTC),  # 03:00 PDT, right after the skipped hour
        datetime(2026, 3, 8, 10, 30, tzinfo=UTC),  # 03:30 PDT
        datetime(2026, 3, 8, 9, 59, tzinfo=UTC),  # 01:59 PST, right before it
    ],
)
def test_the_times_before_a_window_next_to_a_change_of_offset(begin: datetime) -> None:
    # Near a change of offset the order of wall times is not that of the moments (02:30 of
    # the skipped hour is 10:30 UTC, after 03:00 PDT): the walk compares each time from three
    # hours before the window on, and gives what dateutil gives.
    window = TimeWindow(start=begin, end=begin + timedelta(hours=6))
    rule = "FREQ=DAILY;BYMINUTE=0,15,30,45;BYHOUR=" + ",".join(map(str, range(24)))
    for start in (datetime(2026, 1, 1, tzinfo=PACIFIC), datetime(2026, 1, 1)):
        expansion = run(rule, start=start, window=window)
        assert list(expansion.starts) == reference(rule, start, *bounds(window, start))
        assert expansion.steps <= 2 + 4 * 3 + 4 * 6 + 8 + 1


def test_every_rrule_line_is_counted_and_walked() -> None:
    single = run(f"{NEVER};BYSETPOS=2")
    triple = run(*[f"{NEVER};BYSETPOS=2"] * 3)
    assert (triple.rules, triple.steps) == (3, 3 * single.steps)
    plain = run("FREQ=WEEKLY", f"{NEVER};BYSETPOS=2")
    assert plain.rules == 2


@pytest.mark.parametrize(
    ("allowance", "exceeded", "walked"),
    [
        (Allowance(steps=10**9, rules=2), "rules", 2),
        (Allowance(steps=1, rules=10), "steps", 1),
        (Allowance(steps=10**9, rules=3), None, 3),
    ],
)
def test_an_expansion_stops_at_the_allowance_of_the_feed(
    allowance: Allowance, exceeded: str | None, walked: int
) -> None:
    # Three lines of one VEVENT; a walk may finish the period it has started.
    single = run(f"{NEVER};BYSETPOS=2")
    expansion = run(*[f"{NEVER};BYSETPOS=2"] * 3, allowance=allowance)
    assert expansion.exceeded == exceeded
    assert expansion.rules == (walked + 1 if exceeded == "rules" else walked)
    assert expansion.steps <= walked * single.steps


def test_the_cap_of_one_rule_stops_it_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ics_rules, "MAX_EXPANSION_STEPS", 10)
    daily = "FREQ=DAILY;COUNT=40"
    early = datetime(2026, 10, 1, 23, tzinfo=UTC)
    assert run(daily, start=early).starts == ()  # the window is more than 10 steps away
    assert run(daily, start=early, allowance=Allowance(steps=10, rules=1)).exceeded == "steps"
    # Ten times of day: the cap is reached between two occurrences of one day.
    hourly = run(
        "FREQ=DAILY;BYHOUR=1,2,3,4,5,6,7,8,9,10", start=datetime(2026, 10, 21, 1, tzinfo=UTC)
    )
    assert hourly.steps == 10
    assert hourly.starts == tuple(datetime(2026, 10, 21, hour, tzinfo=UTC) for hour in range(1, 10))


def test_rdates_exdates_and_dtstart() -> None:
    expansion = run(
        "FREQ=WEEKLY;COUNT=3",
        rdates=(datetime(2026, 10, 22, 23, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)),
        exdates=(START + timedelta(weeks=1),),
    )
    assert expansion.starts == (
        START,
        datetime(2026, 10, 22, 23, tzinfo=UTC),
        START + timedelta(weeks=2),
    )


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=DAILY;BYHOUR=24",
        "FREQ=DAILY;BYMINUTE=60",
        "FREQ=DAILY;BYSECOND=60",
        "FREQ=MONTHLY;BYDAY=0MO",
        "FREQ=MONTHLY;BYDAY=MO;BYSETPOS=0",
        "FREQ=YEARLY;BYDAY=MO;BYSETPOS=400",
        "FREQ=DAILY;RSCALE=GREGORIAN",
        "FREQ=DAILY;X-NAME=1",
    ],
)
def test_values_that_name_nothing_are_refused(rule: str) -> None:
    with pytest.raises(ValueError, match=r"names no|unsupported|BYDAY"):
        run(rule)


def test_a_weekday_that_does_not_exist_is_refused() -> None:
    # icalendar refuses it while reading; the guard does not rely on that.
    with pytest.raises(ValueError, match="BYDAY names no weekday"):
        ics_rules.check_rule({"FREQ": ["WEEKLY"], "BYDAY": ["XX"]})


def test_a_count_of_zero_leaves_dtstart_alone() -> None:
    assert run("FREQ=DAILY;COUNT=0").starts == (START,)


def test_the_walk_ends_with_the_calendar() -> None:
    end_of_time = TimeWindow(
        start=datetime(9999, 12, 30, tzinfo=UTC), end=datetime(9999, 12, 31, 23, tzinfo=UTC)
    )
    for rule in ("FREQ=DAILY", "FREQ=WEEKLY", "FREQ=MONTHLY", "FREQ=YEARLY"):
        expansion = run(rule, start=datetime(9999, 12, 30, 1, tzinfo=UTC), window=end_of_time)
        assert expansion.starts[0] == datetime(9999, 12, 30, 1, tzinfo=UTC)


# --- the guard and the start of a walk -----------------------------------------------------

LATE = datetime(2026, 10, 25, 23, 59, 0, tzinfo=UTC)


def test_a_rule_that_repeats_its_months_and_days_is_checked_in_linear_time() -> None:
    months = ",".join(["2"] * 20_000)
    days = ",".join(["31"] * 20_000)
    rule = icalendar.vRecur.from_ical(f"FREQ=YEARLY;BYMONTH={months};BYMONTHDAY={days}")
    parts: dict[str, list[Any]] = dict(rule)
    started = time.perf_counter()
    with pytest.raises(ValueError, match="BYMONTHDAY names no day"):
        ics_rules.check_rule(parts)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize("count", ["", ";COUNT=5"])
def test_the_times_before_dtstart_cost_no_steps(count: str) -> None:
    expansion = run(f"{EVERY_SECOND}{count}", start=LATE)
    assert expansion.starts[:5] == tuple(LATE + timedelta(seconds=n) for n in range(5))
    if count:
        assert len(expansion.starts) == 5
        assert expansion.steps < 100
    else:
        assert len(expansion.starts) == MAX_OCCURRENCES


@pytest.mark.parametrize(
    ("rule", "first"),
    [
        ("FREQ=MONTHLY;BYDAY=1TU,3TU;BYSETPOS=1,3", datetime(2026, 11, 3, 23, 0, tzinfo=UTC)),
        (
            "FREQ=DAILY;INTERVAL=2;BYDAY=TU;BYMONTHDAY=13",
            None,  # walked: no Tuesday the 13th on the parity of DTSTART in the window
        ),
        ("FREQ=DAILY;INTERVAL=1;BYDAY=FR;BYMONTHDAY=13", datetime(2026, 11, 13, 23, 0, tzinfo=UTC)),
    ],
)
def test_rules_that_can_match_are_walked(rule: str, first: datetime | None) -> None:
    parts: dict[str, list[Any]] = dict(icalendar.vRecur.from_ical(rule))
    ics_rules.check_rule(parts, start=START)
    starts = run(rule).starts
    assert starts[0] == START
    assert (starts[1] if len(starts) > 1 else None) == first
