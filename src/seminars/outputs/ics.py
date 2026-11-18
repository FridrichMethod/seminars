"""ICS calendar feeds (RFC 5545) and the validator behind ``seminars validate``.

``write_ics_feeds`` writes one calendar per feed id: ``all`` holds every
event it is given, ``picks`` the events at or above the picks threshold,
each field feed the events tagged with that field and ``conferences`` the
meetings (``Event.is_meeting``: a conference, or a workshop over several
days or of at least three hours), which are in ``all`` and in their field
feeds as well. The files are a pure function of the arguments. Events are
ordered by start and uid, every time stamp comes from the event
(``DTSTAMP`` and ``LAST-MODIFIED`` are its ``last_changed``) and nothing
reads the clock, so the same events give the same bytes and a subscriber
only sees a change when an event changed.

A calendar holds

* the header that calendar programs read: the name and description of the
  feed (English locale, ``feeds.<id>.*``), the time zone and the refresh
  interval of the settings;
* one VTIMEZONE for the zone of the settings. It lists the changes of the
  UTC offset in the years the events of the run fall in, each in a
  sub-component of its own whose DTSTART is the local time at which it
  happens (no RDATE, which some readers misread), so every feed of a run
  has the same definition and no calendar is without a component;
* one VEVENT per event. ``UID`` is ``<uid>@<uid_domain>`` and never changes;
  ``SEQUENCE`` is the counter of the event. A timed event is written in the
  zone of the settings and gets the default duration when it has no end; an
  all-day event is written with DATE values and an exclusive end. An all-day
  event is free time (``TRANSP:TRANSPARENT``, and
  ``X-MICROSOFT-CDO-BUSYSTATUS:FREE`` for Outlook, which reads only that),
  so a conference of a week does not mark the days of a subscriber as busy;
  a timed event keeps the default of RFC 5545, busy. No event has an alarm:
  calendar programs ignore the alarms of a subscribed calendar. A
  cancelled event stays in the feed with ``STATUS:CANCELLED`` and a marked
  summary, because not every program shows the status.

Text from the web. Titles, names, locations and abstracts are untrusted, so
every one of them is cleaned (``seminars.textrules.clean_text``: no control
character, no emoji, no decorative character, every kind of line break a
newline) before the calendar library escapes and folds it. What should be
one line (summary, location) has its line breaks turned into spaces. The
labels inside a description come from the English locale as well.

``validate_ics`` does not use the library that wrote the feed for its own
rules: it checks line endings, line lengths, folding, the nesting of
components, required and repeated properties, the form of dates, offsets,
escapes and references to time zones, and only then lets the library parse
the document as a second opinion.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import icalendar

from seminars.config import AppConfig
from seminars.i18n import DEFAULT_LOCALE, Locale
from seminars.models import FEED_IDS, TBA_TITLE, Event, Speaker, ascii_url
from seminars.textio import write_text_atomic
from seminars.textrules import ascii_printable, clean_text

SUMMARY_MAX_CHARS: Final = 250
LOCATION_MAX_CHARS: Final = 250
ABSTRACT_MAX_CHARS: Final = 1000
SUMMARY_MAX_SPEAKERS: Final = 3
ELLIPSIS: Final = "..."

ZONE_MAX_YEARS: Final = 10
"""A time zone definition covers at most this many years after its first one."""

EMPTY_RUN_YEAR: Final = 1970
"""Year of the time zone definition when a run has no event at all."""

MEETINGS_FEED: Final = "conferences"
"""The feed of the meetings (``Event.is_meeting``), one of FEED_IDS."""

_LABEL_SPEAKER: Final = "digest.table.speaker"
_LABEL_REASONS: Final = "digest.table.reasons"
_LABEL_ONLINE: Final = "digest.labels.online"
_LABEL_DETAILS: Final = "digest.labels.details"
_LABEL_SOURCE: Final = "digest.health.source"
_LABEL_CANCELLED: Final = "digest.labels.cancelled"

_LAST_WORD: Final = re.compile(r"\s\S{0,30}$")
_NOT_A_WORD: Final = re.compile(r"[^\w]|_")
_ONE_DAY: Final = timedelta(days=1)
_ONE_SECOND: Final = timedelta(seconds=1)
_LAST_SCANNED_YEAR: Final = 9998  # the scan of a year needs the first day of the next one


@dataclass(frozen=True)
class _Context:
    """What rendering needs besides the events; built once per call."""

    zone: ZoneInfo
    zone_name: str
    uid_domain: str
    default_duration: timedelta
    refresh: timedelta
    picks_threshold: float
    locale: Locale


def _context(config: AppConfig) -> _Context:
    settings = config.settings
    return _Context(
        zone=ZoneInfo(settings.timezone),
        zone_name=settings.timezone,
        uid_domain=settings.uid_domain,
        default_duration=timedelta(minutes=settings.events.default_duration_minutes),
        refresh=timedelta(hours=settings.feeds.refresh_interval_hours),
        picks_threshold=config.interests.threshold,
        locale=Locale.load(DEFAULT_LOCALE, config.paths.locales_dir),
    )


# --- text ----------------------------------------------------------------------------------


def _one_line(text: str | None) -> str:
    """Cleaned text on a single line: every run of whitespace becomes one space."""
    return " ".join(clean_text(text or "").split())


def _paragraphs(text: str | None) -> str:
    """Cleaned text that keeps its line breaks; at most one blank line in a row."""
    lines = [" ".join(line.split()) for line in clean_text(text or "").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


def _shorten(text: str, limit: int) -> str:
    """``text`` in at most ``limit`` characters; a cut is marked with three dots.

    The cut falls between two words when the last word is not too long to
    give up, so that text without spaces (CJK, a long address) is cut too.
    """
    if len(text) <= limit:
        return text
    kept = text[: limit - len(ELLIPSIS)]
    broken = _LAST_WORD.search(kept)
    if broken is not None and not text[len(kept)].isspace():
        kept = kept[: broken.start()]
    return kept.rstrip() + ELLIPSIS


def _speaker(speaker: Speaker) -> str:
    name, affiliation = _one_line(speaker.name), _one_line(speaker.affiliation)
    return f"{name} ({affiliation})" if name and affiliation else name


def _words(text: str) -> str:
    """``text`` folded for finding a name in it: no accents, no case, words between spaces."""
    decomposed = unicodedata.normalize("NFKD", text)
    letters = "".join(char for char in decomposed if not unicodedata.combining(char))
    return f" {' '.join(_NOT_A_WORD.sub(' ', letters.casefold()).split())} "


def _speaker_list(names: Sequence[str]) -> str:
    speakers = ", ".join(names[:SUMMARY_MAX_SPEAKERS])
    return f"{speakers}, {ELLIPSIS}" if len(names) > SUMMARY_MAX_SPEAKERS else speakers


def _summary(event: Event, ctx: _Context) -> str:
    """``<title> - <speakers>``, or ``<series>: <speakers>`` while the title is not announced.

    A speaker whose name the title holds already is left out after the
    title, so that "Thesis Defense: Jane Doe" does not end in "- Jane Doe".
    The name counts when its words occur as whole words of the title, both
    folded (no accents, no case, no punctuation); names are stored without
    honorifics and degrees, so this is the comparison of their canonical
    form. The limit of ``SUMMARY_MAX_SPEAKERS`` applies to the speakers that
    are left, and without one the summary is the title alone.
    """
    title = _one_line(event.title)
    names = [name for speaker in event.speakers if (name := _one_line(speaker.name))]
    if not title or title == TBA_TITLE:
        heading = _one_line(event.series) or _one_line(event.host_org)
        text = f"{heading}: {_speaker_list(names) or TBA_TITLE}"
    else:
        named = _words(title)
        speakers = _speaker_list([name for name in names if _words(name) not in named])
        text = f"{title} - {speakers}" if speakers else title
    if event.status == "cancelled":
        text = f"{ctx.locale.t(_LABEL_CANCELLED)}: {text}"
    return _shorten(text, SUMMARY_MAX_CHARS)


def _location(event: Event, ctx: _Context) -> str:
    """The place on one line; an event that is online only says so."""
    place = _shorten(_one_line(event.location), LOCATION_MAX_CHARS)
    return place or (ctx.locale.t(_LABEL_ONLINE) if event.is_online else "")


def _facts(event: Event, ctx: _Context) -> list[str]:
    """The lines above the abstract: who, which series, who hosts, why picked, online."""
    label = ctx.locale.t
    lines: list[str] = []
    speakers = "; ".join(text for speaker in event.speakers if (text := _speaker(speaker)))
    if speakers:
        lines.append(f"{label(_LABEL_SPEAKER)}: {speakers}")
    series, host = _one_line(event.series), _one_line(event.host_org)
    lines.extend(name for name in (series, host) if name)
    if series.casefold() == host.casefold() and series:
        lines.pop()
    reasons = "; ".join(text for reason in event.picks_reasons if (text := _one_line(reason)))
    if reasons and event.is_pick(ctx.picks_threshold):
        lines.append(f"{label(_LABEL_REASONS)}: {reasons}")
    if event.is_online:
        stream = f": {ascii_url(str(event.stream_url))}" if event.stream_url is not None else ""
        lines.append(f"{label(_LABEL_ONLINE)}{stream}")
    return lines


def _links(event: Event, ctx: _Context) -> list[str]:
    """The detail page, then every other page the event was found on."""
    detail = ascii_url(str(event.url))
    others = [url for source in event.source_urls if (url := ascii_url(str(source))) != detail]
    source = ctx.locale.t(_LABEL_SOURCE)
    return [f"{ctx.locale.t(_LABEL_DETAILS)}: {detail}", *(f"{source}: {url}" for url in others)]


def _description(event: Event, ctx: _Context) -> str:
    abstract = _shorten(_paragraphs(event.abstract), ABSTRACT_MAX_CHARS)
    blocks = [_facts(event, ctx), [abstract] if abstract else [], _links(event, ctx)]
    return "\n\n".join("\n".join(block) for block in blocks if block)


# --- time ----------------------------------------------------------------------------------


def _instant(moment: datetime, zone: ZoneInfo) -> datetime:
    """``moment`` as local time of ``zone``, or in UTC when that local time is ambiguous.

    A local time in the hour that is repeated when daylight saving time ends
    names two moments. RFC 5545 says a reader takes the first of them, but
    ical.js and libical take the second, so both moments of that hour are
    written in UTC, which no reader can misread.
    """
    local = moment.astimezone(zone)
    other = local.replace(fold=1 - local.fold)
    return moment.astimezone(UTC) if other.utcoffset() != local.utcoffset() else local


def _day_after(local: datetime) -> date:
    """The first day that an all-day event ending at ``local`` does not cover."""
    return local.date() if local.time() == time.min else local.date() + _ONE_DAY


def _span(event: Event, ctx: _Context) -> tuple[date, date]:
    """Values of DTSTART and DTEND: two dates for an all-day event, else two date-times."""
    if event.all_day:
        first = event.start.astimezone(ctx.zone).date()
        after = first + _ONE_DAY
        if event.end is not None:
            after = max(after, _day_after(event.end.astimezone(ctx.zone)))
        return first, after
    start = event.start.astimezone(UTC)
    end = event.end.astimezone(UTC) if event.end is not None else start
    if end <= start:
        end = start + ctx.default_duration
    return _instant(start, ctx.zone), _instant(end, ctx.zone)


# --- components ----------------------------------------------------------------------------


def _vevent(event: Event, ctx: _Context) -> icalendar.Event:
    component = icalendar.Event()
    stamp = event.last_changed.astimezone(UTC)
    start, end = _span(event, ctx)
    component.add("uid", f"{event.uid}@{ctx.uid_domain}")
    component.add("dtstamp", stamp)
    component.add("last-modified", stamp)
    component.add("sequence", event.sequence)
    component.add("dtstart", start)
    component.add("dtend", end)
    component.add("summary", _summary(event, ctx))
    component.add("description", _description(event, ctx))
    component.add("url", ascii_url(str(event.url)))
    component.add("status", event.status.upper())
    if event.all_day:
        component.add("transp", "TRANSPARENT")
        component.add("x-microsoft-cdo-busystatus", "FREE")
    location = _location(event, ctx)
    if location:
        component.add("location", location)
    if event.fields:
        component.add("categories", [ctx.locale.t(f"fields.{field}") for field in event.fields])
    return component


@dataclass(frozen=True)
class _Observance:
    """A stretch of time in which a zone keeps one UTC offset."""

    onset: datetime  # naive: the local time at which it begins, read with ``offset_from``
    offset_from: timedelta
    offset_to: timedelta
    name: str
    daylight: bool


def _offset(moment: datetime, zone: ZoneInfo) -> timedelta:
    return moment.astimezone(zone).utcoffset() or timedelta(0)


def _change(low: datetime, high: datetime, zone: ZoneInfo) -> _Observance:
    """The change of offset between two UTC moments, found to the second."""
    before = _offset(low, zone)
    while high - low > _ONE_SECOND:
        middle = low + timedelta(seconds=(high - low) // _ONE_SECOND // 2)
        if _offset(middle, zone) == before:
            low = middle
        else:
            high = middle
    local = high.astimezone(zone)
    return _Observance(
        onset=(high + before).replace(tzinfo=None),
        offset_from=before,
        offset_to=_offset(high, zone),
        name=local.tzname() or zone.key,
        daylight=bool(local.dst()),
    )


@lru_cache(maxsize=512)
def _year_changes(zone_name: str, year: int) -> tuple[_Observance, ...]:
    """Every change of the UTC offset of a zone during one local calendar year."""
    zone = ZoneInfo(zone_name)
    moment = datetime(year, 1, 1, tzinfo=zone).astimezone(UTC)
    stop = datetime(year + 1, 1, 1, tzinfo=zone).astimezone(UTC)
    changes: list[_Observance] = []
    while moment < stop:
        later = min(moment + _ONE_DAY, stop)
        if _offset(later, zone) != _offset(moment, zone):
            changes.append(_change(moment, later, zone))
        moment = later
    return tuple(changes)


def _observances(ctx: _Context, first_year: int, last_year: int) -> list[_Observance]:
    """The offset at the start of ``first_year`` and every change until the end of ``last_year``."""
    opening = datetime(first_year, 1, 1, tzinfo=ctx.zone)
    offset = opening.utcoffset() or timedelta(0)
    initial = _Observance(
        onset=opening.replace(tzinfo=None),
        offset_from=offset,
        offset_to=offset,
        name=opening.tzname() or ctx.zone_name,
        daylight=bool(opening.dst()),
    )
    years = range(first_year, min(last_year, _LAST_SCANNED_YEAR) + 1)
    return [initial, *(change for year in years for change in _year_changes(ctx.zone_name, year))]


def _vtimezone(ctx: _Context, first_year: int, last_year: int) -> icalendar.Timezone:
    """The zone of the settings: one sub-component per onset, named by its DTSTART alone.

    RFC 5545 also allows one sub-component per kind of change with the later
    onsets as RDATEs, but ical.js (Thunderbird and other web calendars)
    ignores the DTSTART of a sub-component that has an RDATE and so loses
    the first onset of every kind: summer time of the first year would read
    as standard time.
    """
    component = icalendar.Timezone()
    component.add("tzid", ctx.zone_name)
    for item in _observances(ctx, first_year, last_year):
        part = icalendar.TimezoneDaylight() if item.daylight else icalendar.TimezoneStandard()
        part.add("dtstart", item.onset)
        part.add("tzoffsetfrom", item.offset_from)
        part.add("tzoffsetto", item.offset_to)
        part.add("tzname", item.name)
        component.add_component(part)
    return component


def _years(events: Sequence[Event], ctx: _Context) -> tuple[int, int]:
    """First and last year the time zone definition has to cover."""
    spans = [_span(event, ctx) for event in events]
    if not spans:
        return EMPTY_RUN_YEAR, EMPTY_RUN_YEAR
    first = min(start.year for start, _ in spans)
    return first, min(max(end.year for _, end in spans), first + ZONE_MAX_YEARS)


def _calendar_text(feed_id: str, parts: Sequence[icalendar.Component], ctx: _Context) -> str:
    """One feed as text with CRLF line endings."""
    calendar = icalendar.Calendar()
    calendar.add("version", "2.0")
    calendar.add("prodid", f"-//{ctx.uid_domain}//seminars//EN")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-calname", icalendar.vText(ctx.locale.t(f"feeds.{feed_id}.name")))
    calendar.add("x-wr-caldesc", icalendar.vText(ctx.locale.t(f"feeds.{feed_id}.description")))
    calendar.add("x-wr-timezone", icalendar.vText(ctx.zone_name))
    calendar.add("refresh-interval", ctx.refresh, parameters={"VALUE": "DURATION"})
    calendar.add("x-published-ttl", icalendar.vDuration(ctx.refresh))
    for part in parts:
        calendar.add_component(part)
    return calendar.to_ical().decode("utf-8")


def _ordered(events: Sequence[Event]) -> list[Event]:
    """Events by start and uid; a uid that appears twice is an error."""
    repeated = sorted(
        uid for uid, count in Counter(event.uid for event in events).items() if count > 1
    )
    if repeated:
        raise ValueError(f"duplicate event uid: {repeated[0]!r}")
    return sorted(events, key=lambda event: (event.start.astimezone(UTC), event.uid))


def _in_feed(event: Event, feed_id: str, ctx: _Context) -> bool:
    if feed_id == "all":
        return True
    if feed_id == "picks":
        return event.is_pick(ctx.picks_threshold)
    if feed_id == MEETINGS_FEED:
        return event.is_meeting
    return feed_id in event.fields


def write_ics_feeds(events: Sequence[Event], out_dir: Path, *, config: AppConfig) -> list[Path]:
    """Write ``<out_dir>/<feed id>.ics`` for all ten FEED_IDS; return the paths.

    The contract is ``seminars.contracts.IcsFeedWriter``. Every event given
    is written, cancelled and past ones included; choosing the events is the
    caller's business. Nothing is written when two events share a uid.
    """
    ordered = _ordered(events)
    ctx = _context(config)
    zone = _vtimezone(ctx, *_years(ordered, ctx))
    rendered = [(event, _vevent(event, ctx)) for event in ordered]
    paths: list[Path] = []
    for feed_id in FEED_IDS:
        chosen = [vevent for event, vevent in rendered if _in_feed(event, feed_id, ctx)]
        path = out_dir / f"{feed_id}.ics"
        write_text_atomic(path, _calendar_text(feed_id, [zone, *chosen], ctx))
        paths.append(path)
    return paths


# --- validation ----------------------------------------------------------------------------

MAX_LINE_OCTETS: Final = 75

_LINE_BREAK: Final = re.compile(r"\r\n|\n|\r")
_NAME: Final = r"[A-Za-z0-9-]+"
_PARAM_VALUE: Final = r'(?:"[^"]*"|[^";:,]*)'
_CONTENT_LINE: Final = re.compile(
    rf"({_NAME})((?:;{_NAME}={_PARAM_VALUE}(?:,{_PARAM_VALUE})*)*):(.*)", re.DOTALL
)
_PARAMETER: Final = re.compile(rf";({_NAME})=({_PARAM_VALUE})")
_FORBIDDEN: Final = re.compile(f"[\x00-\x08\x0a-\x1f\x7f{chr(0xD800)}-{chr(0xDFFF)}]")
_DATE: Final = re.compile(r"\d{8}")
_DATE_TIME: Final = re.compile(r"\d{8}T\d{6}Z?")
_UTC_OFFSET: Final = re.compile(r"[+-]\d{4}(?:\d{2})?")
_URI: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:\S+")
_ESCAPES_ONLY: Final = re.compile(r"(?:[^\\]|\\[\\;,nN])*")
_SEPARATORS: Final = re.compile(r"(?<!\\)(?:\\\\)*[;,]")
_LIST_SEPARATORS: Final = re.compile(r"(?<!\\)(?:\\\\)*;")
_EVENT_STATUSES: Final = frozenset({"TENTATIVE", "CONFIRMED", "CANCELLED"})
_TRANSPARENCIES: Final = re.compile("(?i)OPAQUE|TRANSPARENT")
_BUSY_STATUSES: Final = re.compile("(?i)FREE|TENTATIVE|BUSY|OOF|WORKINGELSEWHERE")
_OBSERVANCES: Final = ("STANDARD", "DAYLIGHT")
_PARSER_DETAIL_CHARS: Final = 200


@dataclass(frozen=True)
class _Line:
    """One unfolded content line; ``number`` is its first line in the document."""

    number: int
    name: str
    params: Mapping[str, str]
    value: str


@dataclass(frozen=True)
class _Component:
    name: str
    number: int
    lines: tuple[_Line, ...]
    parts: tuple[_Component, ...]


def _physical_lines(text: str) -> tuple[list[str], list[str]]:
    """The lines of a document as they are stored, and what is wrong with their endings."""
    lines = _LINE_BREAK.split(text)
    wrong = [number for number, found in enumerate(_LINE_BREAK.findall(text), 1) if found != "\r\n"]
    if lines[-1]:
        wrong.append(len(lines))  # the last line has no line ending at all
    else:
        lines.pop()
    problems = [f"line {wrong[0]}: line ending is not CRLF ({len(wrong)} lines)"] if wrong else []
    for number, line in enumerate(lines, 1):
        octets = len(line.encode("utf-8", "surrogatepass"))
        if octets > MAX_LINE_OCTETS:
            problems.append(f"line {number}: longer than {MAX_LINE_OCTETS} octets ({octets})")
        found = _FORBIDDEN.search(line)
        if found is not None:
            problems.append(f"line {number}: control character U+{ord(found.group()):04X}")
    return lines, problems


def _unfold(lines: Sequence[str]) -> tuple[list[tuple[int, str]], list[str]]:
    """Join continuation lines to the line they continue."""
    unfolded: list[tuple[int, str]] = []
    problems: list[str] = []
    for number, line in enumerate(lines, 1):
        if not line.startswith((" ", "\t")):
            unfolded.append((number, line))
        elif unfolded:
            first, start = unfolded[-1]
            unfolded[-1] = (first, start + line[1:])
        else:
            problems.append(f"line {number}: continuation of nothing")
    return unfolded, problems


def _content_line(number: int, text: str) -> _Line | None:
    found = _CONTENT_LINE.fullmatch(text)
    if found is None:
        return None
    params = {name.upper(): value.strip('"') for name, value in _PARAMETER.findall(found.group(2))}
    return _Line(number=number, name=found.group(1).upper(), params=params, value=found.group(3))


def _tree(lines: Sequence[_Line]) -> tuple[list[_Component], list[str]]:
    """Nest the lines into components; returns the calendars and the nesting problems."""
    roots: list[_Component] = []
    problems: list[str] = []
    open_parts: list[tuple[_Line, list[_Line], list[_Component]]] = []
    for line in lines:
        if not open_parts and (roots or line.name != "BEGIN" or line.value.upper() != "VCALENDAR"):
            what = (
                "content after END:VCALENDAR"
                if roots
                else "the document must start with BEGIN:VCALENDAR"
            )
            problems.append(f"line {line.number}: {what}")
            break
        if line.name == "BEGIN":
            open_parts.append((line, [], []))
        elif line.name == "END":
            begin, own_lines, parts = open_parts.pop()
            if line.value.upper() != begin.value.upper():
                problems.append(
                    f"line {line.number}: END:{ascii_printable(line.value)} "
                    f"does not close BEGIN:{ascii_printable(begin.value)}"
                )
            part = _Component(begin.value.upper(), begin.number, tuple(own_lines), tuple(parts))
            (open_parts[-1][2] if open_parts else roots).append(part)
        else:
            open_parts[-1][1].append(line)
    problems.extend(
        f"line {begin.number}: BEGIN:{ascii_printable(begin.value)} is never closed"
        for begin, _, _ in open_parts
    )
    return roots, problems


def _moment(line: _Line) -> date | None:
    """The value of a DATE or DATE-TIME property; ``None`` when it is not one."""
    try:
        if line.params.get("VALUE", "").upper() == "DATE":
            valid = _DATE.fullmatch(line.value)
            return datetime.strptime(line.value, "%Y%m%d").date() if valid else None
        if not _DATE_TIME.fullmatch(line.value):
            return None
        moment = datetime.strptime(line.value[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    if line.value.endswith("Z"):
        return moment.replace(tzinfo=UTC)
    try:
        return moment.replace(tzinfo=ZoneInfo(line.params["TZID"]))
    except (KeyError, ValueError, OSError):  # no TZID, or one the system does not know
        return moment


def _moment_rule(line: _Line) -> str | None:
    kind = "date" if line.params.get("VALUE", "").upper() == "DATE" else "date-time"
    if _moment(line) is None:
        return f"{line.name} is not a {kind}"
    if line.value.endswith("Z") and "TZID" in line.params:
        return f"{line.name} is UTC with a TZID"
    return None


def _utc_rule(line: _Line) -> str | None:
    valid = line.value.endswith("Z") and isinstance(_moment(line), datetime)
    return None if valid else f"{line.name} is not a UTC date-time"


def _local_rule(line: _Line) -> str | None:
    """The onset of a time zone observance: a date-time without a zone of its own."""
    zoned = line.value.endswith("Z") or "TZID" in line.params
    valid = not zoned and isinstance(_moment(line), datetime)
    return None if valid else f"{line.name} is not a local date-time"


def _text_rule(separators: re.Pattern[str]) -> Callable[[_Line], str | None]:
    def rule(line: _Line) -> str | None:
        if not _ESCAPES_ONLY.fullmatch(line.value):
            return f"{line.name} has an invalid escape"
        if separators.search(line.value):
            return f"{line.name} has an unescaped separator"
        return None

    return rule


def _pattern_rule(pattern: re.Pattern[str], what: str) -> Callable[[_Line], str | None]:
    return lambda line: None if pattern.fullmatch(line.value) else f"{line.name} {what}"


_TEXT: Final = _text_rule(_SEPARATORS)
_RULES: Final[Mapping[tuple[str, str], Callable[[_Line], str | None]]] = {
    ("VCALENDAR", "VERSION"): _pattern_rule(re.compile(r"2\.0"), "must be 2.0"),
    ("VCALENDAR", "CALSCALE"): _pattern_rule(re.compile("(?i)GREGORIAN"), "must be GREGORIAN"),
    ("VEVENT", "DTSTART"): _moment_rule,
    ("VEVENT", "DTEND"): _moment_rule,
    ("VEVENT", "STATUS"): lambda line: (
        None if line.value.upper() in _EVENT_STATUSES else "STATUS is not a status of an event"
    ),
    ("VEVENT", "TRANSP"): _pattern_rule(_TRANSPARENCIES, "is neither OPAQUE nor TRANSPARENT"),
    ("VEVENT", "X-MICROSOFT-CDO-BUSYSTATUS"): _pattern_rule(
        _BUSY_STATUSES, "is not a busy status of Outlook"
    ),
    ("STANDARD", "DTSTART"): _local_rule,
    ("DAYLIGHT", "DTSTART"): _local_rule,
    ("*", "DTSTAMP"): _utc_rule,
    ("*", "LAST-MODIFIED"): _utc_rule,
    ("*", "CREATED"): _utc_rule,
    ("*", "TZOFFSETFROM"): _pattern_rule(_UTC_OFFSET, "is not a UTC offset"),
    ("*", "TZOFFSETTO"): _pattern_rule(_UTC_OFFSET, "is not a UTC offset"),
    ("*", "SEQUENCE"): _pattern_rule(re.compile(r"\d+"), "is not a non-negative integer"),
    ("*", "UID"): _pattern_rule(re.compile(r".+", re.DOTALL), "is empty"),
    ("*", "URL"): _pattern_rule(_URI, "is not a URI"),
    ("*", "SUMMARY"): _TEXT,
    ("*", "DESCRIPTION"): _TEXT,
    ("*", "LOCATION"): _TEXT,
    ("*", "CATEGORIES"): _text_rule(_LIST_SEPARATORS),
}
_REQUIRED: Final[Mapping[str, tuple[str, ...]]] = {
    "VCALENDAR": ("VERSION", "PRODID"),
    "VEVENT": ("UID", "DTSTAMP", "DTSTART"),
    "VTIMEZONE": ("TZID",),
    "STANDARD": ("DTSTART", "TZOFFSETFROM", "TZOFFSETTO"),
    "DAYLIGHT": ("DTSTART", "TZOFFSETFROM", "TZOFFSETTO"),
}
_AT_MOST_ONCE: Final[Mapping[str, tuple[str, ...]]] = {
    "VCALENDAR": ("VERSION", "PRODID", "CALSCALE", "METHOD"),
    "VEVENT": (
        *("UID", "DTSTAMP", "DTSTART", "DTEND", "DURATION", "SUMMARY", "DESCRIPTION"),
        *("LOCATION", "STATUS", "SEQUENCE", "LAST-MODIFIED", "CREATED", "URL", "CLASS"),
        *("TRANSP", "PRIORITY", "ORGANIZER", "GEO", "RECURRENCE-ID"),
        "X-MICROSOFT-CDO-BUSYSTATUS",
    ),
    "VTIMEZONE": ("TZID", "LAST-MODIFIED", "TZURL"),
    "STANDARD": ("DTSTART", "TZOFFSETFROM", "TZOFFSETTO"),
    "DAYLIGHT": ("DTSTART", "TZOFFSETFROM", "TZOFFSETTO"),
}


def _comparable(first: date, second: date) -> bool:
    """True when two parsed moments can be put in order."""
    if isinstance(first, datetime) and isinstance(second, datetime):
        return (first.tzinfo is None) == (second.tzinfo is None)
    return not isinstance(first, datetime) and not isinstance(second, datetime)


def _event_problems(event: _Component, zones: frozenset[str]) -> list[str]:
    """Rules that involve more than one line of a VEVENT."""
    by_name = {line.name: line for line in reversed(event.lines)}
    problems = [
        f"line {line.number}: TZID '{ascii_printable(line.params['TZID'])}' has no VTIMEZONE"
        for line in event.lines
        if "TZID" in line.params and line.params["TZID"] not in zones
    ]
    if "DTEND" in by_name and "DURATION" in by_name:
        problems.append(f"line {event.number}: VEVENT has both DTEND and DURATION")
    start, end = by_name.get("DTSTART"), by_name.get("DTEND")
    first = _moment(start) if start is not None else None
    last = _moment(end) if end is not None else None
    if end is None or first is None or last is None:
        return problems
    if isinstance(first, datetime) != isinstance(last, datetime):
        problems.append(f"line {end.number}: DTEND and DTSTART have different value types")
    elif _comparable(first, last) and not last > first:
        problems.append(f"line {end.number}: DTEND is not later than DTSTART")
    return problems


def _component_problems(component: _Component, zones: frozenset[str]) -> list[str]:
    counts = Counter(line.name for line in component.lines)
    where = f"line {component.number}: {component.name}"
    problems = [
        f"{where} has no {name}" for name in _REQUIRED.get(component.name, ()) if not counts[name]
    ]
    problems.extend(
        f"{where} has more than one {name}"
        for name in _AT_MOST_ONCE.get(component.name, ())
        if counts[name] > 1
    )
    for line in component.lines:
        rule = _RULES.get((component.name, line.name)) or _RULES.get(("*", line.name))
        message = rule(line) if rule is not None else None
        if message is not None:
            problems.append(f"line {line.number}: {message}")
    if component.name == "VEVENT":
        problems.extend(_event_problems(component, zones))
    if component.name == "VTIMEZONE" and not any(p.name in _OBSERVANCES for p in component.parts):
        problems.append(f"{where} has neither STANDARD nor DAYLIGHT")
    for part in component.parts:
        problems.extend(_component_problems(part, zones))
    return problems


def _calendar_problems(calendar: _Component) -> list[str]:
    zones = frozenset(
        line.value
        for part in calendar.parts
        if part.name == "VTIMEZONE"
        for line in part.lines
        if line.name == "TZID"
    )
    problems = _component_problems(calendar, zones)
    if not calendar.parts:
        problems.append(f"line {calendar.number}: VCALENDAR has no component")
    seen: set[str] = set()
    for part in calendar.parts:
        names = {line.name: line for line in part.lines}
        uid = names.get("UID")
        if part.name != "VEVENT" or uid is None or "RECURRENCE-ID" in names:
            continue
        if uid.value in seen:
            problems.append(f"line {uid.number}: UID is used by more than one VEVENT")
        seen.add(uid.value)
    return problems


def _parser_problems(text: str) -> list[str]:
    """What the icalendar library has to say about a document that passed the rules above."""

    def report(detail: str) -> str:
        return f"the icalendar parser reports: {ascii_printable(detail)[:_PARSER_DETAIL_CHARS]}"

    try:
        parsed = icalendar.Calendar.from_ical(text)
    except Exception as error:  # whatever the library raises on odd input is a finding
        return [report(f"{type(error).__name__}: {error}")]
    calendars = parsed if isinstance(parsed, list) else [parsed]
    return [
        report(f"{part.name} {name}: {detail}")
        for calendar in calendars
        for part in calendar.walk()
        for name, detail in part.errors
    ]


def validate_ics(text: str) -> list[str]:
    """Problems found in one ICS document; an empty list means valid.

    The contract is ``seminars.contracts.IcsValidator``. ``text`` has to be
    the document with its line endings as stored (read it without newline
    translation, as ``seminars.textio.read_text`` does): a feed whose lines
    do not end with CRLF is not valid. Every problem is one line of
    printable ASCII that names the line it is about and never quotes a value.
    """
    if not text:
        return ["the document is empty"]
    physical, problems = _physical_lines(text)
    unfolded, folding_problems = _unfold(physical)
    problems.extend(folding_problems)
    lines: list[_Line] = []
    for number, content in unfolded:
        line = _content_line(number, content)
        if line is None:
            problems.append(f"line {number}: not a content line")
        else:
            lines.append(line)
    calendars, nesting_problems = _tree(lines)
    problems.extend(nesting_problems)
    for calendar in calendars:
        problems.extend(_calendar_problems(calendar))
    return problems or _parser_problems(text)
