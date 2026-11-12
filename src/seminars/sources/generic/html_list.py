"""Generic adapter ``html_list``: talk lists described by CSS selectors in the YAML.

Nothing in this module knows a site. A source names its list page(s) as
``url`` / ``urls`` and describes one talk of the page in ``params``::

    params:
      selectors:
        item: "ol.talk-list > li.talk"   # one element per talk (required)
        title: "div.talk-title"
        date: "time.talk-date"
      date_attribute: datetime
      date_format: "%Y-%m-%d"

Keys of ``params`` (an unknown key is an error that names it;
``extra_hosts`` is read by the shared host check):

* ``selectors.item``: one element per talk. On every list page it must match
  at least once, and at least one item must have every field of
  ``require``; otherwise the page is not understood (``ParseError``).
* ``selectors.<field>``: evaluated inside the item, first match. The fields
  are ``title``, ``link`` (detail page), ``date``, ``time``, ``speaker``,
  ``affiliation``, ``location``, ``abstract``, ``stream_link`` (recording or
  livestream), ``region`` and ``summary`` (a teaser, the abstract when there
  is no other).
* ``<field>_attribute``: read this attribute of the element instead of its text.
* ``<field>_pattern``: a regular expression searched in the value. A group
  named like the field gives its value (no match: the field is empty); every
  other named group sets the field of that name unless that field has a
  value of its own, so ``speaker_pattern`` can set ``affiliation``. Besides
  the fields, groups may be ``end_date`` (last day of a multi-day talk),
  ``start`` and ``end`` (times of day) and ``meridiem`` (one AM or PM for both
  times). The first pattern in field order that sets a group wins.
* ``date_format``: ``strptime`` format of ``date`` and ``end_date``. Without
  it a date is ISO 8601 or anything ``dateutil`` reads. A date without a
  year (in either case) gets the year that puts it closest to the fetch.
* ``time_format``: ``strptime`` format of ``start`` and ``end``. Without it,
  "4:30", "4:30pm", "4:30 PM", "16:30", "4 p.m.", "noon", "12:00 noon" work. A
  ``time`` value without ``start`` / ``end`` groups is read as a range ("4:30
  - 5:30pm PT", en dash or "to" work too); "TBA" / "TBD" mean no time. One
  AM / PM completes the other end ("11am - 1:30" ends at 13:30); an end before
  the start is on the next day if at most 6 hours later, else the row has none.
* ``default_start_time`` / ``default_end_time``: local time of day ("12:00")
  for rows without one. A row without any time is an all-day talk; with
  ``end_date`` it lasts to that day (inclusive).
* ``require``: fields a row needs to count as a talk (default: ``date``).
* ``skip_title_patterns`` / ``skip_summary_patterns``: rows whose title or
  summary matches one of them are dropped; ``exclude_regions``: rows whose
  region is one of these names are dropped.
* ``title_event_types``: ``{word: event type}``; the first word found in the
  title (whole word, any case) sets the event type.
* ``detail_page``: fetch the page of the ``link`` of every kept row in the
  run window that stays on the source's hosts (at most ``max_pages``,
  default 10). ``times``: elements whose ``datetime`` attributes (or texts)
  are the start and the end, replacing the row's times; ``location``;
  ``native_id`` with ``native_id_attribute`` and ``native_id_pattern``
  (group ``native_id``); ``tag``: the texts of all matches become
  categories. A row whose detail page is missing (not fetched, or failed)
  is dropped: its detail page holds what the list lacks. Links to other
  hosts are never fetched; a list page that was redirected to a host the
  source does not name raises ``ForeignHostError``.
* ``keep_tags``: with ``detail_page.tag``, keep only rows with one of these tags.

Times are naive local wall times (the pipeline applies the source's
timezone) unless the page states an offset. A row without a link has no
``url``: the list page is not the page of a talk, so the uid falls back to
the start and the speaker.

The module also holds the helpers that ``rss`` and ``jsonld`` share: params
validation (``load_params``, ``AdapterConfigError``), text of HTML nodes,
times of day and lists of speaker names.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, Final, Literal, get_args
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser
from pydantic import AfterValidator, Field, ValidationError, model_validator
from selectolax.lexbor import LexborHTMLParser, LexborNode, SelectolaxError

from seminars.http import HttpClient, HttpError
from seminars.models import (
    EventDraft,
    EventType,
    FetchedDocument,
    Model,
    RawPayload,
    SourceConfig,
    SpeakerDraft,
    TimeWindow,
)
from seminars.pipeline.times import MAX_ROLLOVER  # the rule of normalization, so both agree
from seminars.sources.base import (
    AdapterError,
    ForeignHostError,
    ParseError,
    absolute_url,
    make_payload,
    on_source_host,
    register_adapter,
)
from seminars.textrules import ascii_printable

logger = logging.getLogger(__name__)

PATTERN_INPUT_LIMIT: Final = 2000
"""Characters of a page text that a pattern from the YAML is applied to; bounds the work."""

FieldName = Literal[
    "title",
    "link",
    "date",
    "time",
    "speaker",
    "affiliation",
    "location",
    "abstract",
    "stream_link",
    "region",
    "summary",
]
FIELDS: Final[tuple[str, ...]] = get_args(FieldName)
EXTRA_GROUPS: Final = ("end_date", "start", "end", "meridiem")


# --- params ------------------------------------------------------------------------------


class AdapterConfigError(AdapterError, ValueError):
    """The ``params`` of a source do not fit its adapter: an unknown key or a wrong value."""


def _describe(problem: Mapping[str, Any]) -> str:
    location = ".".join(["params", *(str(part) for part in problem["loc"])])
    detail = "unknown key" if problem["type"] == "extra_forbidden" else problem["msg"]
    return f"{location}: {detail}"


def load_params[P: Model](model: type[P], source: SourceConfig) -> P:
    """``source.params`` validated as ``model``; ``extra_hosts`` is left to the host check."""
    data = {key: value for key, value in source.params.items() if key != "extra_hosts"}
    try:
        return model.model_validate(data)
    except ValidationError as error:
        problems = "; ".join(_describe(problem) for problem in error.errors())
        message = f"source {source.id}: params of adapter {source.adapter}: {problems}"
        raise AdapterConfigError(ascii_printable(message)) from None


def check_selector(value: str) -> str:
    """Validate a CSS selector with the parser that will use it."""
    try:
        LexborHTMLParser("<html><body></body></html>").css(value)
    except (SelectolaxError, ValueError) as error:
        raise ValueError(f"invalid CSS selector {value!r}") from error
    return value


Selector = Annotated[str, AfterValidator(check_selector)]
Pattern = re.Pattern[str]


# --- text helpers ------------------------------------------------------------------------


def collapse(text: str | None) -> str | None:
    """Runs of whitespace as one space, stripped; ``None`` for empty text."""
    return None if text is None else " ".join(text.split()) or None


def node_text(node: LexborNode | None) -> str | None:
    """All text below ``node``, whitespace collapsed."""
    return None if node is None else collapse(node.text(deep=True, strip=False))


def node_lines(node: LexborNode | None) -> str | None:
    """Text below ``node`` with every ``<br>`` as a line break, lines joined with ", "."""
    if node is None:
        return None
    lines: list[list[str]] = [[]]
    for child in node.traverse(include_text=True):
        if child.tag == "br":
            lines.append([])
        elif child.is_text_node:
            lines[-1].append(child.text_content or "")
    parts = [collapse("".join(line)) for line in lines]
    return ", ".join(part for part in parts if part) or None


def node_value(node: LexborNode | None, attribute: str | None) -> str | None:
    """The attribute ``attribute`` of ``node`` (collapsed), or its text without one."""
    if node is None or attribute is None:
        return node_text(node)
    return collapse(node.attributes.get(attribute))


def inner_html(node: LexborNode | None) -> str | None:
    """The markup inside ``node`` without ``<summary>`` captions; ``None`` when blank."""
    if node is None:
        return None
    copy = node.clone()
    for caption in copy.css("summary"):
        caption.decompose()
    markup = copy.inner_html
    return markup if isinstance(markup, str) and markup.strip() else None


def search(pattern: Pattern, text: str) -> re.Match[str] | None:
    """``pattern.search`` over at most PATTERN_INPUT_LIMIT characters of ``text``."""
    return pattern.search(text[:PATTERN_INPUT_LIMIT])


def any_match(patterns: Sequence[Pattern], text: str | None) -> bool:
    return text is not None and any(search(pattern, text) for pattern in patterns)


_NAME_LIST: Final = re.compile(r"\s(?:and|&)\s", re.IGNORECASE)
_NAME_SEPARATOR: Final = re.compile(r"\s*,\s*(?:(?:and|&)\s+)?|\s+(?:and|&)\s+", re.IGNORECASE)


def split_names(text: str | None) -> list[str]:
    """Names of a list such as "A and B" or "A, B, and C"; one name otherwise.

    A comma alone does not split ("Jane Doe, Jr."); it does in a list that
    also has "and" or "&" between two names.
    """
    value = collapse(text)
    if value is None:
        return []
    if not _NAME_LIST.search(value[:PATTERN_INPUT_LIMIT]):
        return [value]
    names = (collapse(part) for part in _NAME_SEPARATOR.split(value[:PATTERN_INPUT_LIMIT]))
    return [name for name in names if name]


def make_speakers(names: str | None, affiliation: str | None) -> list[SpeakerDraft]:
    """One speaker per name of ``names``, all with ``affiliation``."""
    return [
        SpeakerDraft(name=name, affiliation=collapse(affiliation)) for name in split_names(names)
    ]


# --- dates and times ---------------------------------------------------------------------

_CLOCK: Final = re.compile(
    r"(?P<hour>\d{1,2})(?:[:.h](?P<minute>\d{2}))?\s*(?:(?P<meridiem>[ap])\.?\s*m\.?)?",
    re.IGNORECASE,
)
_ZONE_SUFFIX: Final = re.compile(r"\s*\(?\b(?!am\b|pm\b|noon\b)[a-z]{2,4}\)?\s*$", re.IGNORECASE)
_RANGE: Final = re.compile(r"\s*[-\u2013\u2014]\s*|\s+to\s+", re.IGNORECASE)
_TBA: Final = re.compile(r"\W*(?:tba|tbd|tbc|to be (?:announced|determined|confirmed))\W*", re.I)
_NAMED_TIMES: Final = {"noon": time(12, 0), "midnight": time(0, 0)}
_NOON: Final = re.compile(r"12(?:[:.h]00)?\s*noon", re.IGNORECASE)


def is_tba(text: str | None) -> bool:
    """True for "TBA", "TBD", "to be announced" and the like."""
    return text is not None and _TBA.fullmatch(text[:PATTERN_INPUT_LIMIT]) is not None


def with_meridiem(value: time, meridiem: str) -> time:
    """``value`` read on a 12-hour clock with ``meridiem`` ("am" / "pm", any case)."""
    hour = value.hour % 12 + (12 if meridiem.lower().startswith("p") else 0)
    return value.replace(hour=hour)


def parse_clock(text: str | None, fmt: str | None = None) -> tuple[time, str | None] | None:
    """A time of day and its meridiem ("am" / "pm", ``None`` when not written)."""
    value = collapse(text)
    if value is None or is_tba(value):
        return None
    if fmt is not None:
        try:
            parsed = datetime.strptime(value, fmt).time()
        except ValueError:
            return None
        return parsed, ("pm" if parsed.hour >= 12 else "am") if "%p" in fmt else None
    value = _ZONE_SUFFIX.sub("", value[:PATTERN_INPUT_LIMIT])
    if value.lower() in _NAMED_TIMES:
        return _NAMED_TIMES[value.lower()], "pm" if value.lower() == "noon" else "am"
    if _NOON.fullmatch(value):  # "12:00 noon"
        return time(12, 0), "pm"
    match = _CLOCK.fullmatch(value)
    if match is None:
        return None
    hour, minute = int(match["hour"]), int(match["minute"] or 0)
    meridiem = match["meridiem"]
    if hour > 23 or minute > 59 or (meridiem is not None and not 1 <= hour <= 12):
        return None
    clock = time(hour, minute)
    if meridiem is None:
        return clock, None
    return with_meridiem(clock, meridiem), f"{meridiem.lower()}m"


def split_range(text: str | None) -> tuple[str | None, str | None]:
    """ "4:30 - 5:30pm" as its two ends; a single time as ``(time, None)``."""
    value = collapse(text)
    if value is None:
        return None, None
    parts = _RANGE.split(value[:PATTERN_INPUT_LIMIT], maxsplit=1)
    return parts[0], parts[1] if len(parts) > 1 else None


def time_range(
    start: str | None, end: str | None, *, meridiem: str | None = None, fmt: str | None = None
) -> tuple[time | None, time | None]:
    """Start and end times of day; a meridiem written on one end completes the other.

    When only the end says AM or PM (or ``meridiem`` does) the start gets the
    same one, unless that would put it after the end: "11:30 - 1:00 PM"
    starts at 11:30 in the morning. When only the start says it (noon is PM),
    an end before the start is read as PM if that puts it after the start:
    "11am - 1:30" ends at 13:30, "4:30 PM - 3:30" keeps 3:30.
    """
    first, second = parse_clock(start, fmt), parse_clock(end, fmt)
    shared = meridiem or (second[1] if second else None)
    end_time = None
    if second is not None:
        end_time = second[0] if second[1] or shared is None else with_meridiem(second[0], shared)
    if first is None:
        return None, end_time
    start_time = first[0]
    if first[1] is None and shared is not None and start_time.hour <= 12:
        start_time = with_meridiem(first[0], shared)
        if end_time is not None and start_time > end_time:
            start_time = with_meridiem(first[0], "am")
    elif first[1] and shared is None and end_time is not None and end_time < start_time:
        afternoon = with_meridiem(end_time, "pm")
        end_time = afternoon if end_time.hour and afternoon > start_time else end_time
    return start_time, end_time


def parse_day(text: str | None, fmt: str | None, reference: date) -> date | None:
    """A calendar date from ``text``; see the module docstring for the rules."""
    value = collapse(text)
    if value is None or is_tba(value):
        return None
    if fmt is None:
        if len(value) == 10:
            with contextlib.suppress(ValueError):
                return date.fromisoformat(value)
        text = value[:PATTERN_INPUT_LIMIT]
        return closest_day(
            lambda year: date_parser.parse(text, default=datetime(year, 1, 1)).date(), reference
        )
    if "%Y" in fmt or "%y" in fmt:
        with contextlib.suppress(ValueError):
            return datetime.strptime(value, fmt).date()
        return None
    return closest_day(
        lambda year: datetime.strptime(f"{value} {year}", f"{fmt} %Y").date(), reference
    )


def closest_day(read: Callable[[int], date], reference: date) -> date | None:
    """Of the dates ``read`` gives for the years around ``reference``, the closest one.

    A text with a year of its own gives the same date for every year; one
    without gets the year that puts it closest to ``reference``.
    """
    candidates: list[date] = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        with contextlib.suppress(ValueError, OverflowError):
            if (day := read(year)) not in candidates:
                candidates.append(day)
    return min(candidates, key=lambda day: abs(day - reference), default=None)


def parse_moment(text: str | None) -> datetime | date | None:
    """An ISO 8601 date (all day, "20261117" too) or date and time; ``None`` for anything else."""
    value = collapse(text)
    if value is None:
        return None
    for reader in (date.fromisoformat, datetime.fromisoformat):
        with contextlib.suppress(ValueError):
            return reader(value)
    return None


def local_day(moment: datetime | date, zone: ZoneInfo) -> date:
    """The local calendar day of ``moment`` (a naive datetime is local already).

    A moment at the very end of the calendar, which has no local day, keeps its own date.
    """
    if isinstance(moment, datetime) and moment.tzinfo is not None:
        with contextlib.suppress(OverflowError):
            return moment.astimezone(zone).date()
    return moment.date() if isinstance(moment, datetime) else moment


def outside_window(
    start: datetime | date | None, end: datetime | date | None, window: TimeWindow, zone: ZoneInfo
) -> bool:
    """True when a talk certainly lies outside ``window`` (unknown starts are inside)."""
    if start is None:
        return False
    first = local_day(start, zone)
    last = max(first, local_day(end, zone)) if end is not None else first
    return last < local_day(window.start, zone) or first >= local_day(window.end, zone)


# --- fetching ----------------------------------------------------------------------------


def get_document(http: HttpClient, source: SourceConfig, url: str, label: str) -> FetchedDocument:
    """``http.get(url)`` labelled; ``ForeignHostError`` before any request off the source."""
    if not on_source_host(source, url):
        raise ForeignHostError(source.id, url)
    return http.get(url).evolve(label=label)


def fetch_optional(
    http: HttpClient, source: SourceConfig, urls: Iterable[str], label: str
) -> list[FetchedDocument]:
    """Documents of ``urls`` that could be fetched; a failure is logged and skipped."""
    documents = []
    for url in urls:
        try:
            documents.append(get_document(http, source, url, label))
        except HttpError as error:
            logger.warning("source %s: %s page skipped: %s", source.id, label, error)
    return documents


# --- the adapter -------------------------------------------------------------------------


class Selectors(Model):
    item: Selector
    title: Selector | None = None
    link: Selector | None = None
    date: Selector | None = None
    time: Selector | None = None
    speaker: Selector | None = None
    affiliation: Selector | None = None
    location: Selector | None = None
    abstract: Selector | None = None
    stream_link: Selector | None = None
    region: Selector | None = None
    summary: Selector | None = None


class DetailParams(Model):
    times: Selector | None = None
    location: Selector | None = None
    native_id: Selector | None = None
    native_id_attribute: str | None = None
    native_id_pattern: Pattern | None = None
    tag: Selector | None = None
    max_pages: Annotated[int, Field(ge=1, le=20)] = 10


class HtmlListParams(Model):
    """``params`` of an ``html_list`` source; see the module docstring."""

    selectors: Selectors
    patterns: dict[str, Pattern] = {}  # filled from the <field>_pattern keys
    attributes: dict[str, str] = {}  # filled from the <field>_attribute keys
    date_format: str | None = None
    time_format: str | None = None
    default_start_time: time | None = None
    default_end_time: time | None = None
    require: list[FieldName] = ["date"]
    skip_title_patterns: list[Pattern] = []
    skip_summary_patterns: list[Pattern] = []
    exclude_regions: list[str] = []
    title_event_types: dict[str, EventType] = {}
    keep_tags: list[str] = []
    detail_page: DetailParams | None = None

    @model_validator(mode="before")
    @classmethod
    def _collect_field_options(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "patterns" in data or "attributes" in data:
            raise ValueError("write <field>_pattern and <field>_attribute keys instead")
        rest: dict[str, Any] = {}
        options: dict[str, dict[str, Any]] = {"pattern": {}, "attribute": {}}
        for key, value in data.items():
            name, _, kind = str(key).rpartition("_")
            if kind in options and name in FIELDS:
                options[kind][name] = value
            else:
                rest[str(key)] = value
        return {**rest, "patterns": options["pattern"], "attributes": options["attribute"]}

    @model_validator(mode="after")
    def _check_fields(self) -> HtmlListParams:
        configured = {name for name in FIELDS if getattr(self.selectors, name) is not None}
        for kind, names in (("pattern", self.patterns), ("attribute", self.attributes)):
            for name in names:
                if name not in configured:
                    raise ValueError(f"{name}_{kind} needs selectors.{name}")
        missing = [name for name in self.require if name not in configured]
        if missing and self.require != ["date"]:
            raise ValueError(f"require names fields without a selector: {', '.join(missing)}")
        for name, pattern in self.patterns.items():
            if unknown := sorted(set(pattern.groupindex) - {*FIELDS, *EXTRA_GROUPS}):
                raise ValueError(f"{name}_pattern has unknown groups: {', '.join(unknown)}")
        if "date" not in configured and (self.detail_page is None or not self.detail_page.times):
            raise ValueError("a source needs selectors.date or detail_page.times")
        return self

    @property
    def required(self) -> list[str]:
        """``require`` without fields that have no selector (the default names ``date``)."""
        return [name for name in self.require if getattr(self.selectors, name) is not None]


@dataclass(frozen=True)
class Row:
    """The values of one item of a list page, after patterns."""

    values: Mapping[str, str]
    abstract_html: str | None = None
    groups: Mapping[str, str] = field(default_factory=dict)
    speakers: tuple[tuple[str, str | None], ...] = ()  # (names, affiliation) per match

    def get(self, name: str) -> str | None:
        return self.values.get(name) or self.groups.get(name)


def read_value(
    params: HtmlListParams, name: str, node: LexborNode | None
) -> tuple[str | None, dict[str, str]]:
    """The value of field ``name`` in ``node`` and the other groups of its pattern."""
    raw = node_value(node, params.attributes.get(name))
    pattern = params.patterns.get(name)
    if raw is None or pattern is None:
        return raw, {}
    match = search(pattern, raw)
    if match is None:
        return None, {}
    found = {key: text for key, value in match.groupdict().items() if (text := collapse(value))}
    return (found.pop(name, None) if name in pattern.groupindex else raw), found


def read_row(params: HtmlListParams, item: LexborNode) -> Row:
    """Read every configured field of ``item`` and apply the patterns.

    Every match of ``selectors.speaker`` is a speaker (or a list of names),
    with the affiliation its pattern finds; other fields use the first match.
    """
    values: dict[str, str] = {}
    groups: dict[str, str] = {}
    speakers: list[tuple[str, str | None]] = []
    abstract_html = None
    for name in FIELDS:
        selector = getattr(params.selectors, name)
        if selector is None:
            continue
        nodes = item.css(selector) if name == "speaker" else [item.css_first(selector)]
        for index, node in enumerate(nodes):
            value, found = read_value(params, name, node)
            if name == "speaker" and value:
                speakers.append((value, found.pop("affiliation", None)))
            if index == 0:
                for key, text in found.items():
                    groups.setdefault(key, text)
                if value:
                    values[name] = value
        if name == "abstract":
            abstract_html = inner_html(nodes[0])
    return Row(values=values, abstract_html=abstract_html, groups=groups, speakers=tuple(speakers))


def read_rows(params: HtmlListParams, document: FetchedDocument) -> list[Row]:
    tree = LexborHTMLParser(document.text)
    return [read_row(params, item) for item in tree.css(params.selectors.item)]


def keep_row(params: HtmlListParams, row: Row) -> bool:
    """False for rows the exclusion keys drop and rows without the required fields."""
    if any_match(params.skip_title_patterns, row.get("title")):
        return False
    if any_match(params.skip_summary_patterns, row.get("summary")):
        return False
    region = row.get("region")
    if region is not None and region in {collapse(name) for name in params.exclude_regions}:
        return False
    return all(row.get(name) for name in params.required)


def row_times(
    params: HtmlListParams, row: Row, reference: date
) -> tuple[datetime | date | None, datetime | date | None]:
    """Start and end of a row from its date, time and the default times."""
    day = parse_day(row.get("date"), params.date_format, reference)
    if day is None:
        return None, None
    last = parse_day(row.get("end_date"), params.date_format, reference)
    start_text, end_text = row.groups.get("start"), row.groups.get("end")
    if start_text is None and end_text is None:
        start_text, end_text = split_range(row.values.get("time"))
    begin, finish = time_range(
        start_text, end_text, meridiem=row.groups.get("meridiem"), fmt=params.time_format
    )
    if begin is None and params.default_start_time is not None:
        begin, finish = params.default_start_time, params.default_end_time
    if begin is None:
        return day, last if last is not None and last > day else None
    start = datetime.combine(day, begin)
    if finish is None:
        return start, None
    end = datetime.combine(last or day, finish)
    if end > start:
        return start, end
    if end.date() == start.date() < date.max:  # "11pm - 1am" ends the next day
        end += timedelta(days=1)
    return start, end if start < end <= start + MAX_ROLLOVER else None


def row_speakers(row: Row) -> list[SpeakerDraft]:
    """The speakers of a row; the row's affiliation fills in where a match has none."""
    fallback = row.get("affiliation")
    speakers: list[SpeakerDraft] = []
    for names, affiliation in row.speakers:
        speakers.extend(make_speakers(names, affiliation or fallback))
    return speakers


