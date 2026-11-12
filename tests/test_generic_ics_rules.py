"""The walk of recurrence rules (ics_rules): what it gives, and what it costs."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

import icalendar
import pytest
from dateutil.rrule import rrule, rruleset, rrulestr

from seminars.models import TimeWindow
from seminars.sources.generic import ics_rules
from seminars.sources.generic.ics_rules import (
    MAX_EXPANSION_STEPS,
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
PLENTY = Allowance(steps=10**9, rules=10**6, filtered=10**6)
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
LONG_WINDOW = TimeWindow(
    start=datetime(2026, 10, 1, 7, 0, tzinfo=UTC), end=datetime(2027, 4, 1, 7, 0, tzinfo=UTC)
)


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
        until = datetime(2026, 11, 1, tzinfo=UTC) + timedelta(days=rng.randint(0, 120))
        stamp = until.strftime("%Y%m%dT%H%M%S")
        parts.append(f"UNTIL={stamp}Z" if start.tzinfo is not None else f"UNTIL={stamp}")
    return ";".join(parts)


def random_start(rng: random.Random) -> datetime:
    zone: tzinfo | None = rng.choice([UTC, PACIFIC, None])
    day = date(2024, 1, 1) + timedelta(days=rng.randint(0, 1050))
    return datetime(
        day.year, day.month, day.day, rng.randint(0, 23), rng.choice([0, 30]), 0, 0, zone
    )


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


def continues(text: str, start: datetime, after: datetime) -> bool:
    """Whether the rule without COUNT and UNTIL has an occurrence in the 6 years after ``after``.

    dateutil looks at UNTIL and COUNT only when it finds an occurrence, so
    without one after the window it would step through every period until
    the year 9999; such rules are left out of the comparison.
    """
    plain = ";".join(part for part in text.split(";") if not part.startswith(("COUNT", "UNTIL")))
    begin = after if after.tzinfo is not None else after.replace(tzinfo=PACIFIC)
    later = TimeWindow(start=begin, end=begin + timedelta(days=6 * 366))
    return bool(run(plain, start=start, window=later).starts)


def test_the_walk_gives_what_dateutil_gives() -> None:
    rng = random.Random(20261111)
    compared = refused = 0
    for _ in range(600):
        start = random_start(rng)
        text = random_rule(rng, start)
        try:
            got = run(text, start=start, window=LONG_WINDOW).starts
        except ValueError:
            refused += 1
            continue
        low, high = ics_rules._window_bounds(LONG_WINDOW, start, "America/Los_Angeles")
        until = icalendar.vRecur.from_ical(text).get("UNTIL")
        last = max(high, until[0]) if until else high
        if not continues(text, start, last):
            continue
        assert list(got) == reference(text, start, low, high)[:MAX_OCCURRENCES], (text, start)
        compared += 1
    assert compared >= 300, (compared, refused)


# --- what the walk costs -------------------------------------------------------------------

NEVER = "FREQ=MONTHLY;BYMONTHDAY=13,31;BYDAY=TU,MO"  # the 13th and the 31st are never both
POSITIONS = ",".join(str(sign * position) for position in range(185, 367) for sign in (1, -1))
WIDE_TIMES = "BYHOUR=" + ",".join(map(str, range(23))) + ";BYMINUTE=" + ",".join(map(str, range(8)))


MONTHS = 31 + 30  # October and November 2026, the periods of a monthly rule in WINDOW


@pytest.mark.parametrize(
    ("rule", "bound"),
    [
        (f"{NEVER};BYSETPOS=2", MONTHS + 2),
        (f"{NEVER};BYSETPOS=" + ",".join(["2"] * 200), MONTHS + 2),
        (f"{NEVER};{WIDE_TIMES};BYSETPOS={POSITIONS}", MONTHS + 2 * 364),
        ("FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=13,31;BYDAY=TU,MO;BYSETPOS=2", 365 + 1),
        ("FREQ=DAILY;BYMONTH=2;BYMONTHDAY=13;BYDAY=MO", days_of(WINDOW) + 1),
    ],
)
def test_a_rule_that_never_matches_costs_only_the_periods_of_the_window(
    rule: str, bound: int
) -> None:
    # Accepted rules whose filters never meet in the window, most of them never at all:
    # dateutil walked each of them to the year 9999, the one with 200 copies of a position
    # 200 times. The walk takes the days of the periods up to the end of the window.
    expansion = run(rule)
    assert expansion.starts == (START,)  # DTSTART is always an occurrence
    assert (expansion.rules, expansion.filtered, expansion.exceeded) == (1, 1, None)
    assert expansion.steps <= bound


def test_a_rule_with_count_that_never_matches_stops_after_its_steps() -> None:
    # COUNT counts from DTSTART, so the walk starts there; the cap of one rule ends it.
    expansion = run(f"{NEVER};BYSETPOS=2;COUNT=3", start=datetime(1800, 1, 1, 23, tzinfo=UTC))
    assert expansion.starts == ()
    assert MAX_EXPANSION_STEPS <= expansion.steps <= MAX_EXPANSION_STEPS + 31 + 1
    assert expansion.exceeded is None


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


def test_a_repeated_position_is_one_position() -> None:
    once = run("FREQ=MONTHLY;BYDAY=MO,TU;BYSETPOS=2")
    assert once.starts == (START, datetime(2026, 11, 3, 23, tzinfo=UTC))
    again = run("FREQ=MONTHLY;BYDAY=MO,TU;BYSETPOS=" + ",".join(["2"] * 200))
    assert (again.starts, again.steps) == (once.starts, once.steps)


def test_the_times_of_a_day_are_not_listed_before_they_are_needed() -> None:
    # 86400 times of day: the walk hands out the first MAX_OCCURRENCES and stops.
    every_second = "FREQ=DAILY;BYHOUR={};BYMINUTE={};BYSECOND={}".format(
        *(",".join(map(str, range(n))) for n in (24, 60, 60))
    )
    expansion = run(every_second, start=datetime(2026, 10, 21, 0, 0, tzinfo=UTC))
    assert len(expansion.starts) == MAX_OCCURRENCES
    assert expansion.steps <= 2 + MAX_OCCURRENCES + 1


def test_every_rrule_line_is_counted_and_walked() -> None:
    single = run(f"{NEVER};BYSETPOS=2")
    triple = run(*[f"{NEVER};BYSETPOS=2"] * 3)
    assert (triple.rules, triple.filtered, triple.steps) == (3, 3, 3 * single.steps)
    plain = run("FREQ=WEEKLY", f"{NEVER};BYSETPOS=2")
    assert (plain.rules, plain.filtered) == (2, 1)


@pytest.mark.parametrize(
    ("allowance", "exceeded", "walked"),
    [
        (Allowance(steps=10**9, rules=2, filtered=10), "rules", 2),
        (Allowance(steps=10**9, rules=10, filtered=1), "filtered", 1),
        (Allowance(steps=1, rules=10, filtered=10), "steps", 1),
        (Allowance(steps=10**9, rules=3, filtered=3), None, 3),
    ],
)
def test_an_expansion_stops_at_the_allowance_of_the_feed(
    allowance: Allowance, exceeded: str | None, walked: int
) -> None:
    # Every line of this VEVENT filters; a walk may finish the period it has started.
    single = run(f"{NEVER};BYSETPOS=2")
    expansion = run(*[f"{NEVER};BYSETPOS=2"] * 3, allowance=allowance)
    assert expansion.exceeded == exceeded
    assert expansion.rules == (walked + 1 if exceeded in ("rules", "filtered") else walked)
    assert expansion.steps <= walked * single.steps


def test_the_cap_of_one_rule_stops_it_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ics_rules, "MAX_EXPANSION_STEPS", 10)
    daily = "FREQ=DAILY;COUNT=40"
    early = datetime(2026, 10, 1, 23, tzinfo=UTC)
    assert run(daily, start=early).starts == ()  # the window is more than 10 steps away
    assert run(daily, start=early, allowance=Allowance(steps=10, rules=1, filtered=0)).exceeded == (
        "steps"
    )
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
