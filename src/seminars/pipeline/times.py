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
  talk is then at most six hours long (``seminars.models.MAX_ROLLOVER``);
* the end of a timed event is not kept either when it is at the same wall
  time as the start on a later day (a seminar at noon that the campus
  calendar lists until noon of the next day: the date of the end is a slip
  of whoever entered it), or when it is more than ``MAX_TIMED_LENGTH``
  after the start. The day is compared in wall time, so the rule holds
  across the clock changes, when the same time of day is 23 or 25 hours
  later. A timed event over several days with other times of day ("9:00 on
  Monday to 17:00 on Wednesday") keeps its end, and so does a span of
  whole days written from midnight to midnight. All-day events are not
  affected. ``EventTimes.end_dropped_as_too_long`` tells normalization
  that this happened; the feeds then give the talk their default length.

Nothing in here reads the clock. ``reference`` supplies the year for a
string that has none: the year that puts the date closest to it, so that
"Jan 15" read in December is next January, not the one before. The time
zones that a string can name are in ``parse_moment``; "AoE" and an offset
such as "UTC-8" are read before ``dateutil`` sees the text, because it
does not know the one and reads the sign of the other the POSIX way
("UTC-8" as eight hours ahead of UTC).
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from typing import Final
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser

from seminars.models import MAX_ROLLOVER

MAX_TEXT_LENGTH: Final = 120
MAX_TIMED_LENGTH: Final = timedelta(days=7)
"""The longest that an event with times of day may last; a later end is not kept."""
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
_AOE: Final = timedelta(hours=-12)
"""Anywhere on Earth: UTC-12 all year (the IANA zone ``Etc/GMT+12``, with the POSIX sign)."""
_OFFSET_LIMITS: Final = (timedelta(hours=-12), timedelta(hours=14))
"""The offsets from UTC that time zones have, from Baker Island to Kiribati."""
_SIGN: Final = r"[+\-\u2212]"
_HOURS: Final = r"(?:[0-9]{4}|[0-9]{1,2}(?::[0-9]{2})?)(?![0-9:]|\.[0-9])"
"""Hours of an offset: "8", "08:00", "0800"; never the start of a longer number."""
_WRITTEN_ZONE: Final = (
    r"(?<![a-z])(?<!etc/)"  # "Etc/GMT+12" is an IANA name, not an offset
    r"(?:aoe|anywhere[\s-]+on[\s-]+earth"
    rf"|(?:utc|gmt)\s*{_SIGN}{_HOURS}(?!\s*[ap]\.?m\b))"  # "4 PM GMT-5 PM" is a range
    r"(?![a-z0-9])"
)
_ZONE: Final = re.compile(_WRITTEN_ZONE, re.IGNORECASE)
_ZONES: Final = re.compile(
    rf"\(\s*{_WRITTEN_ZONE}(?:\s*[,/;]?\s*{_WRITTEN_ZONE})*\s*\)|{_WRITTEN_ZONE}", re.IGNORECASE
)
"""One zone of ``_ZONE``, or several in parentheses: "AoE", "(Anywhere on Earth, UTC-12)"."""
_UNREAD_OFFSET: Final = re.compile(
    rf"(?<![a-z])(?<!etc/)(?:utc|gmt)\s*{_SIGN}\s*[0-9]", re.IGNORECASE
)
"""An offset that ``_ZONE`` does not read; dateutil would give it a sign of its own."""
_OFFSET: Final = re.compile(
    rf"(?P<sign>{_SIGN})(?:(?P<hhmm>[0-9]{{4}})|(?P<hours>[0-9]{{1,2}})(?::(?P<minutes>[0-9]{{2}}))?)"
)
"""The sign, the hours and the minutes of an offset that ``_ZONE`` matched."""
_NAME_IN_PARENTHESES: Final = re.compile(r"\(\s*([A-Z]{2,5})\s*\)")
"""A name in parentheses, "(PST)" of "UTC-8 (PST)": dateutil reads one only after a number."""

Moment = datetime | date | str


class TimeError(ValueError):
    """A draft time cannot be understood."""


@dataclass(frozen=True)
class EventTimes:
    """Start and end in the form ``Event`` stores them."""

    start: datetime
    end: datetime | None
    all_day: bool
    end_dropped_as_too_long: bool = False
    """The draft had an end after the start that was dropped by the length rule."""


def _parse(text: str, default: datetime) -> datetime:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return date_parser.parse(text, default=default, tzinfos=_TZINFOS)


def _offset(zone: str) -> timedelta:
    """The offset from UTC of a match of ``_ZONE``: "AoE", "UTC-8", "GMT+0530"."""
    found = _OFFSET.search(zone)
    if zone[:1].lower() == "a" or found is None:  # AoE, Anywhere on Earth
        return _AOE
    digits = found["hhmm"]
    hours, minutes = (digits[:2], digits[2:]) if digits else (found["hours"], found["minutes"])
    offset = timedelta(hours=int(hours), minutes=int(minutes or 0))
    offset = offset if found["sign"] == "+" else -offset
    low, high = _OFFSET_LIMITS
    if int(minutes or 0) > 59 or not low <= offset <= high:
        raise TimeError("time text has an offset from UTC that no time zone has")
    return offset


def _written_offset(text: str) -> tuple[str, timedelta | None]:
    """``text`` without its AoE and UTC offsets, and the offset from UTC that they name."""
    offsets = {
        _offset(zone[0]) for found in _ZONES.finditer(text) for zone in _ZONE.finditer(found[0])
    }
    rest = _ZONES.sub(" ", text)
    if offsets:  # the name that followed the offset stays, so that both are compared
        rest = _NAME_IN_PARENTHESES.sub(r" \1 ", rest)
    if _UNREAD_OFFSET.search(rest):  # "UTC - 8", "UTC+530": dateutil would guess the sign
        raise TimeError("time text has an offset from UTC that cannot be read")
    if len(offsets) > 1:
        raise TimeError("time text names different offsets from UTC")
    return rest, next(iter(offsets), None)


def _at_offset(moment: datetime, offset: timedelta) -> datetime:
    """``moment`` at ``offset`` from UTC; an error when its text names another zone too."""
    try:
        named = moment.utcoffset()
    except ValueError as error:  # an offset of dateutil of a day or more
        raise TimeError("time text has an offset from UTC that no time zone has") from error
    if named is not None and named != offset:
        raise TimeError("time text names two different time zones")
    return moment.replace(tzinfo=timezone(offset))


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
    as "4pm" or "Thursday", is an error. The time zone a text names:

    * a North American abbreviation ("PST", "ET", the keys of
      ``_ZONE_ABBREVIATIONS``) is the time zone of its region in every
      season, so "PST" in July is Pacific daylight time;
    * an offset from UTC is read as people write it, from UTC-12 to UTC+14:
      "UTC-8", "UTC -8" and "GMT-08:00" are eight hours behind UTC,
      "UTC+5:30" and "GMT+0530" five and a half hours ahead. One that
      cannot be read that way ("UTC - 8", "UTC+530") is an error, and so is
      an offset that no time zone has ("UTC-13");
    * "AoE" and "Anywhere on Earth", in any case, in parentheses or not,
      are UTC-12.

    A zone written twice must say the same ("23:59 AoE (UTC-12)", "4 PM PST
    (UTC-8)", "4 PM UTC-8 (PST)"); two different ones are an error. An unknown abbreviation is
    ignored and the time is then taken as wall time of the source. A text
    without a time of day is a date in any zone: "Jan 29, 2027 AoE" is
    January 29.
    """
    if len(text) > MAX_TEXT_LENGTH:
        raise TimeError("time text is too long")
    rest, offset = _written_offset(text)
    try:
        first, other = (_parse(rest, default) for default in _PROBE_DEFAULTS)
    except (ValueError, OverflowError, TypeError) as error:
        raise TimeError("time text cannot be parsed") from error
    if (first.month, first.day) != (other.month, other.day):
        raise TimeError("time text has no day")
    moment = first if first.year == other.year else _nearest_year(first, reference)
    if first.time() != other.time():  # no time of day: a date
        return moment.date()
    return moment if offset is None else _at_offset(moment, offset)


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


def _too_long(start: datetime, end: datetime) -> bool:
    """Whether a timed event from ``start`` to ``end`` (both later) breaks the length rule."""
    if end - start > MAX_TIMED_LENGTH:
        return True
    whole_days = end.date() > start.date() and end.time() == start.time()
    return whole_days and start.time() != time.min


def _timed_end(
    start: datetime, end: datetime | date | None, source: ZoneInfo, project: ZoneInfo
) -> datetime | None:
    """The end of a timed event; ``None`` when there is none or it cannot be right."""
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


def _timed(
    start: datetime, end: datetime | date | None, source: ZoneInfo, project: ZoneInfo
) -> EventTimes:
    begin = _wall(_instant(start, source), project)
    until = _timed_end(begin, end, source, project)
    if until is not None and _too_long(begin, until):
        return EventTimes(start=begin, end=None, all_day=False, end_dropped_as_too_long=True)
    return EventTimes(start=begin, end=until, all_day=False)


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
        return _timed(first, last, source, project)
    except (OverflowError, ValueError) as error:  # dates at the edge of what datetime can hold
        raise TimeError("time is out of range") from error