def row_event_type(params: HtmlListParams, title: str | None) -> EventType | None:
    for word, event_type in params.title_event_types.items():
        if title and re.search(rf"\b{re.escape(word)}\b", title, re.IGNORECASE):
            return event_type
    return None


@dataclass(frozen=True)
class Detail:
    """What a detail page adds to a row."""

    start: datetime | date | None = None
    end: datetime | date | None = None
    location: str | None = None
    native_id: str | None = None
    tags: tuple[str, ...] = ()


def read_detail(params: DetailParams, document: FetchedDocument) -> Detail:
    tree = LexborHTMLParser(document.text)
    nodes = tree.css(params.times) if params.times else []
    moments = [parse_moment(node.attributes.get("datetime") or node_text(node)) for node in nodes]
    native_id = None
    if params.native_id is not None:
        native_id = node_value(tree.css_first(params.native_id), params.native_id_attribute)
        if native_id is not None and params.native_id_pattern is not None:
            match = search(params.native_id_pattern, native_id)
            native_id = (match.groupdict().get("native_id") or match[0]) if match else None
    tag_nodes = tree.css(params.tag) if params.tag else []
    tags = tuple(text for node in tag_nodes if (text := node_text(node)))
    return Detail(
        start=moments[0] if moments else None,
        end=moments[1] if len(moments) > 1 else None,
        location=node_lines(tree.css_first(params.location)) if params.location else None,
        native_id=native_id,
        tags=tags,
    )


