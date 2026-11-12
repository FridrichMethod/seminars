"""The ``ics`` adapter: iCalendar feeds (series feeds, category exports, other .ics files).

``fetch`` requests every URL of the source (``fetch_urls``); a feed has no
pages. ``parse`` reads each answer with the ``icalendar`` library and turns
every VEVENT, or every occurrence of a recurring one, into a draft. A body
that is not iCalendar at all is a ``ParseError``; a single VEVENT that
cannot be used (no DTSTART, a broken value, an invalid recurrence rule) is
skipped. Events of a single occurrence are returned whatever their date,
so that a feed whose talks all lie in the past still counts as read (the
pipeline applies the run window).

Times, as the draft conventions want them:

* ``DTSTART`` / ``DTEND`` with ``TZID`` (a known zone or one defined by a
  VTIMEZONE of the feed) and UTC values (``...Z``) become timezone-aware
  datetimes. Floating values stay naive: they are wall time in the
  calendar's ``X-WR-TIMEZONE`` when that names a known zone (it becomes the
  draft's ``timezone``), else in the source's timezone. A ``TZID`` that
  names no known zone leaves the value floating.
* ``VALUE=DATE`` makes an all-day event. ``DTEND`` of a date is the day
  after the last day, so the draft's ``end`` is ``DTEND`` minus one day (no
  ``end`` for a single day). ``DURATION`` works like ``DTEND``.
* ``DTEND`` equal to ``DTSTART`` means no end time, and so does a date as
  ``DTEND`` of an event that starts at a time of day.
* Recurring events (``RRULE``, ``RDATE``, ``EXDATE``) are expanded inside
  the run window only (occurrences that start in it), at most
  ``MAX_OCCURRENCES`` per event and after at most ``MAX_EXPANSION_STEPS``
  steps of the rule from ``DTSTART``; a rule the expansion cannot bound is
  skipped (``check_rule``). An event whose rules all end (``UNTIL``) before
  the window is not expanded. A feed with more than
  ``MAX_RECURRING_EVENTS`` other recurring events, or whose recurrences
  need more than ``MAX_FEED_STEPS`` steps in all, is a ``ParseError``
  rather than a part of the feed, whose missing events would count as
  gone. An occurrence keeps the duration of the first one. A
  VEVENT with ``RECURRENCE-ID`` replaces the occurrence it names; one whose
  occurrence is not generated (it was moved into the window, or the rule is
  gone) stands on its own when it starts in the window. The override of
  an event without recurrence (its RECURRENCE-ID is the event's DTSTART)
  replaces the event and keeps its native id. An override without
  ``DTSTART`` keeps the original start (and end, unless it has its own);
  one whose times cannot be read is ignored, so the occurrence stays as the
  rule gives it and the rest of the series is not affected.

Fields of a draft, without parameters: ``native_id`` = ``UID`` (an
occurrence of a recurring event: ``<UID>/<RECURRENCE-ID>``, the original
start as ``YYYYMMDD``, ``YYYYMMDDTHHMMSS`` or ``YYYYMMDDTHHMMSSZ``),
``title`` = ``SUMMARY``, ``location`` = ``LOCATION``, ``abstract`` =
``DESCRIPTION`` (``abstract_html`` when it is HTML; ``X-ALT-DESC`` when
there is no DESCRIPTION), ``url`` = the first ``URL`` that is a web page,
``categories`` = ``CATEGORIES``, ``status_hint`` from ``STATUS``
(``CANCELLED``, ``TENTATIVE``, ``CONFIRMED``). ``ORGANIZER`` and
``ATTENDEE`` are never read. Of several VEVENTs with the same native id
the first is kept.

Parameters (``params``; any other key is a ``ConfigError`` that names it,
``extra_hosts`` excepted):

* ``property_fields``: which draft field a property fills instead of the
  default above. Keys: ``SUMMARY``, ``DESCRIPTION``, ``LOCATION``,
  ``URL``. Values: ``title``, ``speaker`` (the raw line, to
  ``speaker_text``), ``series``, ``location``, ``abstract``, ``url``,
  ``stream_url`` (sets ``is_online``), or null to ignore the property. For
  example a feed whose SUMMARY is the speaker and whose LOCATION is the
  page of the talk::

      property_fields: {SUMMARY: speaker, LOCATION: url, URL: stream_url}

* ``patterns``: regular expressions per property (``SUMMARY``,
  ``DESCRIPTION``, ``LOCATION``; one string or a list), searched in the
  text of the property (an HTML description as plain text, ``plain_text``).
  Every named group of every pattern that matches sets the draft field of
  its name: ``title``, ``speaker`` with ``affiliation`` (one structured
  speaker, replacing ``speaker_text``), ``series``, ``location``,
  ``abstract``, ``url``, ``stream_url``. The first pattern that sets a
  field wins, and what the patterns set wins over ``property_fields``. A
  pattern is applied to at most ``MAX_PATTERN_INPUT`` characters.
* ``calendar_ids`` and ``embed_selector``: research notes (where the feed
  was found); not read.

Public helpers for adapters of feeds with a layout of their own (for
example the custom ``researchseminars`` module), which take the common
part from here and read the rest themselves: ``read_calendar``,
``occurrences``, ``occurrence_draft`` and ``vevent_draft`` (one VEVENT to
the common part of a draft), ``property_texts`` and ``plain_text``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Final, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import icalendar
from dateutil.rrule import rrule, rruleset, rrulestr
from pydantic import BeforeValidator, ValidationError, field_validator
from selectolax.lexbor import LexborHTMLParser

from seminars.config import ConfigError
from seminars.httpbase import HttpClient
from seminars.models import (
    EventDraft,
    EventStatus,
    FetchedDocument,
    Model,
    RawPayload,
    SourceConfig,
    SpeakerDraft,
    TimeWindow,
)
from seminars.sources.base import ParseError, absolute_url, fetch_urls, register_adapter

SHARED_PARAMS: Final = frozenset({"extra_hosts"})
"""Keys of ``params`` that the shared helpers read; they are not checked here."""

NOTE_PARAMS: Final = frozenset({"calendar_ids", "embed_selector"})
"""Keys of ``params`` that record research notes; accepted and not read."""

MAX_OCCURRENCES: Final = 500
"""Most occurrences of one recurring event inside the window."""

MAX_EXPANSION_STEPS: Final = 10_000
"""Most steps through the recurrence of one event, counted from its DTSTART."""

MAX_FEED_STEPS: Final = 200_000
"""Most steps through the recurrences of one feed (well under a second in all)."""

MAX_RECURRING_EVENTS: Final = 1_000
"""Most recurring VEVENTs of one feed that are expanded (not counting those that ended)."""

MAX_PATTERN_INPUT: Final = 20_000
"""Longest text a configured regular expression is applied to."""

Moment = date | datetime
Property = Literal["SUMMARY", "DESCRIPTION", "LOCATION", "URL"]
TextProperty = Literal["SUMMARY", "DESCRIPTION", "LOCATION"]
Target = Literal["title", "speaker", "series", "location", "abstract", "url", "stream_url"]

DEFAULT_FIELDS: Final[dict[str, Target]] = {
    "SUMMARY": "title",
    "DESCRIPTION": "abstract",
    "LOCATION": "location",
    "URL": "url",
}
PATTERN_GROUPS: Final = frozenset(
    {"title", "speaker", "affiliation", "series", "location", "abstract", "url", "stream_url"}
)
_STATUS: Final[dict[str, EventStatus]] = {
    "CANCELLED": "cancelled",
    "TENTATIVE": "tentative",
    "CONFIRMED": "confirmed",
}
_HTML_TAGS: Final = frozenset(
    {
        *("a", "abbr", "b", "blockquote", "br", "code", "div", "em", "font", "hr", "i", "img"),
        *("h1", "h2", "h3", "h4", "h5", "h6", "li", "ol", "p", "pre", "span", "strong"),
        *("sub", "sup", "table", "tbody", "td", "th", "tr", "u", "ul"),
    }
)
_LINK_TARGETS: Final[tuple[Target, ...]] = ("url", "stream_url")
_UNSUPPORTED_RULE_PARTS: Final = frozenset({"BYYEARDAY", "BYWEEKNO", "BYEASTER"})
_DAYS_IN_MONTH: Final = {
    month: 29 if month == 2 else 30 if month in (4, 6, 9, 11) else 31 for month in range(1, 13)
}
_WEEKDAYS: Final = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_BLOCK_TAGS: Final = "p, div, li, tr, h1, h2, h3, h4, h5, h6, blockquote, pre, hr"


# --- parameters ----------------------------------------------------------------------------


def _pattern_list(value: object) -> object:
    return [value] if isinstance(value, str) else value


PatternList = Annotated[list[re.Pattern[str]], BeforeValidator(_pattern_list)]


class IcsParams(Model):
    """``params`` of an ``ics`` source, validated."""

    property_fields: dict[Property, Target | None] = {}
    patterns: dict[TextProperty, PatternList] = {}
    calendar_ids: list[str] = []
    embed_selector: str | None = None

    @field_validator("patterns")
    @classmethod
    def _known_groups(
        cls, value: dict[str, list[re.Pattern[str]]]
    ) -> dict[str, list[re.Pattern[str]]]:
        for patterns in value.values():
            for pattern in patterns:
                unknown = sorted(set(pattern.groupindex) - PATTERN_GROUPS)
                if unknown:
                    names = ", ".join(unknown)
                    raise ValueError(f"unknown named group(s) {names} in {pattern.pattern!r}")
        return value


def ics_params(source: SourceConfig) -> IcsParams:
    """The validated ``params`` of ``source``; ``ConfigError`` names a bad or unknown key."""
    data = {key: value for key, value in source.params.items() if key not in SHARED_PARAMS}
    try:
        return IcsParams.model_validate(data)
    except ValidationError as error:
        problems = "; ".join(
            f"params.{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors(include_url=False, include_input=False)
        )
        raise ConfigError(f"source {source.id} (adapter ics): {problems}") from error


# --- reading a feed ------------------------------------------------------------------------


@dataclass(frozen=True)
class IcsCalendar:
    """The VEVENTs of one feed and the zone its floating times are in (``X-WR-TIMEZONE``)."""

    vevents: tuple[icalendar.Component, ...]
    timezone: str | None = None


def _zone_name(value: object) -> str | None:
    name = str(value).strip() if value is not None else ""
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def read_calendar(document: FetchedDocument) -> IcsCalendar:
    """The VEVENTs of an iCalendar body; ``ParseError`` when it is no calendar.

    A body without any VCALENDAR (an HTML error page, a login form, a
    truncated file) cannot be understood. A calendar without events is fine.
    """
    if "BEGIN:VCALENDAR" not in document.text.upper():
        raise ParseError(f"the answer is not an iCalendar feed: {document.url}")
    try:
        components = icalendar.Calendar.from_ical(document.text, multiple=True)
    except ValueError as error:
        raise ParseError(f"the iCalendar feed cannot be read: {document.url}") from error
    calendars = [item for item in components if item.name == "VCALENDAR"]
    if not calendars:
        raise ParseError(f"the answer is not an iCalendar feed: {document.url}")
    vevents = tuple(event for calendar in calendars for event in calendar.walk("VEVENT"))
    return IcsCalendar(vevents=vevents, timezone=_zone_name(calendars[0].get("X-WR-TIMEZONE")))


def _values(vevent: icalendar.Component, name: str) -> list[Any]:
    value = vevent.get(name)
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def property_texts(vevent: icalendar.Component, name: str) -> list[str]:
    """The non-empty text values of property ``name`` (several for a repeated property).

    ``CATEGORIES`` gives one entry per category. Escapes (``\\,``,
    ``\\n``) are already undone, and folded lines are joined.
    """
    texts: list[str] = []
    for value in _values(vevent, name):
        parts = getattr(value, "cats", None)
        items = parts if isinstance(parts, list) else [value]
        texts += [str(item).strip() for item in items if str(item).strip()]
    return texts


def _first_text(vevent: icalendar.Component, name: str) -> str | None:
    texts = property_texts(vevent, name)
    return texts[0] if texts else None


def _moment(vevent: icalendar.Component, name: str) -> Moment | None:
    """The date or date-time of property ``name``; ``ValueError`` when it is broken."""
    values = _values(vevent, name)
    if not values:
        return None
    moment = getattr(values[0], "dt", None)  # a broken value raises (a ValueError) here
    if not isinstance(moment, date):
        raise ValueError(f"{name} is not a date or a date-time")
    return moment


def _end(vevent: icalendar.Component, start: Moment) -> Moment | None:
    end = _moment(vevent, "DTEND")
    if end is not None:
        return end
    durations = _values(vevent, "DURATION")
    duration = getattr(durations[0], "dt", None) if durations else None
    return start + duration if isinstance(duration, timedelta) else None


# --- occurrences ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IcsOccurrence:
    """One occurrence of a VEVENT: its start, its end as in iCalendar, and its native id.

    ``end`` follows iCalendar: for a date it is the day after the last day.
    ``occurrence_draft`` converts it to the draft convention.
    """

    vevent: icalendar.Component
    start: Moment
    end: Moment | None
    native_id: str | None

    @classmethod
    def of(cls, vevent: icalendar.Component) -> IcsOccurrence:
        """The VEVENT as it stands: its own DTSTART, DTEND and UID.

        Raises ``ValueError`` when it has no usable DTSTART.
        """
        start = _moment(vevent, "DTSTART")
        if start is None:
            raise ValueError("the VEVENT has no DTSTART")
        return cls(vevent, start, _end(vevent, start), _first_text(vevent, "UID"))


def instance_key(moment: Moment) -> str:
    """The text of a RECURRENCE-ID: ``YYYYMMDD``, ``YYYYMMDDTHHMMSS`` or UTC with ``Z``."""
    if not isinstance(moment, datetime):
        return moment.strftime("%Y%m%d")
    if moment.tzinfo is None:
        return moment.strftime("%Y%m%dT%H%M%S")
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _is_recurring(vevent: icalendar.Component) -> bool:
    return "RRULE" in vevent or "RDATE" in vevent


_Override = tuple[icalendar.Component, Moment]
"""A VEVENT with RECURRENCE-ID and the original start that this property names."""


def occurrences(calendar: IcsCalendar, window: TimeWindow, *, timezone: str) -> list[IcsOccurrence]:
    """Every occurrence of the calendar's events; recurring ones only inside ``window``.

    ``timezone`` is the zone of floating times (``calendar.timezone`` or the
    source's). VEVENTs that cannot be used are left out; see the module
    docstring for the limits of the expansion. Raises ``ParseError`` when
    the feed has more than ``MAX_RECURRING_EVENTS`` recurring events that
    have to be expanded or when they need more than ``MAX_FEED_STEPS``
    steps: a part of the feed would make the events left out count as gone.
    """
    overrides: dict[tuple[str, str], _Override] = {}
    masters: list[icalendar.Component] = []
    for vevent in calendar.vevents:
        uid = _first_text(vevent, "UID")
        try:
            recurrence_id = _moment(vevent, "RECURRENCE-ID")
        except ValueError:
            continue
        if uid is not None and recurrence_id is not None:
            overrides.setdefault((uid, instance_key(recurrence_id)), (vevent, recurrence_id))
        else:
            masters.append(vevent)
    found: list[IcsOccurrence] = []
    steps = expanded = 0
    for vevent in masters:
        try:
            items, expansion = _occurrences_of(
                vevent, window, timezone, overrides, budget=MAX_FEED_STEPS - steps
            )
        except (ValueError, TypeError, OverflowError):
            continue
        steps += expansion.steps
        expanded += expansion.expanded
        if expanded > MAX_RECURRING_EVENTS:
            raise ParseError(
                f"the feed has more than {MAX_RECURRING_EVENTS} recurring events"
                " that do not end before the run window"
            )
        found += items
    for (uid, key), (vevent, original) in overrides.items():
        single = _replacement(vevent, original, None, f"{uid}/{key}")
        if single is not None and _starts_in(single.start, window, timezone):
            found.append(single)
    return found


def _replacement(
    override: icalendar.Component, original: Moment, end: Moment | None, native_id: str | None
) -> IcsOccurrence | None:
    """The occurrence that ``override`` makes of the one that starts at ``original``.

    ``end`` is the end of that original occurrence. An override without
    DTSTART (which a published feed may send) keeps the original start, and
    the original end unless it has a DTEND or DURATION of its own. ``None``
    when its times cannot be read: the occurrence is then left as the
    recurrence gives it, and the rest of the series is not affected.
    """
    try:
        start = _moment(override, "DTSTART")
        if start is not None:
            return IcsOccurrence(override, start, _end(override, start), native_id)
        own_end = _end(override, original)
    except ValueError:
        return None
    return IcsOccurrence(override, original, end if own_end is None else own_end, native_id)


def _occurrences_of(
    vevent: icalendar.Component,
    window: TimeWindow,
    timezone: str,
    overrides: dict[tuple[str, str], _Override],
    *,
    budget: int,
) -> tuple[list[IcsOccurrence], _Expansion]:
    """The occurrences of one VEVENT and what expanding it cost.

    Replaced occurrences are taken out of ``overrides``; ``budget`` is the
    number of steps the feed has left.
    """
    first = IcsOccurrence.of(vevent)
    uid = first.native_id
    if not _is_recurring(vevent):
        # An override of the only occurrence replaces the event and keeps its native id.
        return [_overridden(first, uid, instance_key(first.start), overrides)], _Expansion()
    expansion = _expand(vevent, first.start, window, timezone, budget=budget)
    duration = _duration(first.start, first.end)
    found: list[IcsOccurrence] = []
    for start in expansion.starts:
        key = instance_key(start)
        native_id = None if uid is None else f"{uid}/{key}"
        end = None if duration is None else start + duration
        found.append(_overridden(IcsOccurrence(vevent, start, end, native_id), uid, key, overrides))
    return found, expansion


def _overridden(
    occurrence: IcsOccurrence,
    uid: str | None,
    key: str,
    overrides: dict[tuple[str, str], _Override],
) -> IcsOccurrence:
    """``occurrence``, or what its override (taken out of ``overrides``) makes of it."""
    override = overrides.pop((uid, key), None) if uid is not None else None
    if override is None:
        return occurrence
    moved = _replacement(override[0], occurrence.start, occurrence.end, occurrence.native_id)
    return occurrence if moved is None else moved


def _duration(start: Moment, end: Moment | None) -> timedelta | None:
    if end is None:
        return None
    if isinstance(start, datetime) and isinstance(end, datetime):
        return end - start
    if not isinstance(start, datetime) and not isinstance(end, datetime):
        return end - start
    return None  # a date as the end of a timed event, or the other way round


def _local_naive(moment: datetime, timezone: str) -> datetime:
    return moment.astimezone(ZoneInfo(timezone)).replace(tzinfo=None)


def _align(value: Moment, anchor: datetime, timezone: str) -> datetime:
    """``value`` as a datetime comparable with ``anchor`` (both aware or both naive)."""
    if not isinstance(value, datetime):
        value = datetime.combine(value, anchor.time())
    if anchor.tzinfo is None:
        return value if value.tzinfo is None else _local_naive(value, timezone)
    return value.replace(tzinfo=anchor.tzinfo) if value.tzinfo is None else value


def check_rule(parts: dict[str, list[Any]], start: date | None = None) -> None:
    """Refuse a recurrence rule that the expansion does not support (``ValueError``).

    The recurrence library steps through every period from DTSTART until
    the year 9999 when its filters never match, whatever UNTIL or COUNT say
    (about a microsecond per period: up to 3 s for a daily rule, 0.6 s for
    a weekly one, 0.3 s for a monthly one). So only the rule parts that
    calendars of talks use are accepted: ``FREQ`` of ``DAILY``, ``WEEKLY``,
    ``MONTHLY`` or ``YEARLY``; no ``BYYEARDAY``, ``BYWEEKNO`` or
    ``BYEASTER``; months 1 to 12 and days of the month 1 to 31 (or -31 to
    -1), and ``BYMONTH`` with ``BYMONTHDAY`` only when one of the days
    exists in one of the months; no ``BYMONTHDAY`` in a weekly rule (RFC
    5545 forbids it); ``BYDAY`` ordinals only in a monthly or yearly rule,
    without ``BYMONTHDAY`` and only up to what a period has; ``BYSETPOS``
    likewise. With ``start`` (the DTSTART) a rule is also refused when its
    ``INTERVAL`` never lands on a period that its filters accept
    (``_check_interval``). Over some 7000 accepted combinations of these
    parts, the slowest rule without any occurrence took 0.03 s; the number
    of recurring events of a feed is bounded as well (``MAX_RECURRING_EVENTS``).
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
    _check_positions(keys, frequency[0])
    if start is not None:
        _check_interval(keys, frequency[0], start)


def _check_calendar_days(keys: dict[str, list[Any]], frequency: str) -> None:
    """BYMONTH and BYMONTHDAY name days that exist."""
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


def _check_ordinals(keys: dict[str, list[Any]], frequency: str) -> None:
    """BYDAY with an ordinal (``2MO``, ``-1FR``) only where a period has that many weekdays."""
    ordinals = [abs(int(day[:-2])) for day in map(str, keys.get("BYDAY", [])) if day[:-2]]
    if not ordinals:
        return
    if frequency in ("DAILY", "WEEKLY"):
        raise ValueError("BYDAY with an ordinal is only valid in a monthly or yearly rule")
    if "BYMONTHDAY" in keys:
        # The ordinal already fixes the day; the two together rarely meet, if ever.
        raise ValueError("BYDAY with an ordinal is not supported together with BYMONTHDAY")
    limit = 5 if frequency == "MONTHLY" or keys.get("BYMONTH") else 53
    if max(ordinals) > limit:
        raise ValueError("BYDAY names an ordinal that no period has")


def _check_positions(keys: dict[str, list[Any]], frequency: str) -> None:
    """BYSETPOS only in a monthly or yearly rule, and only for positions a period can have."""
    if "BYSETPOS" not in keys:
        return
    if frequency not in ("MONTHLY", "YEARLY"):
        raise ValueError("BYSETPOS is only supported with a monthly or yearly rule")
    largest = max(abs(int(position)) for position in keys["BYSETPOS"])
    if largest > _set_size_bound(keys, yearly=frequency == "YEARLY"):
        raise ValueError("BYSETPOS names a position that no period has")


def _check_interval(keys: dict[str, list[Any]], frequency: str, start: date) -> None:
    """Refuse a rule whose ``INTERVAL`` never lands on a period that its filters accept.

    A daily rule whose INTERVAL is a multiple of 7 stays on the weekday of
    ``start``, so its BYDAY must name that weekday. A daily rule with an
    INTERVAL may not combine BYDAY with BYMONTHDAY (with INTERVAL=27, for
    example, it can step through the centuries without a match). A monthly
    rule only reaches the months that are congruent to the month of
    ``start`` modulo gcd(INTERVAL, 12), so its BYMONTH must name one of them.
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
    months = [int(month) for month in keys.get("BYMONTH", [])]
    step = math.gcd(interval, 12)
    if frequency == "MONTHLY" and months and all((m - start.month) % step for m in months):
        raise ValueError("BYMONTH never names a month that this monthly rule falls on")


def _set_size_bound(keys: dict[str, list[Any]], *, yearly: bool) -> int:
    """An upper bound of the number of occurrences in one month or year of a rule."""
    months = len(set(keys.get("BYMONTH", []))) or (12 if yearly else 1)
    weekdays = [str(day) for day in keys.get("BYDAY", [])]
    by_weekday = sum(1 if day[:1] in "+-0123456789" else 5 for day in weekdays) * months
    by_monthday = len(set(keys.get("BYMONTHDAY", []))) * months
    counts = [count for count in (by_weekday, by_monthday) if count] or [months]
    times = 1
    for part in ("BYHOUR", "BYMINUTE", "BYSECOND"):
        times *= len(set(keys.get(part, []))) or 1
    return min(counts) * times


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


def _list_values(vevent: icalendar.Component, name: str) -> list[Moment]:
    moments: list[Moment] = []
    for value in _values(vevent, name):
        for item in getattr(value, "dts", []):
            moment = getattr(item, "dt", None)
            if isinstance(moment, date):  # a PERIOD (a tuple) is not used
                moments.append(moment)
    return moments


@dataclass(frozen=True)
class _Expansion:
    """The starts that the recurrence of a VEVENT gives inside the window, and their cost.

    ``expanded`` is false when nothing was expanded: the event does not
    recur, or all its rules end before the window.
    """

    starts: tuple[Moment, ...] = ()
    steps: int = 0
    expanded: bool = False


def _recurrence(
    vevent: icalendar.Component, anchor: datetime, timezone: str
) -> tuple[rruleset, datetime | None]:
    """The rule set of a recurring VEVENT and the latest start it can give.

    That bound is known (else ``None``) when every RRULE has an UNTIL: it is
    the latest of those, of the RDATEs and of DTSTART (``anchor``).
    """
    rules = rruleset()
    lasts: list[datetime | None] = [anchor]
    for value in _values(vevent, "RRULE"):
        text, until = _rule_text(value, anchor, timezone)
        # The text of one RRULE always gives a single rule, never a set.
        rules.rrule(cast(rrule, rrulestr(text, dtstart=anchor)))
        lasts.append(until)
    rules.rdate(anchor)
    for moment in _list_values(vevent, "RDATE"):
        aligned = _align(moment, anchor, timezone)
        rules.rdate(aligned)
        lasts.append(aligned)
    for moment in _list_values(vevent, "EXDATE"):
        rules.exdate(_align(moment, anchor, timezone))
    bounded = [moment for moment in lasts if moment is not None]
    return rules, max(bounded) if len(bounded) == len(lasts) else None


def _expand(
    vevent: icalendar.Component, start: Moment, window: TimeWindow, timezone: str, *, budget: int
) -> _Expansion:
    """The starts of the occurrences of a recurring VEVENT that start inside ``window``.

    Every occurrence from DTSTART on is one step. Raises ``ParseError`` when
    the expansion needs more than the ``budget`` steps that the feed has left.
    """
    is_date = not isinstance(start, datetime)
    anchor = start if isinstance(start, datetime) else datetime.combine(start, time.min)
    rules, last = _recurrence(vevent, anchor, timezone)
    low, high = _window_bounds(window, anchor, timezone)
    if last is not None and last < low:
        return _Expansion()
    starts: list[Moment] = []
    steps = 0
    for moment in rules:
        if moment >= high or len(starts) >= MAX_OCCURRENCES or steps >= MAX_EXPANSION_STEPS:
            break
        if steps >= budget:
            raise ParseError(
                f"the recurring events of the feed need more than {MAX_FEED_STEPS} steps"
            )
        steps += 1
        if moment >= low:
            starts.append(moment.date() if is_date else moment)
    return _Expansion(tuple(starts), steps, expanded=True)


def _window_bounds(
    window: TimeWindow, anchor: datetime, timezone: str
) -> tuple[datetime, datetime]:
    if anchor.tzinfo is not None:
        return window.start, window.end
    return _local_naive(window.start, timezone), _local_naive(window.end, timezone)


def _starts_in(start: Moment, window: TimeWindow, timezone: str) -> bool:
    anchor = start if isinstance(start, datetime) else datetime.combine(start, time.min)
    low, high = _window_bounds(window, anchor, timezone)
    return low <= anchor < high


# --- drafts --------------------------------------------------------------------------------


def _looks_like_html(text: str) -> bool:
    if "<" not in text or ">" not in text:
        return False
    body = LexborHTMLParser(text).body
    return body is not None and any(node.tag in _HTML_TAGS for node in body.traverse())


def plain_text(text: str) -> str:
    """``text`` without markup when it is HTML (line breaks kept), else ``text`` as it is.

    ``<br>`` and the start of block elements (paragraphs, list items, ...)
    become line breaks and entities are decoded. Text that only contains a
    ``<`` (``a < b``, ``$x<y$``) is not HTML and stays as it is.
    """
    if not _looks_like_html(text):
        return text
    tree = LexborHTMLParser(text)
    for node in tree.css("br"):
        node.replace_with("\n")
    for node in tree.css(_BLOCK_TAGS):
        node.insert_before("\n")
    body = tree.body
    return body.text(deep=True, separator="", strip=False) if body is not None else ""


def _description(vevent: icalendar.Component) -> tuple[str | None, str | None]:
    """``(abstract, abstract_html)`` of a VEVENT."""
    text = _first_text(vevent, "DESCRIPTION")
    if text is None:
        return None, _first_text(vevent, "X-ALT-DESC")
    return (None, text) if _looks_like_html(text) else (text, None)


def _first_url(texts: Sequence[str], base_url: str) -> str | None:
    for text in texts:
        url = absolute_url(base_url, text)
        if url is not None:
            return url
    return None


def _draft_times(occurrence: IcsOccurrence) -> tuple[Moment, Moment | None, bool]:
    start, end = occurrence.start, occurrence.end
    if isinstance(start, datetime):
        return start, (end if isinstance(end, datetime) and end != start else None), False
    if end is None:
        return start, None, True
    last = (
        end - timedelta(days=1)
        if not isinstance(end, datetime)
        else (end - timedelta(microseconds=1)).date()
    )
    return start, (last if last > start else None), True


def occurrence_draft(
    occurrence: IcsOccurrence,
    source: SourceConfig,
    *,
    base_url: str,
    calendar_timezone: str | None = None,
) -> EventDraft:
    """The common part of the draft of one occurrence (see the module docstring).

    ``base_url`` is the address of the feed (``document.final_url``), which
    relative links are resolved against. ``calendar_timezone`` is the zone
    of floating times when the feed names one; it becomes the draft's
    ``timezone`` for a floating start. Raises ``ValueError`` (pydantic's
    ``ValidationError`` included) when the occurrence cannot be a draft.
    """
    vevent = occurrence.vevent
    start, end, all_day = _draft_times(occurrence)
    floating = isinstance(start, datetime) and start.tzinfo is None
    abstract, abstract_html = _description(vevent)
    return EventDraft(
        source_id=source.id,
        extraction="feed",
        native_id=occurrence.native_id,
        url=_first_url(property_texts(vevent, "URL"), base_url),
        title=_first_text(vevent, "SUMMARY"),
        start=start,
        end=end,
        all_day=all_day,
        timezone=calendar_timezone if floating else None,
        location=_first_text(vevent, "LOCATION"),
        categories=list(dict.fromkeys(property_texts(vevent, "CATEGORIES"))),
        status_hint=_STATUS.get((_first_text(vevent, "STATUS") or "").upper()),
        abstract=abstract,
        abstract_html=abstract_html,
    )


def vevent_draft(
    vevent: icalendar.Component,
    source: SourceConfig,
    *,
    base_url: str,
    calendar_timezone: str | None = None,
) -> EventDraft:
    """The common part of the draft of one VEVENT as it stands (no recurrence).

    The same as ``occurrence_draft(IcsOccurrence.of(vevent), ...)``: the
    native id is the UID, the times are DTSTART and DTEND (or DURATION),
    and SUMMARY, LOCATION, DESCRIPTION, URL, CATEGORIES and STATUS fill
    their default fields. Raises ``ValueError`` for a VEVENT that cannot be
    a draft.
    """
    return occurrence_draft(
        IcsOccurrence.of(vevent), source, base_url=base_url, calendar_timezone=calendar_timezone
    )


def _assign(target: Target, texts: list[str], base_url: str) -> dict[str, Any]:
    """The draft fields that ``target`` sets from the texts of one property."""
    text = texts[0] if texts else None
    if target in ("url", "stream_url"):
        url = _first_url(texts, base_url)
        return {target: url, **({"is_online": True} if target == "stream_url" and url else {})}
    if target == "speaker":
        return {"speaker_text": text}
    if target == "abstract":
        if text is not None and _looks_like_html(text):
            return {"abstract": None, "abstract_html": text}
        return {"abstract": text, "abstract_html": None}
    return {target: text}


def _cleared(target: Target) -> dict[str, Any]:
    if target == "abstract":
        return {"abstract": None, "abstract_html": None}
    return {target: None}


def _mapped_fields(vevent: icalendar.Component, params: IcsParams, base_url: str) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    remapped: dict[str, Target | None] = {
        name: target
        for name, target in params.property_fields.items()
        if target != DEFAULT_FIELDS[name]
    }
    for name in remapped:
        changes.update(_cleared(DEFAULT_FIELDS[name]))
    for name, target in remapped.items():
        if target is not None:
            changes.update(_assign(target, property_texts(vevent, name), base_url))
    return changes


def _pattern_fields(
    vevent: icalendar.Component, params: IcsParams, base_url: str
) -> dict[str, Any]:
    groups: dict[str, str] = {}
    for name, patterns in params.patterns.items():
        text = _first_text(vevent, name)
        if text is None:
            continue
        subject = plain_text(text)[:MAX_PATTERN_INPUT]
        for pattern in patterns:
            match = pattern.search(subject)
            for key, value in (match.groupdict() if match else {}).items():
                if value is not None and value.strip() and key not in groups:
                    groups[key] = value.strip()
    changes: dict[str, Any] = {}
    for key in ("title", "series", "location", "abstract"):
        if key in groups:
            changes.update(
                {key: groups[key], **({"abstract_html": None} if key == "abstract" else {})}
            )
    for link in _LINK_TARGETS:
        if link in groups:
            changes.update(_assign(link, [groups[link]], base_url))
    if "speaker" in groups:
        speaker = SpeakerDraft(name=groups["speaker"], affiliation=groups.get("affiliation"))
        changes.update({"speakers": [speaker], "speaker_text": None})
    return changes


def _configured_draft(
    occurrence: IcsOccurrence,
    source: SourceConfig,
    params: IcsParams,
    *,
    base_url: str,
    calendar_timezone: str | None,
) -> EventDraft:
    draft = occurrence_draft(
        occurrence, source, base_url=base_url, calendar_timezone=calendar_timezone
    )
    changes = _mapped_fields(occurrence.vevent, params, base_url)
    changes.update(_pattern_fields(occurrence.vevent, params, base_url))
    return draft.evolve(**changes) if changes else draft


def _read_feed(
    document: FetchedDocument, source: SourceConfig, window: TimeWindow
) -> tuple[IcsCalendar, list[IcsOccurrence]]:
    """The calendar of one answer and its occurrences; a ``ParseError`` names the source."""
    try:
        calendar = read_calendar(document)
    except ParseError as error:
        raise ParseError(f"source {source.id}: {error}") from error
    zone = calendar.timezone or source.timezone
    try:
        return calendar, occurrences(calendar, window, timezone=zone)
    except ParseError as error:
        raise ParseError(f"source {source.id}: {error}: {document.url}") from error


@register_adapter("ics")
class IcsAdapter:
    """Reads iCalendar feeds; see the module docstring."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        ics_params(source)
        return fetch_urls(http, source, window=window)

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        params = ics_params(source)
        drafts: list[EventDraft] = []
        seen: set[str] = set()
        for document in payload.documents:
            calendar, found = _read_feed(document, source, payload.window)
            for occurrence in found:
                try:
                    draft = _configured_draft(
                        occurrence,
                        source,
                        params,
                        base_url=document.final_url,
                        calendar_timezone=calendar.timezone,
                    )
                except (ValueError, TypeError):  # pydantic's ValidationError is a ValueError
                    continue
                if draft.native_id is not None:
                    if draft.native_id in seen:
                        continue
                    seen.add(draft.native_id)
                drafts.append(draft)
        return drafts
