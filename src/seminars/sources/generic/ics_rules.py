"""Recurrence rules of iCalendar events: which ones are supported, and their expansion.

The ``ics`` adapter expands recurring VEVENTs with the recurrence library
(dateutil) inside the run window. When the filters of a rule never match,
that library steps through every period until the year 9999, whatever
UNTIL or COUNT say, before it returns. So a rule is checked first
(``check_rule``), and the expansion of one event is capped
(``MAX_OCCURRENCES``, ``MAX_EXPANSION_STEPS`` and the steps the feed has
left). The caps of a whole feed are the adapter's
(``seminars.sources.generic.ics``). This module registers no adapter.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any, Final, cast
from zoneinfo import ZoneInfo

import icalendar
from dateutil.rrule import rrule, rruleset, rrulestr

from seminars.models import TimeWindow

MAX_OCCURRENCES: Final = 500
"""Most occurrences of one recurring event inside the window."""

MAX_EXPANSION_STEPS: Final = 10_000
"""Most steps through the recurrence of one event, counted from its DTSTART."""

Moment = date | datetime

_UNSUPPORTED_RULE_PARTS: Final = frozenset({"BYYEARDAY", "BYWEEKNO", "BYEASTER"})
_FILTER_PARTS: Final = frozenset({"BYMONTH", "BYMONTHDAY", "BYSETPOS"})
_DAYS_IN_MONTH: Final = {
    month: 29 if month == 2 else 30 if month in (4, 6, 9, 11) else 31 for month in range(1, 13)
}
_WEEKDAYS: Final = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


# --- the guard -----------------------------------------------------------------------------


def check_rule(parts: dict[str, list[Any]], start: date | None = None) -> None:
    """Refuse a recurrence rule that the expansion does not support (``ValueError``).

    When its filters never match, the recurrence library steps through every
    period until the year 9999 whatever UNTIL or COUNT say (1 to 3 us per
    period: up to 3 s for a daily rule). So only what calendars of talks use
    is accepted: ``FREQ`` ``DAILY``, ``WEEKLY``, ``MONTHLY`` or ``YEARLY``;
    no ``BYYEARDAY``, ``BYWEEKNO``, ``BYEASTER``; days and months that exist
    (``_check_calendar_days``); a ``BYDAY`` of weekdays or of ordinals, not
    both (``_check_ordinals``); ordinals and ``BYSETPOS`` only where a
    period has them (``_set_size_bound``); and, with ``start`` (the
    DTSTART), an ``INTERVAL`` and days that land on periods the filters
    accept (``_check_from_start``). What still passes without ever matching
    is a BYSETPOS beyond the days that BYMONTHDAY and BYDAY have in common:
    in random sweeps of 24000 rules every accepted rule without occurrence
    that took more than 0.04 s was of that kind, the slowest took 0.3 s (a
    monthly one), and ``_filters`` counts them, so a feed has at most
    ``ics.MAX_FILTERED_EVENTS`` of them (about 30 s).
    """
    keys = {str(key).upper(): value for key, value in parts.items()}
    frequency = [str(value).upper() for value in keys.get("FREQ", [])]
    if frequency not in (["DAILY"], ["WEEKLY"], ["MONTHLY"], ["YEARLY"]):
        raise ValueError(f"unsupported recurrence frequency {frequency}")
    unsupported = sorted(set(keys) & _UNSUPPORTED_RULE_PARTS)
    if unsupported:
        raise ValueError(f"unsupported recurrence rule part(s) {', '.join(unsupported)}")
    _check_calendar_days(keys, frequency[0])
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


def _weekdays(keys: dict[str, list[Any]]) -> tuple[set[str], list[int]]:
    """The BYDAY weekdays without an ordinal (``MO``), and the ordinals of the others (abs)."""
    plain: set[str] = set()
    ordinals: list[int] = []
    for day in (str(value).upper() for value in keys.get("BYDAY", [])):
        if day[:-2]:
            ordinals.append(abs(int(day[:-2])))
        else:
            plain.add(day)
    return plain, ordinals


def _check_ordinals(keys: dict[str, list[Any]], frequency: str) -> None:
    """BYDAY with an ordinal (``2MO``, ``-1FR``) only where a period has that many weekdays.

    A BYDAY that mixes weekdays with and without an ordinal (``TH,1FR``) is
    refused: RFC 5545 takes the union of the two kinds, the recurrence
    library requires a day to be of both, which no day is when the weekdays
    differ, so it would step through every period until the year 9999.
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
    if max(ordinals) > limit:
        raise ValueError("BYDAY names an ordinal that no period has")