def detail_targets(
    params: HtmlListParams,
    detail: DetailParams,
    source: SourceConfig,
    documents: Sequence[FetchedDocument],
    window: TimeWindow,
) -> list[str]:
    """Detail pages to fetch: kept rows in the window with a link on the source's hosts.

    A link to another host is a third party's page: it is not fetched (the
    row is then dropped, and a warning counts such rows). When the list page
    itself was redirected to a host the source does not name, every link on
    it is foreign, and that is ``ForeignHostError``.
    """
    zone = ZoneInfo(source.timezone)
    wanted: list[str] = []
    foreign = 0
    for document in documents:
        reference = local_day(document.fetched_at, zone)
        for row in read_rows(params, document):
            url = absolute_url(document.final_url, row.get("link"))
            if url is None or not keep_row(params, row):
                continue
            if outside_window(*row_times(params, row, reference), window, zone):
                continue
            if not on_source_host(source, document.final_url):
                raise ForeignHostError(source.id, document.final_url)
            if not on_source_host(source, url):
                foreign += 1
            elif url not in wanted:
                wanted.append(url)
    if foreign:
        logger.warning(
            "source %s: %d row(s) in the window dropped: their detail page is on a host "
            "the source does not name (params.extra_hosts)",
            source.id,
            foreign,
        )
    return wanted[: detail.max_pages]


