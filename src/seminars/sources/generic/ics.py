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
  names no known zone leaves the value floating. The library keeps the
  zones that feeds define for the whole process, so that the first feed
  to define a name would decide it for every later one; ``read_calendar``
  makes it forget them before it reads a feed, and a feed only ever gets
  its own definitions (feeds are read one at a time: the pipeline parses
  the sources in turn).
* ``VALUE=DATE`` makes an all-day event. ``DTEND`` of a date is the day
  after the last day, so the draft's ``end`` is ``DTEND`` minus one day (no
  ``end`` for a single day). ``DURATION`` works like ``DTEND``.
* ``DTEND`` equal to ``DTSTART`` means no end time, and so does a date as
  ``DTEND`` of an event that starts at a time of day.
* Recurring events (``RRULE``, ``RDATE``, ``EXDATE``) are expanded inside
  the run window only (occurrences that start in it), at most
  ``MAX_OCCURRENCES`` per event. Each RRULE is walked only up to the end of
  the window (or its earlier UNTIL), from the window on, or from
  ``DTSTART`` when it has a COUNT, in at most ``MAX_EXPANSION_STEPS``
  steps; a rule the expansion does not support is skipped with its event,
  and one whose ``UNTIL`` lies before the window is not walked (all in
  ``seminars.sources.generic.ics_rules``, which states the cost). A feed
  whose RRULE lines need more than ``MAX_FEED_STEPS`` steps, or that walks
  more than ``MAX_RECURRING_EVENTS`` RRULE lines, is a ``ParseError``: a
  part of it would make the events left out count as gone. The lines are
  counted, not the events: one VEVENT with many RRULE properties costs as
  much as many VEVENTs. A series with COUNT is walked from its DTSTART at
  about two steps per period, so long ones add up: a feed holds 170 weekly
  series with COUNT that began in 2000, or 25 daily ones, and more is a
  ``ParseError``. One such series that began so long ago that its own
  steps run out before the window gives nothing in it; that is logged as
  a warning with the source and the UID. A feed with more than
  ``MAX_FEED_OCCURRENCES`` occurrences in the window (recurring and single
  events together) is a ``ParseError`` as well. An occurrence keeps the
  duration of the first one.
* A VEVENT with ``RECURRENCE-ID`` replaces the occurrence it names (also
  the only one of an event without recurrence, which keeps its native id);
  without ``DTSTART`` it keeps the original times, and one whose times
  cannot be read is ignored. An override whose occurrence is not generated
  (moved into the window, or the rule is gone) stands on its own when it
  starts in the window.

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
  example a feed whose SUMMARY is the speaker and whose URL is the
  livestream::

      property_fields: {SUMMARY: speaker, URL: stream_url}

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

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import icalendar
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
from seminars.sources.generic.ics_rules import (
    MAX_EXPANSION_STEPS,
    Allowance,
    Expansion,
    Limit,
    Moment,
    RecurrenceProperties,
    expand,
    starts_in,
)
from seminars.textrules import ascii_printable

logger = logging.getLogger(__name__)

SHARED_PARAMS: Final = frozenset({"extra_hosts"})
"""Keys of ``params`` that the shared helpers read; they are not checked here."""

NOTE_PARAMS: Final = frozenset({"calendar_ids", "embed_selector"})
"""Keys of ``params`` that record research notes; accepted and not read."""

MAX_FEED_STEPS: Final = 500_000
"""Most steps through the recurrence rules of one feed (0.3 to 1 s in all)."""

MAX_RECURRING_EVENTS: Final = 500
"""Most RRULE lines of one feed that do not end before the window (all VEVENTs together).

The steps bound what the walks cost; this bounds the drafts, up to
``MAX_OCCURRENCES`` per line.
"""

MAX_FEED_OCCURRENCES: Final = 5000
"""Most occurrences of one feed that start inside the window, recurring or not.

Turning an occurrence into a draft costs about 20 us; a feed of talks has
a few hundred in a window of four months.
"""

MAX_LOGGED_UID: Final = 80
"""Characters of a UID that a log line shows."""

MAX_PATTERN_INPUT: Final = 20_000
"""Longest text a configured regular expression is applied to."""

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


_READER_ERRORS: Final = (
    ValueError,
    AttributeError,
    TypeError,
    KeyError,
    IndexError,
    OSError,
    OverflowError,
    RecursionError,
)
"""What the iCalendar reader raises on broken input."""


def _zone_name(value: object) -> str | None:
    name = str(value).strip() if value is not None else ""
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):  # OSError: too long for a file name
        return None
    return name