def _check_positions(keys: dict[str, list[Any]], frequency: str, start: date | None) -> None:
    """BYSETPOS only in a monthly or yearly rule, and only for positions a period can have."""
    if "BYSETPOS" not in keys:
        return
    if frequency not in ("MONTHLY", "YEARLY"):
        raise ValueError("BYSETPOS is only supported with a monthly or yearly rule")
    largest = max(abs(int(position)) for position in keys["BYSETPOS"])
    day = None if start is None else start.day
    if largest > _set_size_bound(keys, yearly=frequency == "YEARLY", day=day):
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
class Expansion:
    """The starts that the recurrence of a VEVENT gives inside the window, and their cost.

    ``expanded`` is true when the recurrence was stepped through (its rules
    do not all end before the window), ``filtered`` when a rule of the event
    filters (``_filters``), ``exhausted`` when the expansion stopped because
    it needed more steps than its budget.
    """

    starts: tuple[Moment, ...] = ()
    steps: int = 0
    expanded: bool = False
    filtered: bool = False
    exhausted: bool = False


@dataclass(frozen=True)
class _Recurrence:
    """The rule set of a recurring VEVENT.

    ``last`` is the latest start it can give, known (else ``None``) when
    every RRULE has an UNTIL: the latest of those, of the RDATEs and of
    DTSTART. ``filtered`` tells whether one of its rules filters.
    """

    rules: rruleset
    last: datetime | None
    filtered: bool


def _filters(parts: dict[str, list[Any]]) -> bool:
    """Whether an accepted rule may step through many periods without an occurrence.

    It may when it filters by month, day of the month or set position, or
    asks for the fifth (or a later) weekday of a period. Every other rule
    that ``check_rule`` accepts has an occurrence within a dozen periods or
    so: its BYDAY is plain weekdays or ordinals up to 4, never a mix of the
    two, and without BYDAY it repeats the day of DTSTART, which comes back
    (the 31st in seven months of twelve, the 29th of February in leap
    years). So its cost is in its steps.
    """
    keys = {str(key).upper(): value for key, value in parts.items()}
    _, ordinals = _weekdays(keys)
    return bool(set(keys) & _FILTER_PARTS) or any(ordinal >= 5 for ordinal in ordinals)


def _rule_set(recurrence: RecurrenceProperties, anchor: datetime, timezone: str) -> _Recurrence:
    """The rule set of a recurring VEVENT whose DTSTART is ``anchor``."""
    rules = rruleset()
    lasts: list[datetime | None] = [anchor]
    filtered = False
    for value in recurrence.rules:
        text, until = _rule_text(value, anchor, timezone)
        # The text of one RRULE always gives a single rule, never a set.
        rules.rrule(cast(rrule, rrulestr(text, dtstart=anchor)))
        lasts.append(until)
        filtered = filtered or _filters(dict(cast(icalendar.vRecur, value)))
    rules.rdate(anchor)
    for moment in recurrence.rdates:
        aligned = _align(moment, anchor, timezone)
        rules.rdate(aligned)
        lasts.append(aligned)
    for moment in recurrence.exdates:
        rules.exdate(_align(moment, anchor, timezone))
    bounded = [moment for moment in lasts if moment is not None]
    last = max(bounded) if len(bounded) == len(lasts) else None
    return _Recurrence(rules, last, filtered)


def expand(
    recurrence: RecurrenceProperties,
    start: Moment,
    window: TimeWindow,
    timezone: str,
    *,
    budget: int,
) -> Expansion:
    """The starts of the occurrences of a recurring VEVENT that start inside ``window``.

    ``start`` is its DTSTART and ``timezone`` the zone of floating times.
    Every occurrence from DTSTART on is one step; when the expansion needs
    more than the ``budget`` steps that the feed has left it stops and is
    ``exhausted``. Raises ``ValueError`` (``TypeError``, ``OverflowError``)
    when a rule is refused (``check_rule``) or cannot be read.
    """
    is_date = not isinstance(start, datetime)
    anchor = start if isinstance(start, datetime) else datetime.combine(start, time.min)
    rule_set = _rule_set(recurrence, anchor, timezone)
    low, high = _window_bounds(window, anchor, timezone)
    if rule_set.last is not None and rule_set.last < low:
        return Expansion()
    starts: list[Moment] = []
    steps = 0
    for moment in rule_set.rules:
        if moment >= high or len(starts) >= MAX_OCCURRENCES or steps >= MAX_EXPANSION_STEPS:
            break
        if steps >= budget:
            return Expansion(tuple(starts), steps, expanded=True, exhausted=True)
        steps += 1
        if moment >= low:
            starts.append(moment.date() if is_date else moment)
    return Expansion(tuple(starts), steps, expanded=True, filtered=rule_set.filtered)


def _rule_text(rule: object, anchor: datetime, timezone: str) -> tuple[str, datetime | None]:
    """The RRULE as text for dateutil, and its UNTIL comparable with ``anchor`` (if any)."""
    if not isinstance(rule, icalendar.vRecur):
        raise ValueError("the RRULE cannot be read")
    parts = dict(rule)
    check_rule(parts, start=anchor)
    until = parts.get("UNTIL")
    if not until:
        return icalendar.vRecur(parts).to_ical().decode("ascii"), None
    value = until[0]
    if not isinstance(value, datetime):
        value = datetime.combine(value, time(23, 59, 59))
    aligned = _align(value, anchor, timezone)
    parts["UNTIL"] = [aligned.astimezone(UTC) if aligned.tzinfo is not None else aligned]
    return icalendar.vRecur(parts).to_ical().decode("ascii"), aligned


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