@register_adapter("html_list")
class HtmlListAdapter:
    """List pages read with the selectors of ``params``; see the module docstring."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        params = load_params(HtmlListParams, source)
        lists = [get_document(http, source, url, "list") for url in source.all_urls]
        details: list[FetchedDocument] = []
        if params.detail_page is not None:
            targets = detail_targets(params, params.detail_page, source, lists, window)
            details = fetch_optional(http, source, targets, "detail")
        return make_payload(source, [*lists, *details], window=window)

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        params = load_params(HtmlListParams, source)
        lists = payload.labelled("list")
        if not lists:
            raise ParseError(f"source {source.id}: the payload has no list page")
        details = {document.url: document for document in payload.labelled("detail")}
        reference = local_day(payload.fetched_at, ZoneInfo(source.timezone))
        drafts: list[EventDraft] = []
        for document in lists:
            rows = read_rows(params, document)
            self._check_page(params, source, document, rows)
            for row in rows:
                try:
                    draft = self._draft(params, source, document, row, details, reference)
                except (ValidationError, OverflowError) as error:
                    kind = type(error).__name__
                    logger.warning("source %s: a malformed row was skipped (%s)", source.id, kind)
                    continue
                if draft is not None:
                    drafts.append(draft)
        return drafts

    @staticmethod
    def _check_page(
        params: HtmlListParams, source: SourceConfig, document: FetchedDocument, rows: list[Row]
    ) -> None:
        url, item = ascii_printable(document.url), params.selectors.item
        if not rows:
            raise ParseError(
                f"source {source.id}: selectors.item {item!r} matches nothing on {url}"
            )
        if not any(all(row.get(name) for name in params.required) for row in rows):
            needed = ", ".join(params.required)
            raise ParseError(f"source {source.id}: no item on {url} has all of: {needed}")

    @staticmethod
    def _draft(
        params: HtmlListParams,
        source: SourceConfig,
        document: FetchedDocument,
        row: Row,
        details: Mapping[str, FetchedDocument],
        reference: date,
    ) -> EventDraft | None:
        if not keep_row(params, row):
            return None
        url = absolute_url(document.final_url, row.get("link"))
        detail = Detail()
        if params.detail_page is not None:
            page = details.get(url) if url is not None else None
            if page is None:
                return None
            detail = read_detail(params.detail_page, page)
            if params.keep_tags and not set(detail.tags) & set(params.keep_tags):
                return None
        start, end = row_times(params, row, reference)
        if detail.start is not None:
            start, end = detail.start, detail.end
        if start is None:
            return None
        title = row.get("title")
        return EventDraft(
            source_id=source.id,
            extraction="html",
            native_id=detail.native_id,
            url=url,
            title=title,
            start=start,
            end=end,
            speakers=row_speakers(row),
            location=row.get("location") or detail.location,
            stream_url=absolute_url(document.final_url, row.get("stream_link")),
            abstract=None if row.abstract_html else row.get("summary"),
            abstract_html=row.abstract_html,
            categories=list(detail.tags),
            event_type_hint=row_event_type(params, title),
        )
