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
  steps of the rule from ``DTSTART``. An occurrence keeps the duration of
  the first one. A VEVENT with ``RECURRENCE-ID`` replaces the occurrence it
  names; one whose occurrence is not generated (it was moved into the
  window, or the rule is gone) stands on its own when it starts in the
  window.

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

import re
from collections.abc import Iterator, Sequence
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


def occurrences(calendar: IcsCalendar, window: TimeWindow, *, timezone: str) -> list[IcsOccurrence]:
    """Every occurrence of the calendar's events; recurring ones only inside ``window``.

    ``timezone`` is the zone of floating times (``calendar.timezone`` or the
    source's). VEVENTs that cannot be used are left out; see the module
    docstring for the limits of the expansion.
    """
    overrides: dict[tuple[str, str], icalendar.Component] = {}
    masters: list[icalendar.Component] = []
    for vevent in calendar.vevents:
        uid = _first_text(vevent, "UID")
        try:
            recurrence_id = _moment(vevent, "RECURRENCE-ID")
        except ValueError:
            continue
        if uid is not None and recurrence_id is not None:
            overrides.setdefault((uid, instance_key(recurrence_id)), vevent)
        else:
            masters.append(vevent)
    found: list[IcsOccurrence] = []
    for vevent in masters:
        try:
            found += _occurrences_of(vevent, window, timezone, overrides)
        except (ValueError, TypeError, OverflowError):
            continue
    for (uid, key), vevent in overrides.items():
        try:
            single = IcsOccurrence.of(vevent)
        except ValueError:
            continue
        if _starts_in(single.start, window, timezone):
            found.append(IcsOccurrence(vevent, single.start, single.end, f"{uid}/{key}"))
    return found


def _occurrences_of(
    vevent: icalendar.Component,
    window: TimeWindow,
    timezone: str,
    overrides: dict[tuple[str, str], icalendar.Component],
) -> list[IcsOccurrence]:
    """The occurrences of one VEVENT; replaced ones are taken out of ``overrides``."""
    first = IcsOccurrence.of(vevent)
    if not _is_recurring(vevent):
        return [first]
    duration = _duration(first.start, first.end)
    found: list[IcsOccurrence] = []
    for start in _expand(vevent, first.start, window, timezone):
        key = instance_key(start)
        native_id = None if first.native_id is None else f"{first.native_id}/{key}"
        replacement = overrides.pop((first.native_id, key), None) if first.native_id else None
        if replacement is not None:
            moved = IcsOccurrence.of(replacement)
            found.append(IcsOccurrence(replacement, moved.start, moved.end, native_id))
        else:
            end = None if duration is None else start + duration
            found.append(IcsOccurrence(vevent, start, end, native_id))
    return found


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


def _rule_text(rule: object, anchor: datetime, timezone: str) -> str:
    """The RRULE as text, with UNTIL written the way dateutil accepts it for ``anchor``."""
    if not isinstance(rule, icalendar.vRecur):
        raise ValueError("the RRULE cannot be read")
    parts = dict(rule)
    until = parts.get("UNTIL")
    if until:
        value = until[0]
        if not isinstance(value, datetime):
            value = datetime.combine(value, time(23, 59, 59))
        aligned = _align(value, anchor, timezone)
        parts["UNTIL"] = [aligned.astimezone(UTC) if aligned.tzinfo is not None else aligned]
    return icalendar.vRecur(parts).to_ical().decode("ascii")


def _list_values(vevent: icalendar.Component, name: str) -> list[Moment]:
    moments: list[Moment] = []
    for value in _values(vevent, name):
        for item in getattr(value, "dts", []):
            moment = getattr(item, "dt", None)
            if isinstance(moment, date):  # a PERIOD (a tuple) is not used
                moments.append(moment)
    return moments


def _expand(
    vevent: icalendar.Component, start: Moment, window: TimeWindow, timezone: str
) -> Iterator[Moment]:
    """Start times of the occurrences of a recurring VEVENT that start inside ``window``."""
    is_date = not isinstance(start, datetime)
    anchor = start if isinstance(start, datetime) else datetime.combine(start, time.min)
    rules = rruleset()
    for value in _values(vevent, "RRULE"):
        # The text of one RRULE always gives a single rule, never a set.
        rules.rrule(cast(rrule, rrulestr(_rule_text(value, anchor, timezone), dtstart=anchor)))
    rules.rdate(anchor)
    for moment in _list_values(vevent, "RDATE"):
        rules.rdate(_align(moment, anchor, timezone))
    for moment in _list_values(vevent, "EXDATE"):
        rules.exdate(_align(moment, anchor, timezone))
    low, high = _window_bounds(window, anchor, timezone)
    count = 0
    for step, moment in enumerate(rules):
        if step >= MAX_EXPANSION_STEPS or moment >= high or count >= MAX_OCCURRENCES:
            return
        if moment >= low:
            count += 1
            yield moment.date() if is_date else moment


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
            try:
                calendar = read_calendar(document)
            except ParseError as error:
                raise ParseError(f"source {source.id}: {error}") from error
            zone = calendar.timezone or source.timezone
            for occurrence in occurrences(calendar, payload.window, timezone=zone):
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
