"""Recurrence rules of iCalendar events: which ones are supported, and their expansion.

The ``ics`` adapter expands recurring VEVENTs inside the run window with the
walk in this module, not with a recurrence library: dateutil checks UNTIL
and COUNT only on the occurrences it finds, so a rule whose filters never
match steps through every period until the year 9999, UNTIL or not, and no
cap that counts events or occurrences sees that cost.

Here every RRULE gets an effective end before it is walked: the end of the
window, or its own UNTIL when that is earlier. A rule without COUNT is
walked only through its periods (days, weeks, months or years, INTERVAL
apart) from the one before the window starts up to that end; a rule with
COUNT from its DTSTART, because the occurrences before the window count.
So what one rule costs is bounded by its periods up to the end of the
window, whatever its filters, and every unit of that work is a step: in
each period the days the rule can hit (``_candidates``: the days of
BYMONTHDAY, else the weekdays of BYDAY, in the months of BYMONTH; at least
one step a period), each BYSETPOS entry (duplicates are dropped) and each
time of day the walk builds. A rule without COUNT does not build the times
of day before the window (``_skip_before``). One rule takes at most
``MAX_EXPANSION_STEPS`` steps (and the rest of a period it has started;
then its walk stops where it is, so a series with COUNT that began more
than about 25000 periods before the window gives nothing in it), and the
caps of a whole feed (``seminars.sources.generic.ics``) count the steps
and the RRULE lines of all its VEVENTs together.

``check_rule`` is a cheap first filter: it refuses what the walk does not
implement and shapes that can never match, which are then not walked at
all. The bound above does not depend on it catching every such shape.

The cost, measured through ``IcsAdapter.parse`` on one core: a step takes
0.6 to 2 us, so a feed that spends the whole step budget of the adapter
(500000) takes 0.3 to 1 s and is then a ``ParseError``. Rules that took
dateutil seconds take milliseconds: BYSETPOS=2 written 200 times 0.001 s
(8.9 s), the 364 positions +-185..+-366 with BYHOUR=0..22;BYMINUTE=0..7
0.001 s (16 s), 100 VEVENTs of that rule 0.1 s, one VEVENT with 50 RRULE
lines of a rule that never matches 0.004 s (50 x 0.29 s). The worst feeds
built for this (rules that never match with COUNT and a DTSTART in 1900,
every position of a year, 500 RRULE lines in one VEVENT) spend the budget
and fail after at most 1 s. A long series with COUNT costs about two
steps per period since its DTSTART: a feed holds 170 weekly series that
began in 2000, or 25 daily ones, within the budget. Turning occurrences
into drafts costs more than finding them, about 20 us each: 500 recurring
VEVENTs with 500 occurrences each in the window (250000 drafts, also with
86400 times of day each) take about 5 s. This module registers no adapter.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

import icalendar

from seminars.models import TimeWindow

MAX_OCCURRENCES: Final = 500
"""Most occurrences of one recurring event inside the window."""

MAX_EXPANSION_STEPS: Final = 50_000
"""Most steps through one recurrence rule; more stop its walk where it is."""

Moment = date | datetime
Limit = Literal["steps", "rules"]
"""Which allowance of a feed an expansion needed more of."""

_RULE_PARTS: Final = frozenset(
    {"FREQ", "UNTIL", "COUNT", "INTERVAL", "WKST", "BYMONTH", "BYMONTHDAY", "BYDAY"}
    | {"BYSETPOS", "BYHOUR", "BYMINUTE", "BYSECOND"}
)
_DAYS_IN_MONTH: Final = {
    month: 29 if month == 2 else 30 if month in (4, 6, 9, 11) else 31 for month in range(1, 13)
}
_WEEKDAYS: Final = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_TIME_PARTS: Final = (("BYHOUR", 23), ("BYMINUTE", 59), ("BYSECOND", 59))
_ONE_DAY: Final = timedelta(days=1)
_MARGIN: Final = timedelta(hours=3)
"""More than daylight saving moves the wall clock (1 h, in a few zones 2 h)."""


# --- the guard -----------------------------------------------------------------------------


def check_rule(parts: dict[str, list[Any]], start: date | None = None) -> None:
    """Refuse a recurrence rule that the expansion does not support (``ValueError``).

    The walk implements ``FREQ`` ``DAILY``, ``WEEKLY``, ``MONTHLY`` and
    ``YEARLY`` with ``INTERVAL``, ``COUNT``, ``UNTIL``, ``WKST``, ``BYMONTH``,
    ``BYMONTHDAY``, ``BYDAY``, ``BYSETPOS``, ``BYHOUR``, ``BYMINUTE`` and
    ``BYSECOND``; any other part is refused. So are values that name nothing
    (``_check_calendar_days``, ``_check_times``), a ``BYDAY`` that mixes
    weekdays with and without ordinals (``_check_ordinals``), ordinals and
    ``BYSETPOS`` where no period has them (``_set_size_bound``) and, with
    ``start`` (the DTSTART), an ``INTERVAL`` and days that never land on a
    period the filters accept (``_check_from_start``).

    This is a first filter that spares the walk of rules that cannot match,
    not what bounds the cost: rules that pass and still never match exist
    (a ``BYSETPOS`` beyond the days that ``BYMONTHDAY`` and ``BYDAY`` have in
    common), and they cost the steps of their periods like any other rule.
    """
    keys = {str(key).upper(): value for key, value in parts.items()}
    frequency = [str(value).upper() for value in keys.get("FREQ", [])]
    if frequency not in (["DAILY"], ["WEEKLY"], ["MONTHLY"], ["YEARLY"]):
        raise ValueError(f"unsupported recurrence frequency {frequency}")
    unsupported = sorted(set(keys) - _RULE_PARTS)
    if unsupported:
        raise ValueError(f"unsupported recurrence rule part(s) {', '.join(unsupported)}")
    _check_calendar_days(keys, frequency[0])
    _check_times(keys)
    _check_ordinals(keys, frequency[0])
    _check_positions(keys, frequency[0], start)
    if start is not None:
        _check_from_start(keys, frequency[0], start)


def _check_calendar_days(keys: dict[str, list[Any]], frequency: str) -> None:
    """Months 1 to 12, days 1 to 31 (or -31 to -1) and a day that one of the months has.

    BYMONTHDAY in a weekly rule is refused as well (RFC 5545 forbids it).
    """
    months, days = keys.get("BYMONTH"), keys.get("BYMONTHDAY")
    if any(not 1 <= int(month) <= 12 for month in months or []):
        raise ValueError("BYMONTH names no month")
    if any(not 1 <= abs(int(day)) <= 31 for day in days or []):
        raise ValueError("BYMONTHDAY names no day of a month")
    if days and frequency == "WEEKLY":
        raise ValueError("BYMONTHDAY is not valid in a weekly rule")
    if months and days:
        possible = any(
            abs(int(day)) <= _DAYS_IN_MONTH.get(int(month), 0) for month in months for day in days
        )
        if not possible:
            raise ValueError("BYMONTHDAY names no day of the months in BYMONTH")


def _check_times(keys: dict[str, list[Any]]) -> None:
    """Hours 0 to 23, minutes and seconds 0 to 59 (a leap second has no datetime)."""
    for part, largest in _TIME_PARTS:
        if any(not 0 <= int(value) <= largest for value in keys.get(part, [])):
            raise ValueError(f"{part} names no time of day")


def _weekdays(keys: dict[str, list[Any]]) -> tuple[set[str], list[int]]:
    """The BYDAY weekdays without an ordinal (``MO``), and the ordinals of the others (abs)."""
    plain: set[str] = set()
    ordinals: list[int] = []
    for day in (str(value).upper() for value in keys.get("BYDAY", [])):
        if day[-2:] not in _WEEKDAYS:
            raise ValueError("BYDAY names no weekday")
        if day[:-2]:
            ordinals.append(abs(int(day[:-2])))
        else:
            plain.add(day)
    return plain, ordinals


def _check_ordinals(keys: dict[str, list[Any]], frequency: str) -> None:
    """BYDAY with an ordinal (``2MO``, ``-1FR``) only where a period has that many weekdays.

    A BYDAY that mixes weekdays with and without an ordinal (``TH,1FR``) is
    refused: RFC 5545 takes the union of the two kinds, other implementations
    (dateutil) the intersection, which no day is in when the weekdays differ.
    """
    plain, ordinals = _weekdays(keys)
    if not ordinals:
        return
    if frequency in ("DAILY", "WEEKLY"):
        raise ValueError("BYDAY with an ordinal is only valid in a monthly or yearly rule")
    if plain:
        raise ValueError("BYDAY mixes weekdays with and without an ordinal")
    if "BYMONTHDAY" in keys:
        # The ordinal already fixes the day; the two together rarely meet, if ever.
        raise ValueError("BYDAY with an ordinal is not supported together with BYMONTHDAY")
    limit = 5 if frequency == "MONTHLY" or keys.get("BYMONTH") else 53
    if min(ordinals) < 1 or max(ordinals) > limit:
        raise ValueError("BYDAY names an ordinal that no period has")


def _check_positions(keys: dict[str, list[Any]], frequency: str, start: date | None) -> None:
    """BYSETPOS only in a monthly or yearly rule, and only for positions a period can have.

    Positions are 1 to 366 or -366 to -1, as RFC 5545 has them; a repeated
    one is one position (the walk drops duplicates).
    """
    if "BYSETPOS" not in keys:
        return
    if frequency not in ("MONTHLY", "YEARLY"):
        raise ValueError("BYSETPOS is only supported with a monthly or yearly rule")
    positions = {abs(int(position)) for position in keys["BYSETPOS"]}
    if not positions or min(positions) < 1 or max(positions) > 366:
        raise ValueError("BYSETPOS names no position of a set")
    day = None if start is None else start.day
    if max(positions) > _set_size_bound(keys, yearly=frequency == "YEARLY", day=day):
        raise ValueError("BYSETPOS names a position that no period has")


def _check_from_start(keys: dict[str, list[Any]], frequency: str, start: date) -> None:
    """Refuse a rule that, from ``start``, never lands on a day that its filters accept.

    A daily rule whose INTERVAL is a multiple of 7 stays on the weekday of
    ``start``, which its BYDAY must name; with an INTERVAL it may not combine
    BYDAY and BYMONTHDAY (INTERVAL=27 can step through centuries without a
    match). A monthly rule only reaches the months congruent to the month of
    ``start`` modulo gcd(INTERVAL, 12), one of which its BYMONTH must name.
    One of the months that a monthly or yearly rule reaches must have a day
    of its BYMONTHDAY, or without BYDAY and BYMONTHDAY the day of ``start``,
    which such a rule repeats (no June has the 31st).
    """
    interval = int(keys.get("INTERVAL", [1])[0])
    if interval < 1:
        raise ValueError("INTERVAL must be positive")
    weekdays = {str(day).upper()[-2:] for day in keys.get("BYDAY", [])}
    if frequency == "DAILY" and interval > 1 and weekdays:
        if interval % 7 == 0 and _WEEKDAYS[start.weekday()] not in weekdays:
            raise ValueError("BYDAY never names the weekday that this daily rule falls on")
        if "BYMONTHDAY" in keys:
            raise ValueError("BYDAY with BYMONTHDAY is not supported in a daily rule with INTERVAL")
    months = sorted({int(month) for month in keys.get("BYMONTH", [])}) or list(range(1, 13))
    if frequency == "MONTHLY":
        step = math.gcd(interval, 12)
        months = [month for month in months if (month - start.month) % step == 0]
        if not months:
            raise ValueError("BYMONTH never names a month that this monthly rule falls on")
    if frequency in ("MONTHLY", "YEARLY") and ("BYMONTHDAY" in keys or "BYDAY" not in keys):
        days = [abs(int(day)) for day in keys.get("BYMONTHDAY", [start.day])]
        if all(day > _DAYS_IN_MONTH[month] for month in months for day in days):
            raise ValueError(
                "no month that this rule falls on has the day of BYMONTHDAY or DTSTART"
            )


def _set_size_bound(keys: dict[str, list[Any]], *, yearly: bool, day: int | None = None) -> int:
    """An upper bound of the number of occurrences in one month or year of a rule.

    A monthly period is one month, which BYMONTH only selects; a yearly one
    has the months of BYMONTH, or the whole year. In a month an ordinal
    weekday is one day, a plain weekday at most five and BYMONTHDAY the days
    that month has; in a whole year an ordinal is one day and a plain weekday
    at most 53. Without BYDAY and BYMONTHDAY the rule repeats ``day`` (that
    of DTSTART, if known): once in each month of BYMONTH that has it, else
    once.
    """
    months = sorted({int(month) for month in keys.get("BYMONTH", [])})
    weekdays = [str(item) for item in keys.get("BYDAY", [])]
    monthdays = {int(item) for item in keys.get("BYMONTHDAY", [])}
    times = math.prod(
        len(set(keys.get(part, []))) or 1 for part in ("BYHOUR", "BYMINUTE", "BYSECOND")
    )
    if not weekdays and not monthdays:
        if yearly and not months:
            return times  # the month and the day of DTSTART
        monthdays = set() if day is None else {day}
    ordinals = sum(1 for item in weekdays if item[:1] in "+-0123456789")
    plain = len(weekdays) - ordinals
    in_month = ordinals + 5 * plain if weekdays else None
    if not yearly:
        size = max(_month_bound(month, in_month, monthdays) for month in months or range(1, 13))
    elif months or monthdays:
        size = sum(_month_bound(month, in_month, monthdays) for month in months or range(1, 13))
    else:
        size = ordinals + 53 * plain
    return size * times


def _month_bound(month: int, in_month: int | None, monthdays: set[int]) -> int:
    """At most how many days of ``month`` a rule picks; ``in_month`` bounds its BYDAY.

    The days of BYMONTHDAY are counted once each as days of the month (in a
    month of 30 days, 30 and -1 are one day), February with 28 or 29 days.
    """
    counts = [] if in_month is None else [in_month]
    if monthdays:
        lengths = (28, 29) if month == 2 else (_DAYS_IN_MONTH[month],)
        counts.append(
            max(len({day % (n + 1) for day in monthdays if abs(day) <= n}) for n in lengths)
        )
    return min(counts, default=1)


# --- one rule ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rule:
    """One checked RRULE, with what RFC 5545 takes from DTSTART made explicit.

    ``until`` is comparable with the DTSTART it was read for (aware in UTC
    for an aware DTSTART, else floating). ``monthdays`` holds positive and
    negative days; ``ordinals`` the ``(ordinal, weekday)`` pairs of BYDAY
    and ``weekdays`` its plain weekdays (0 is Monday).
    """

    frequency: str
    interval: int
    count: int | None
    until: datetime | None
    week_start: int
    months: frozenset[int]
    monthdays: frozenset[int]
    weekdays: frozenset[int]
    ordinals: frozenset[tuple[int, int]]
    positions: tuple[int, ...]
    hours: tuple[int, ...]
    minutes: tuple[int, ...]
    seconds: tuple[int, ...]

    @property
    def times_per_day(self) -> int:
        return len(self.hours) * len(self.minutes) * len(self.seconds)

    def time_at(self, index: int) -> time:
        """The ``index``-th time of day of the rule, in order (no list of them is built)."""
        minutes, seconds = len(self.minutes), len(self.seconds)
        return time(
            self.hours[index // (minutes * seconds)],
            self.minutes[index // seconds % minutes],
            self.seconds[index % seconds],
        )


def _read_rule(value: object, anchor: datetime, timezone: str) -> _Rule:
    """The RRULE ``value`` of a VEVENT whose DTSTART is ``anchor``; ``ValueError`` if refused."""
    if not isinstance(value, icalendar.vRecur):
        raise ValueError("the RRULE cannot be read")
    parts: dict[str, list[Any]] = {str(key).upper(): list(items) for key, items in value.items()}
    check_rule(parts, start=anchor)
    frequency = str(parts["FREQ"][0]).upper()
    weekdays: set[int] = set()
    ordinals: set[tuple[int, int]] = set()
    for text in (str(item).upper() for item in parts.get("BYDAY", [])):
        weekday = _WEEKDAYS.index(text[-2:])
        if text[:-2]:
            ordinals.add((int(text[:-2]), weekday))
        else:
            weekdays.add(weekday)
    months = {int(month) for month in parts.get("BYMONTH", [])}
    monthdays = {int(day) for day in parts.get("BYMONTHDAY", [])}
    if not (weekdays or ordinals or monthdays):  # the day of DTSTART comes back
        if frequency == "YEARLY":
            months, monthdays = months or {anchor.month}, {anchor.day}
        elif frequency == "MONTHLY":
            monthdays = {anchor.day}
        elif frequency == "WEEKLY":
            weekdays = {anchor.weekday()}
    clock = {
        part: tuple(sorted({int(item) for item in parts.get(part, [])})) or (default,)
        for part, default in zip(
            ("BYHOUR", "BYMINUTE", "BYSECOND"),
            (anchor.hour, anchor.minute, anchor.second),
            strict=True,
        )
    }
    return _Rule(
        frequency=frequency,
        interval=int(parts.get("INTERVAL", [1])[0]),
        count=int(parts["COUNT"][0]) if "COUNT" in parts else None,
        until=_until(parts.get("UNTIL"), anchor, timezone),
        week_start=_WEEKDAYS.index(str(parts.get("WKST", ["MO"])[0]).upper()),
        months=frozenset(months),
        monthdays=frozenset(monthdays),
        weekdays=frozenset(weekdays),
        ordinals=frozenset(ordinals),
        positions=tuple(sorted({int(position) for position in parts.get("BYSETPOS", [])})),
        hours=clock["BYHOUR"],
        minutes=clock["BYMINUTE"],
        seconds=clock["BYSECOND"],
    )


def _until(values: list[Any] | None, anchor: datetime, timezone: str) -> datetime | None:
    """UNTIL comparable with ``anchor``: a date means its end, a floating time local wall time."""
    if not values:
        return None
    value = values[0]
    if not isinstance(value, datetime):
        value = datetime.combine(value, time(23, 59, 59))
    aligned = _align(value, anchor, timezone)
    return aligned.astimezone(UTC) if aligned.tzinfo is not None else aligned


# --- the walk ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Walk:
    """The starts one rule gives inside the window; ``stopped`` when steps ran out first."""

    starts: tuple[datetime, ...]
    steps: int
    stopped: bool = False


def _week_start(rule: _Rule, day: date) -> date:
    return day - timedelta(days=(day.weekday() - rule.week_start) % 7)


def _period(rule: _Rule, anchor: date, index: int) -> tuple[date, date]:
    """First and last day of period ``index`` (0 holds DTSTART); raises past the calendar.

    A period is a day, a week from WKST (the first one from DTSTART on), a
    month or a year; INTERVAL periods apart.
    """
    if rule.frequency == "DAILY":
        day = anchor + timedelta(days=index * rule.interval)
        return day, day
    if rule.frequency == "WEEKLY":
        first = _week_start(rule, anchor) + timedelta(weeks=index * rule.interval)
        return (anchor if index == 0 else first), first + timedelta(days=6)
    if rule.frequency == "MONTHLY":
        year, month = divmod(anchor.year * 12 + anchor.month - 1 + index * rule.interval, 12)
        last = _month_length(year, month + 1)
        return date(year, month + 1, 1), date(year, month + 1, last)
    year = anchor.year + index * rule.interval
    return date(year, 1, 1), date(year, 12, 31)


def _period_index(rule: _Rule, anchor: date, day: date) -> int:
    """The index of the last period that starts on or before ``day`` (0 when none does)."""
    if rule.frequency == "DAILY":
        span = (day - anchor).days
    elif rule.frequency == "WEEKLY":
        span = (day - _week_start(rule, anchor)).days // 7
    elif rule.frequency == "MONTHLY":
        span = (day.year - anchor.year) * 12 + day.month - anchor.month
    else:
        span = day.year - anchor.year
    return max(0, span // rule.interval)


def _month_length(year: int, month: int) -> int:
    """Days of ``month`` in ``year`` (``_DAYS_IN_MONTH`` gives February the 29th it may have)."""
    if month != 2:
        return _DAYS_IN_MONTH[month]
    return 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28


def _accepts(rule: _Rule, day: date, first: date, last: date) -> bool:
    """Whether the filters of ``rule`` keep ``day`` of the period from ``first`` to ``last``.

    An ordinal weekday counts within the month in a monthly rule and in a
    yearly one with BYMONTH, else within the year.
    """
    if rule.months and day.month not in rule.months:
        return False
    weekday = day.weekday()
    if rule.weekdays and weekday not in rule.weekdays:
        return False
    if rule.monthdays:
        length = _month_length(day.year, day.month)
        if day.day not in rule.monthdays and day.day - length - 1 not in rule.monthdays:
            return False
    if rule.ordinals:
        if rule.frequency == "YEARLY" and rule.months:  # else the period: its month or year
            first = day.replace(day=1)
            last = day.replace(day=_month_length(day.year, day.month))
        forward, backward = (day - first).days // 7 + 1, -((last - day).days // 7 + 1)
        if (forward, weekday) not in rule.ordinals and (backward, weekday) not in rule.ordinals:
            return False
    return True


def _candidates(rule: _Rule, first: date, last: date) -> tuple[list[date], int]:
    """The days of the period ``first`` to ``last`` that ``rule`` keeps, and how many it read.

    Only the days the rule can hit are read, not every day of the period:
    in a month it reaches (BYMONTH, else every month of the period) the
    days of BYMONTHDAY, else its ordinal weekdays, else its plain weekdays
    (a yearly rule with ordinals and no BYMONTH: its ordinals of the year);
    in a week its weekdays. ``_read_rule`` gives every monthly and yearly
    rule one of these, so a yearly rule on one day costs that day, not 366.
    """
    if rule.frequency == "DAILY":
        read = [first]
    elif rule.frequency == "WEEKLY":
        read = _week_days(first, last, rule.weekdays)
    elif rule.frequency == "YEARLY" and rule.ordinals and not rule.months:
        found = (_nth_weekday(first, last, *ordinal) for ordinal in rule.ordinals)
        read = [day for day in found if day is not None]
    else:
        months = range(first.month, last.month + 1)
        read = [
            day
            for month in months
            if not rule.months or month in rule.months
            for day in _month_candidates(rule, first.year, month)
        ]
    days = {day for day in read if first <= day <= last and _accepts(rule, day, first, last)}
    return sorted(days), len(read)


def _week_days(first: date, last: date, weekdays: frozenset[int]) -> list[date]:
    """The days of ``weekdays`` from ``first`` to ``last`` (at most a week)."""
    numbers = (first.toordinal() + (weekday - first.weekday()) % 7 for weekday in weekdays)
    return [date.fromordinal(number) for number in numbers if number <= last.toordinal()]


def _month_candidates(rule: _Rule, year: int, month: int) -> list[date]:
    """The days of ``month`` that the day filters of ``rule`` name (see ``_candidates``)."""
    length = _month_length(year, month)
    first, last = date(year, month, 1), date(year, month, length)
    if rule.monthdays:
        numbers = {day if day > 0 else length + day + 1 for day in rule.monthdays}
        return [date(year, month, day) for day in sorted(numbers) if 1 <= day <= length]
    if rule.ordinals:
        found = (_nth_weekday(first, last, *ordinal) for ordinal in rule.ordinals)
        return [day for day in found if day is not None]
    weekdays = rule.weekdays or frozenset(range(7))
    begin = first.weekday()
    return [
        date(year, month, day)
        for weekday in weekdays
        for day in range(1 + (weekday - begin) % 7, length + 1, 7)
    ]


def _nth_weekday(first: date, last: date, ordinal: int, weekday: int) -> date | None:
    """The ``ordinal``-th ``weekday`` of ``first`` to ``last`` (counted back when negative)."""
    if ordinal > 0:
        number = first.toordinal() + (weekday - first.weekday()) % 7 + 7 * (ordinal - 1)
    else:
        number = last.toordinal() - (last.weekday() - weekday) % 7 - 7 * (-ordinal - 1)
    return date.fromordinal(number) if first.toordinal() <= number <= last.toordinal() else None


def _moments(
    rule: _Rule, days: list[date], zone: tzinfo | None, since: datetime | None
) -> Iterator[datetime]:
    """The candidates of one period in order: every time of every day, or the BYSETPOS ones.

    Without BYSETPOS the times before ``since`` (a wall time, if given) are
    left out without being built: whole days, and the times of its own day
    found by bisection (the times of a day are in order).
    """
    per_day = rule.times_per_day
    if rule.positions:
        size = len(days) * per_day
        indexes = {position - 1 if position > 0 else size + position for position in rule.positions}
        for index in sorted(index for index in indexes if 0 <= index < size):
            yield datetime.combine(days[index // per_day], rule.time_at(index % per_day), zone)
        return
    for day in days:
        begin = 0
        if since is not None and day <= since.date():
            if day < since.date():
                continue
            begin = bisect_left(range(per_day), since.time(), key=rule.time_at)
        for index in range(begin, per_day):
            yield datetime.combine(day, rule.time_at(index), zone)


def _walk(rule: _Rule, anchor: datetime, low: datetime, high: datetime, *, limit: int) -> _Walk:
    """The starts of ``rule`` in ``[low, high)``, in at most about ``limit`` steps.

    The periods walked end with the one that holds the effective end (UNTIL
    or ``high``, the earlier); a rule without COUNT starts with the period
    before ``low`` and leaves out the times of day before ``low`` without
    steps (``_skip_before``); one with COUNT starts with the period of
    DTSTART and counts every occurrence from there.
    """
    if rule.count is not None and rule.count < 1:
        return _Walk((), 0)
    end = high if rule.until is None else min(high, rule.until)
    last_day, start_day = _wall(end, anchor).date(), _wall(low, anchor).date() - _ONE_DAY
    index = 0 if rule.count is not None else _period_index(rule, anchor.date(), start_day)
    since = None if rule.count is not None else _skip_before(low, anchor)
    starts: list[datetime] = []
    steps = taken = 0
    while True:
        try:
            first, last = _period(rule, anchor.date(), index)
        except (ValueError, OverflowError):  # past the end of the calendar
            return _Walk(tuple(starts), steps)
        if first > last_day:
            return _Walk(tuple(starts), steps)
        if steps >= limit:
            return _Walk(tuple(starts), steps, stopped=True)
        days, read = _candidates(rule, first, last)
        steps += max(1, read) + len(rule.positions)
        for moment in _moments(rule, days, anchor.tzinfo, since):
            if steps >= limit:
                return _Walk(tuple(starts), steps, stopped=True)
            steps += 1
            if moment >= high or (rule.until is not None and moment > rule.until):
                return _Walk(tuple(starts), steps)
            if moment < anchor:
                continue
            taken += 1
            if moment >= low:
                starts.append(moment)
            if taken == rule.count or len(starts) >= MAX_OCCURRENCES:
                return _Walk(tuple(starts), steps)
        index += 1


def _skip_before(low: datetime, anchor: datetime) -> datetime | None:
    """A wall time before which no time of day on ``anchor``'s clock reaches ``low``.

    Daylight saving moves the wall clock by less than ``_MARGIN``, so a
    time that much before ``low`` lies before it. Where the UTC offset is
    the same ``_MARGIN`` before and after ``low`` (always on a floating
    clock or in UTC; no zone changes it twice within hours), the wall times
    there are in the order of their moments, so ``low`` itself is the
    bound; next to a change (a skipped or repeated hour) it is ``_MARGIN``
    earlier, and the walk compares each time it builds. ``None`` (skip
    nothing) at an end of the calendar.
    """
    wall, zone = _wall(low, anchor), anchor.tzinfo
    try:
        early, late = wall - _MARGIN, wall + _MARGIN
    except OverflowError:
        return None
    if zone is None or zone.utcoffset(early) == zone.utcoffset(late):
        return wall
    return early


def _wall(moment: datetime, anchor: datetime) -> datetime:
    """``moment`` on the wall clock of ``anchor``, naive."""
    if anchor.tzinfo is None or moment.tzinfo is None:
        return moment.replace(tzinfo=None)
    return moment.astimezone(anchor.tzinfo).replace(tzinfo=None)


# --- expansion -----------------------------------------------------------------------------


@dataclass(frozen=True)
class RecurrenceProperties:
    """The recurrence of a VEVENT as its properties give it.

    ``rules`` are the values of its RRULE properties (``icalendar.vRecur``
    when they could be read), ``rdates`` and ``exdates`` the dates and
    date-times of its RDATE and EXDATE properties.
    """

    rules: tuple[object, ...] = ()
    rdates: tuple[Moment, ...] = ()
    exdates: tuple[Moment, ...] = ()


@dataclass(frozen=True)
class Allowance:
    """What a feed has left for its expansions: steps and RRULE lines."""

    steps: int
    rules: int


@dataclass(frozen=True)
class Expansion:
    """The starts that the recurrence of a VEVENT gives inside the window, and their cost.

    ``rules`` counts its RRULE lines that were walked (all but those whose
    UNTIL lies before the window), ``steps`` the steps they took.
    ``exceeded`` names the allowance of the feed that the expansion needed
    more of; it stopped there.
    """

    starts: tuple[Moment, ...] = ()
    steps: int = 0
    rules: int = 0
    exceeded: Limit | None = None


def expand(
    recurrence: RecurrenceProperties,
    start: Moment,
    window: TimeWindow,
    timezone: str,
    *,
    allowance: Allowance,
) -> Expansion:
    """The starts of the occurrences of a recurring VEVENT that start inside ``window``.

    ``start`` is its DTSTART, which is always an occurrence, and ``timezone``
    the zone of floating times. Each RRULE is walked as the module docstring
    says; RDATEs are added and EXDATEs taken out. Raises ``ValueError``
    (``TypeError``, ``OverflowError``) when a rule is refused
    (``check_rule``) or cannot be read; then no rule is walked.
    """
    is_date = not isinstance(start, datetime)
    anchor = start if isinstance(start, datetime) else datetime.combine(start, time.min)
    anchor = anchor.replace(microsecond=0)
    rules = [_read_rule(value, anchor, timezone) for value in recurrence.rules]
    low, high = _window_bounds(window, anchor, timezone)
    found: set[datetime] = set()
    steps = walked = 0
    for rule in rules:
        if rule.until is not None and rule.until < low:
            continue  # ends before the window
        walked += 1
        if walked > allowance.rules:
            return Expansion(steps=steps, rules=walked, exceeded="rules")
        left = allowance.steps - steps
        walk = _walk(rule, anchor, low, high, limit=max(0, min(MAX_EXPANSION_STEPS, left)))
        steps += walk.steps
        found.update(walk.starts)
        if walk.stopped and left <= MAX_EXPANSION_STEPS:
            return Expansion(steps=steps, rules=walked, exceeded="steps")
    extra = (anchor, *(_align(moment, anchor, timezone) for moment in recurrence.rdates))
    found.update(moment for moment in extra if low <= moment < high)
    excluded = {_align(moment, anchor, timezone) for moment in recurrence.exdates}
    starts = sorted(found - excluded)[:MAX_OCCURRENCES]
    moments = tuple(moment.date() if is_date else moment for moment in starts)
    return Expansion(moments, steps, walked)


def _local_naive(moment: datetime, timezone: str) -> datetime:
    return moment.astimezone(ZoneInfo(timezone)).replace(tzinfo=None)


def _align(value: Moment, anchor: datetime, timezone: str) -> datetime:
    """``value`` as a datetime comparable with ``anchor`` (both aware or both naive)."""
    if not isinstance(value, datetime):
        value = datetime.combine(value, anchor.time())
    if anchor.tzinfo is None:
        return value if value.tzinfo is None else _local_naive(value, timezone)
    return value.replace(tzinfo=anchor.tzinfo) if value.tzinfo is None else value


def _window_bounds(
    window: TimeWindow, anchor: datetime, timezone: str
) -> tuple[datetime, datetime]:
    if anchor.tzinfo is not None:
        return window.start, window.end
    return _local_naive(window.start, timezone), _local_naive(window.end, timezone)


def starts_in(start: Moment, window: TimeWindow, timezone: str) -> bool:
    """Whether ``start`` lies in ``window``; a floating one is wall time in ``timezone``."""
    anchor = start if isinstance(start, datetime) else datetime.combine(start, time.min)
    low, high = _window_bounds(window, anchor, timezone)
    return low <= anchor < high