def read_calendar(document: FetchedDocument) -> IcsCalendar:
    """The VEVENTs of an iCalendar body; ``ParseError`` when it is no calendar.

    A body without any VCALENDAR (an HTML error page, a login form, a
    truncated file) cannot be understood. A calendar without events is fine.
    Whatever the reader of the library raises on a broken feed is a
    ``ParseError`` too: besides ``ValueError``, an ``AttributeError`` or
    ``TypeError`` of a broken VTIMEZONE, an ``OSError`` of a TZID too long
    for a file name of the zone database.
    """
    if "BEGIN:VCALENDAR" not in document.text.upper():
        raise ParseError(f"the answer is not an iCalendar feed: {document.url}")
    try:
        icalendar.use_zoneinfo()  # a fresh store of zones: forget those of earlier feeds
        components = icalendar.Calendar.from_ical(document.text, multiple=True)
    except _READER_ERRORS as error:
        url = ascii_printable(document.url)
        raise ParseError(f"the iCalendar feed cannot be read: {url}") from error
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


def occurrences(
    calendar: IcsCalendar, window: TimeWindow, *, timezone: str, source_id: str | None = None
) -> list[IcsOccurrence]:
    """Every occurrence of the calendar's events; recurring ones only inside ``window``.

    ``timezone`` is the zone of floating times (``calendar.timezone`` or the
    source's). VEVENTs that cannot be used are left out; see the module
    docstring for the limits of the expansion. Raises ``ParseError`` when
    the RRULE lines of the feed need more than ``MAX_FEED_STEPS`` steps,
    when more than ``MAX_RECURRING_EVENTS`` lines are walked, or when more
    than ``MAX_FEED_OCCURRENCES`` occurrences start inside ``window``: a
    part of the feed would make the events left out count as gone. A
    series whose walk ran out of its own steps is logged with
    ``source_id`` and its UID.
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
    steps = rules = 0
    for vevent in masters:
        allowance = Allowance(steps=MAX_FEED_STEPS - steps, rules=MAX_RECURRING_EVENTS - rules)
        try:
            items, expansion = _occurrences_of(
                vevent, window, timezone, overrides, allowance=allowance
            )
        except _READER_ERRORS:  # a VEVENT that cannot be used; the others stay
            continue
        if expansion.exceeded is not None:
            raise ParseError(_limit_message(expansion.exceeded))
        if expansion.cut_short:
            _log_cut_short(source_id, _first_text(vevent, "UID"))
        steps += expansion.steps
        rules += expansion.rules
        found += items
    for (uid, key), (vevent, original) in overrides.items():
        single = _replacement(vevent, original, None, f"{uid}/{key}")
        if single is not None and starts_in(single.start, window, timezone):
            found.append(single)
    inside = sum(1 for item in found if starts_in(item.start, window, timezone))
    if inside > MAX_FEED_OCCURRENCES:
        raise ParseError(
            f"the feed has more than {MAX_FEED_OCCURRENCES} occurrences in the run window"
        )
    return found


def _log_cut_short(source_id: str | None, uid: str | None) -> None:
    """Say that a series gave no occurrence after the point where its walk stopped."""
    shown = ascii_printable(uid or "(none)")[:MAX_LOGGED_UID]
    logger.warning(
        "source %s: the recurrence of UID %s ran out of its %d steps before the end of the"
        " run window; its later occurrences are missing",
        source_id or "(unknown)",
        shown,
        MAX_EXPANSION_STEPS,
    )


def _limit_message(limit: Limit) -> str:
    """Why a feed whose expansion needed more than ``limit`` allows cannot be read."""
    if limit == "steps":
        return f"the recurrence rules of the feed need more than {MAX_FEED_STEPS} steps"
    return (
        f"the feed has more than {MAX_RECURRING_EVENTS} recurrence rules (RRULE lines)"
        " that do not end before the run window"
    )


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
    allowance: Allowance,
) -> tuple[list[IcsOccurrence], Expansion]:
    """The occurrences of one VEVENT and what expanding it cost.

    Replaced occurrences are taken out of ``overrides``; ``allowance`` is
    what the feed has left.
    """
    first = IcsOccurrence.of(vevent)
    uid = first.native_id
    if not _is_recurring(vevent):
        # An override of the only occurrence replaces the event and keeps its native id.
        return [_overridden(first, uid, instance_key(first.start), overrides)], Expansion()
    expansion = _expand(vevent, first.start, window, timezone, allowance=allowance)
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


def _list_values(vevent: icalendar.Component, name: str) -> list[Moment]:
    moments: list[Moment] = []
    for value in _values(vevent, name):
        for item in getattr(value, "dts", []):
            moment = getattr(item, "dt", None)
            if isinstance(moment, date):  # a PERIOD (a tuple) is not used
                moments.append(moment)
    return moments


def _expand(
    vevent: icalendar.Component,
    start: Moment,
    window: TimeWindow,
    timezone: str,
    *,
    allowance: Allowance,
) -> Expansion:
    """The starts of the occurrences of a recurring VEVENT inside ``window`` (``expand``)."""
    recurrence = RecurrenceProperties(
        rules=tuple(_values(vevent, "RRULE")),
        rdates=tuple(_list_values(vevent, "RDATE")),
        exdates=tuple(_list_values(vevent, "EXDATE")),
    )
    return expand(recurrence, start, window, timezone, allowance=allowance)


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
        return calendar, occurrences(calendar, window, timezone=zone, source_id=source.id)
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
