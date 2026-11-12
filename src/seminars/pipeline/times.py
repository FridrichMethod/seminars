"""Times of normalization: from what a draft says to the start and end of an ``Event``.

``EventDraft`` fixes what each kind of value means (an aware datetime, a
naive one, a date, a string). This module applies those rules:

* a naive time is wall time in the time zone of the draft or of its
  source. A time that does not exist on the day the clocks go forward
  (02:30 on 2027-03-14 in America/Los_Angeles) moves forward by the gap,
  and a time that occurs twice on the day they go back (01:30 on
  2026-11-01) is the first of the two;
* every result is wall time of the project time zone with a fixed numeric
  offset, which is how data/events.json stores it;
* a date without a time of day makes an all-day event that starts at local
  midnight of the project time zone, whatever zone the source is in; its
  inclusive last day becomes the exclusive end of ``Event``;
* an end that is not later than the start is not kept. One case is
  repaired first: an end on the same day as the start but earlier, which
  is what a source produces when it puts an end time next to a start date
  for a talk that runs past midnight. It moves to the next day when the
  talk is then at most six hours long (``seminars.models.MAX_ROLLOVER``).

Nothing in here reads the clock. ``reference`` supplies the year for a
string that has none: the year that puts the date closest to it, so that
"Jan 15" read in December is next January, not the one before.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from typing import Final
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser

from seminars.models import MAX_ROLLOVER

MAX_TEXT_LENGTH: Final = 120
_PROBE_DEFAULTS: Final = (datetime(2004, 1, 1, 0, 0), datetime(2008, 3, 2, 11, 0))
"""Two defaults that differ in every field: what a text leaves out differs between them.

Both years are leap years, so "Feb 29" without a year can be read, and
both days exist in every month.
"""
_ONE_DAY: Final = timedelta(days=1)
_ZONE_ABBREVIATIONS: Final[dict[str, str]] = {
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "PT": "America/Los_Angeles",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "MT": "America/Denver",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "CT": "America/Chicago",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "ET": "America/New_York",
    "UTC": "UTC",
    "GMT": "UTC",
    "Z": "UTC",
}
_TZINFOS: Final[dict[str, tzinfo]] = {
    name: ZoneInfo(zone) for name, zone in _ZONE_ABBREVIATIONS.items()
}

Moment = datetime | date | str


class TimeError(ValueError):
    """A draft time cannot be understood."""


@dataclass(frozen=True)
class EventTimes:
    """Start and end in the form ``Event`` stores them."""

    start: datetime
    end: datetime | None
    all_day: bool


def _parse(text: str, default: datetime) -> datetime:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return date_parser.parse(text, default=default, tzinfos=_TZINFOS)


def _nearest_year(moment: datetime, reference: datetime) -> datetime:
    """``moment`` in the year next to ``reference`` that puts it closest to ``reference``."""
    day = reference.date()
    options = []
    for year in (day.year - 1, day.year, day.year + 1):
        try:
            options.append(moment.replace(year=year))
        except ValueError:  # February 29 in a year that has none
            continue
    if not options:
        raise TimeError("the date does not exist in the years around the reference")
    return min(options, key=lambda option: (abs((option.date() - day).days), option.year))


def parse_moment(text: str, *, reference: datetime) -> datetime | date:
    """A free-form time such as "Nov 17, 2026 4:00 PM" as a datetime, or a date without one.

    A missing year is the one that puts the date closest to ``reference``
    (the year before, of, or after it); a text without month and day, such
    as "4pm" or "Thursday", is an error. Common North American zone
    abbreviations are understood; an unknown one is ignored and the time is
    then taken as wall time of the source.
    """
    if len(text) > MAX_TEXT_LENGTH:
        raise TimeError("time text is too long")
    try:
        first, other = (_parse(text, default) for default in _PROBE_DEFAULTS)
    except (ValueError, OverflowError, TypeError) as error:
        raise TimeError("time text cannot be parsed") from error
    if (first.month, first.day) != (other.month, other.day):
        raise TimeError("time text has no day")
    moment = first if first.year == other.year else _nearest_year(first, reference)
    return moment.date() if first.time() != other.time() else moment  # no time of day: a date


def _typed(moment: Moment, reference: datetime) -> datetime | date:
    return parse_moment(moment, reference=reference) if isinstance(moment, str) else moment


def _instant(moment: datetime, zone: ZoneInfo) -> datetime:
    """``moment`` as an instant in UTC; a naive one is wall time in ``zone``."""
    aware = moment if moment.utcoffset() is not None else moment.replace(tzinfo=zone)
    return aware.astimezone(UTC)


def _wall(instant: datetime, zone: ZoneInfo) -> datetime:
    """``instant`` as wall time of ``zone`` with a fixed offset, whole seconds."""
    local = instant.astimezone(zone).replace(microsecond=0)
    return local.replace(tzinfo=timezone(local.utcoffset() or timedelta(0)))


def _midnight(day: date, zone: ZoneInfo) -> datetime:
    return _wall(_instant(datetime.combine(day, time.min), zone), zone)


def _day_of(moment: datetime | date, zone: ZoneInfo) -> date:
    """The calendar day of ``moment`` for a source in ``zone``."""
    if not isinstance(moment, datetime):
        return moment
    return (moment if moment.utcoffset() is None else moment.astimezone(zone)).date()


def _last_day(end: datetime | date, zone: ZoneInfo) -> date:
    """The inclusive last day an all-day draft states with ``end``."""
    if not isinstance(end, datetime):
        return end
    return _day_of(end - timedelta(microseconds=1), zone)


def _all_day(
    start: datetime | date, end: datetime | date | None, source: ZoneInfo, project: ZoneInfo
) -> EventTimes:
    first = _day_of(start, source)
    last = first if end is None else _last_day(end, source)
    until = _midnight(last + _ONE_DAY, project) if last > first else None
    return EventTimes(start=_midnight(first, project), end=until, all_day=True)


def _timed_end(
    start: datetime, end: datetime | date | None, source: ZoneInfo, project: ZoneInfo
) -> datetime | None:
    if end is None:
        return None
    if not isinstance(end, datetime):
        until = _midnight(end + _ONE_DAY, project)
        return until if until > start else None
    until = _wall(_instant(end, source), project)
    if until > start:
        return until
    rolled = until + _ONE_DAY
    same_day = until.date() == start.date()
    return rolled if same_day and start < rolled <= start + MAX_ROLLOVER else None


def resolve_times(
    start: Moment,
    end: Moment | None,
    *,
    all_day: bool,
    source_zone: str,
    project_zone: str,
    reference: datetime,
) -> EventTimes:
    """Start, end and the all-day flag of an event; ``TimeError`` when the start is unusable.

    ``all_day`` is the flag of the draft; a start without a time of day
    makes the event all-day as well. An end that cannot be understood is
    dropped, it never makes the event invalid.
    """
    source, project = ZoneInfo(source_zone), ZoneInfo(project_zone)
    first = _typed(start, reference)
    try:
        last = None if end is None else _typed(end, reference)
    except TimeError:
        last = None
    try:
        if all_day or not isinstance(first, datetime):
            return _all_day(first, last, source, project)
        begin = _wall(_instant(first, source), project)
        return EventTimes(start=begin, end=_timed_end(begin, last, source, project), all_day=False)
    except (OverflowError, ValueError) as error:  # dates at the edge of what datetime can hold
        raise TimeError("time is out of range") from error
