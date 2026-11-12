"""Custom adapter ``nlp_seminar``: a schedule page with one HTML table per term.

The page lists the talks of each term in a table, newest term first. A row
has a date without a year, a speaker and a title; the year, the weekday,
the time of day and the venue are in the heading above the table, the same
for every row. Each talk may have a details page with the affiliation of the
speaker, the abstract and its own time and venue lines.

``fetch`` requests the schedule (every URL of the source) and the details
pages of the talks that start on or after the day the schedule was fetched
and lie in the run window, at most ``max_detail_pages``, only on the hosts of
the source. ``parse`` turns every talk of the newest ``max_sections`` tables
into a draft, past talks included: a schedule whose newest term is over
still parses, so zero talks in the run window is not mistaken for a page
that could not be read. A page without any table is not understood
(``ParseError``).

Keys of ``params`` (an unknown key is an error that names it;
``extra_hosts`` is read by the shared host check):

* ``max_sections`` (default 2): how many tables, newest first, are read.
* ``max_detail_pages`` (default 12, 0 to fetch none).
* ``selectors``: ``section`` (one table per term), ``section_heading`` (the
  nearest preceding sibling of a table that matches it and has text is its
  heading), ``row`` (inside the table), and inside a row ``date``,
  ``speaker``, ``speaker_link`` (its ``href`` is the speaker's page),
  ``title`` and ``detail_link`` (its ``href`` is the details page). A row
  without a date or without a details link is a note ("Semester Break"),
  not a talk.
* ``heading_patterns``, each searched in every line of the heading (lines
  end at ``<br>``): ``term`` (groups ``year`` and optionally ``term``),
  ``time`` (groups ``start``, ``end`` and ``weekday``) and ``venue`` (group
  ``venue``). A table whose heading has no year is skipped.
* ``date_pattern``: groups ``month`` (a month name, compared on its first
  three letters) and ``day`` in the date cell.
* ``title_suffix_pattern``: removed from the end of the title ("(details)").
* ``detail_selectors``: ``root`` (default: the whole page), and inside it
  ``title``, ``speaker`` (a link: its text and ``href``), ``affiliation``
  and ``abstract`` (first match each).
* ``detail_patterns``: ``date_line`` and ``venue_line`` (group ``value``),
  searched in every line of the root. The times of day before the first
  comma of the date line ("11:00am - 12:00 noon PT, Thursday, May 28th")
  replace those of the heading; the venue line replaces its venue.

The heading's year can be wrong. When the heading names a weekday and most
dates of its table fall on that weekday in the year before or after instead,
the whole table is moved to that year; a talk on another day of the week
keeps the year of its table. A table of one or two dates moves only when the
other year also puts them nearer to the day the page was fetched. Times are
naive wall times of the source; a talk without a time of day is an all-day
draft. The draft's ``url`` is the details page, which identifies the talk.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Final
from zoneinfo import ZoneInfo

from pydantic import Field, ValidationError, model_validator
from selectolax.lexbor import LexborHTMLParser, LexborNode

from seminars.http import HttpClient
from seminars.models import (
    EventDraft,
    FetchedDocument,
    Model,
    RawPayload,
    SourceConfig,
    SpeakerDraft,
    TimeWindow,
)
from seminars.sources.base import (
    ParseError,
    absolute_url,
    make_payload,
    on_source_host,
    register_adapter,
)
from seminars.sources.generic.common import (
    Pattern,
    Selector,
    collapse,
    fetch_optional,
    get_document,
    inner_html,
    load_params,
    local_day,
    month_number,
    node_text,
    outside_window,
    require_groups,
    search,
    split_names,
    split_range,
    time_range,
)
from seminars.textrules import ascii_printable

logger = logging.getLogger(__name__)

WEEKDAYS: Final = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_HEADING_DISTANCE: Final = 50
"""Siblings looked at before a table when searching for its heading."""
MIN_VOTING_DAYS: Final = 3  # dated rows a table needs to move its year on weekdays alone


# --- params ------------------------------------------------------------------------------


class NlpSelectors(Model):
    section: Selector
    section_heading: Selector
    row: Selector
    date: Selector
    speaker: Selector
    speaker_link: Selector | None = None
    title: Selector
    detail_link: Selector


class HeadingPatterns(Model):
    term: Pattern
    time: Pattern | None = None
    venue: Pattern | None = None

    @model_validator(mode="after")
    def _check_groups(self) -> HeadingPatterns:
        require_groups(self.term, "heading_patterns.term", "year")
        require_groups(self.time, "heading_patterns.time", "start")
        require_groups(self.venue, "heading_patterns.venue", "venue")
        return self


class DetailSelectors(Model):
    root: Selector | None = None
    title: Selector | None = None
    speaker: Selector | None = None
    affiliation: Selector | None = None
    abstract: Selector | None = None


class DetailPatterns(Model):
    date_line: Pattern | None = None
    venue_line: Pattern | None = None

    @model_validator(mode="after")
    def _check_groups(self) -> DetailPatterns:
        require_groups(self.date_line, "detail_patterns.date_line", "value")
        require_groups(self.venue_line, "detail_patterns.venue_line", "value")
        return self


class NlpParams(Model):
    """``params`` of an ``nlp_seminar`` source; see the module docstring."""

    max_sections: Annotated[int, Field(ge=1, le=50)] = 2
    max_detail_pages: Annotated[int, Field(ge=0, le=20)] = 12
    selectors: NlpSelectors
    heading_patterns: HeadingPatterns
    date_pattern: Pattern
    title_suffix_pattern: Pattern | None = None
    detail_selectors: DetailSelectors = DetailSelectors()
    detail_patterns: DetailPatterns = DetailPatterns()

    @model_validator(mode="after")
    def _check_groups(self) -> NlpParams:
        require_groups(self.date_pattern, "date_pattern", "month", "day")
        return self


# --- text helpers ------------------------------------------------------------------------


def text_lines(node: LexborNode | None) -> list[str]:
    """The text below ``node`` cut at every ``<br>`` and line break, each line collapsed."""
    if node is None:
        return []
    lines: list[list[str]] = [[]]
    for child in node.traverse(include_text=True):
        if child.tag == "br":
            lines.append([])
        elif child.is_text_node:
            parts = (child.text_content or "").split("\n")
            lines[-1].append(parts[0])
            lines.extend([part] for part in parts[1:])
    return [text for line in lines if (text := collapse("".join(line)))]


def first_group(pattern: Pattern | None, lines: Sequence[str], group: str) -> str | None:
    """The group ``group`` of the first line that ``pattern`` matches."""
    if pattern is None:
        return None
    for line in lines:
        if (match := search(pattern, line)) is not None:
            return collapse(match.groupdict().get(group))
    return None


def weekday_number(name: str | None) -> int | None:
    """0 (Monday) to 6 for an English weekday name; ``None`` for anything else."""
    key = (name or "")[:3].lower()
    return WEEKDAYS.index(key) if key in WEEKDAYS else None


# --- the schedule ------------------------------------------------------------------------


@dataclass(frozen=True)
class Heading:
    """What the heading of a table says about all of its rows."""

    year: int
    weekday: int | None = None
    start: str | None = None
    end: str | None = None
    venue: str | None = None


@dataclass(frozen=True)
class Talk:
    """One row of the schedule that is a talk."""

    day: date
    title: str | None
    speaker: str | None
    speaker_url: str | None
    url: str
    heading: Heading


def section_heading(params: NlpParams, table: LexborNode) -> LexborNode | None:
    """The nearest preceding sibling of ``table`` that is a heading and has text."""
    node = table.prev
    for _ in range(MAX_HEADING_DISTANCE):
        if node is None:
            return None
        if node.is_element_node:
            if node.css_matches(params.selectors.section):
                return None  # the table before has no heading of its own either
            if node.css_matches(params.selectors.section_heading) and node_text(node):
                return node
        node = node.prev
    return None


def read_heading(params: NlpParams, node: LexborNode | None) -> Heading | None:
    """The year, weekday, times and venue of a heading; ``None`` without a year."""
    lines = text_lines(node)
    patterns = params.heading_patterns
    year = first_group(patterns.term, lines, "year")
    if year is None or not year.isdigit():
        return None
    return Heading(
        year=int(year),
        weekday=weekday_number(first_group(patterns.time, lines, "weekday")),
        start=first_group(patterns.time, lines, "start"),
        end=first_group(patterns.time, lines, "end"),
        venue=first_group(patterns.venue, lines, "venue"),
    )


def month_day(params: NlpParams, text: str | None) -> tuple[int, int] | None:
    """Month and day of a date cell; ``None`` when it names no date."""
    match = search(params.date_pattern, text) if text else None
    if match is None:
        return None
    month, day = month_number(match["month"]), match["day"]
    if month is None or not day or not day.isdigit():
        return None
    return month, int(day)


def make_day(year: int, day: tuple[int, int] | None) -> date | None:
    if day is None:
        return None
    try:
        return date(year, *day)
    except ValueError:  # "Feb 30"
        return None


def table_year(heading: Heading, days: Sequence[tuple[int, int]], today: date) -> int:
    """The heading's year, or the year before or after when most ``days`` fall on its weekday there.

    A date never falls on the same weekday in two neighbouring years: a typo in the
    year moves most dates of a table off the weekday, a talk on another day only one.
    A table of fewer than MIN_VOTING_DAYS dates, often the newest one, moves only when
    that also brings its dates nearer to ``today``: a talk or two on another weekday
    must not move the coming term out of the run.
    """
    if heading.weekday is None:
        return heading.year

    def dates(year: int) -> list[date]:
        return [day for day in (make_day(year, pair) for pair in days) if day is not None]

    def fitting(year: int) -> int:
        return sum(1 for day in dates(year) if day.weekday() == heading.weekday)

    def distance(year: int) -> int:
        return sum(abs((day - today).days) for day in dates(year))

    for year in (heading.year - 1, heading.year + 1):
        if 2 * fitting(year) <= len(days):
            continue
        if len(days) >= MIN_VOTING_DAYS or distance(year) < distance(heading.year):
            return year
    return heading.year


def strip_suffix(params: NlpParams, title: str | None) -> str | None:
    if title is None or params.title_suffix_pattern is None:
        return title
    match = search(params.title_suffix_pattern, title)
    return collapse(title[: match.start()]) if match is not None else title


def read_talk(
    params: NlpParams,
    document: FetchedDocument,
    row: LexborNode,
    heading: Heading,
    day: date | None,
) -> Talk | None:
    selectors = params.selectors
    link = row.css_first(selectors.detail_link)
    url = absolute_url(document.final_url, link.attributes.get("href") if link else None)
    if url is None or day is None:
        return None
    speaker_link = row.css_first(selectors.speaker_link) if selectors.speaker_link else None
    href = speaker_link.attributes.get("href") if speaker_link is not None else None
    return Talk(
        day=day,
        title=strip_suffix(params, node_text(row.css_first(selectors.title))),
        speaker=node_text(row.css_first(selectors.speaker)),
        speaker_url=absolute_url(document.final_url, href),
        url=url,
        heading=heading,
    )


def read_schedule(params: NlpParams, source: SourceConfig, document: FetchedDocument) -> list[Talk]:
    """The talks of the newest ``max_sections`` tables of a schedule page."""
    tree = LexborHTMLParser(document.text)
    tables = tree.css(params.selectors.section)
    url = ascii_printable(document.url)
    if not tables:
        raise ParseError(f"source {source.id}: no {params.selectors.section!r} on {url}")
    talks: list[Talk] = []
    readable = 0
    today = local_day(document.fetched_at, ZoneInfo(source.timezone))
    for table in tables[: params.max_sections]:
        heading = read_heading(params, section_heading(params, table))
        if heading is None:
            logger.warning(
                "source %s: a table without a year in its heading was skipped", source.id
            )
            continue
        readable += 1
        rows = [
            (row, month_day(params, node_text(row.css_first(params.selectors.date))))
            for row in table.css(params.selectors.row)
        ]
        year = table_year(heading, [day for _, day in rows if day is not None], today)
        for row, day in rows:
            if (talk := read_talk(params, document, row, heading, make_day(year, day))) is not None:
                talks.append(talk)
    if not readable:
        raise ParseError(f"source {source.id}: no table heading on {url} names a year")
    return talks


# --- details pages -----------------------------------------------------------------------


@dataclass(frozen=True)
class Details:
    """What a details page adds to a row."""

    title: str | None = None
    speaker: str | None = None
    speaker_url: str | None = None
    affiliation: str | None = None
    abstract_html: str | None = None
    start: str | None = None
    end: str | None = None
    venue: str | None = None


def read_details(params: NlpParams, document: FetchedDocument) -> Details:
    tree = LexborHTMLParser(document.text)
    selectors = params.detail_selectors
    root = (tree.css_first(selectors.root) if selectors.root else None) or tree.body

    def first(selector: str | None) -> LexborNode | None:
        return root.css_first(selector) if root is not None and selector else None

    lines = text_lines(root)
    date_line = first_group(params.detail_patterns.date_line, lines, "value")
    start, end = split_range((date_line or "").split(",", 1)[0])
    speaker = first(selectors.speaker)
    href = speaker.attributes.get("href") if speaker is not None else None
    return Details(
        title=node_text(first(selectors.title)),
        speaker=node_text(speaker),
        speaker_url=absolute_url(document.final_url, href),
        affiliation=node_text(first(selectors.affiliation)),
        abstract_html=inner_html(first(selectors.abstract)),
        start=start,
        end=end,
        venue=first_group(params.detail_patterns.venue_line, lines, "value"),
    )


def talk_times(talk: Talk, details: Details) -> tuple[datetime | date, datetime | None]:
    """Start and end of a talk: the details page's times, else the heading's, else all day."""
    begin, finish = time_range(details.start, details.end)
    if begin is None:
        begin, finish = time_range(talk.heading.start, talk.heading.end)
    if begin is None:
        return talk.day, None
    start = datetime.combine(talk.day, begin)
    end = datetime.combine(talk.day, finish) if finish is not None else None
    return start, end if end is not None and end > start else None


def talk_speakers(talk: Talk, details: Details) -> list[SpeakerDraft]:
    """The speakers of the row, else of the details page; one speaker gets the link and place."""
    name, url = (talk.speaker, talk.speaker_url) if talk.speaker else (details.speaker, None)
    names = split_names(name)
    if len(names) != 1:
        return [SpeakerDraft(name=name) for name in names]
    return [
        SpeakerDraft(name=names[0], affiliation=details.affiliation, url=url or details.speaker_url)
    ]


def make_draft(source: SourceConfig, talk: Talk, details: Details) -> EventDraft:
    start, end = talk_times(talk, details)
    return EventDraft(
        source_id=source.id,
        extraction="html",
        url=talk.url,
        title=talk.title or details.title,
        start=start,
        end=end,
        speakers=talk_speakers(talk, details),
        location=details.venue or talk.heading.venue,
        abstract_html=details.abstract_html,
    )


def detail_targets(
    params: NlpParams,
    source: SourceConfig,
    schedules: Sequence[FetchedDocument],
    window: TimeWindow,
) -> list[str]:
    """Details pages of talks from the fetch day on, in the window and on the source's hosts."""
    zone = ZoneInfo(source.timezone)
    wanted: dict[str, None] = {}  # ordered, without repeats
    foreign = 0
    for document in schedules:
        today = local_day(document.fetched_at, zone)
        try:
            talks = read_schedule(params, source, document)
        except ParseError:  # parse reports it; the payload stays whole for the pipeline
            continue
        for talk in talks:
            if talk.day < today or outside_window(talk.day, None, window, zone):
                continue
            if not on_source_host(source, talk.url):
                foreign += 1
            else:
                wanted[talk.url] = None
    if foreign:
        logger.warning(
            "source %s: %d details page(s) on a host the source does not name were not fetched",
            source.id,
            foreign,
        )
    return list(wanted)[: params.max_detail_pages]


@register_adapter("nlp_seminar")
class NlpSeminarAdapter:
    """A schedule of one table per term; see the module docstring."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        params = load_params(NlpParams, source)
        schedules = [get_document(http, source, url, "schedule") for url in source.all_urls]
        targets = detail_targets(params, source, schedules, window)
        details = fetch_optional(http, source, targets, "details")
        return make_payload(source, [*schedules, *details], window=window)

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        params = load_params(NlpParams, source)
        schedules = payload.labelled("schedule")
        if not schedules:
            raise ParseError(f"source {source.id}: the payload has no schedule page")
        pages: Mapping[str, FetchedDocument] = {
            document.url: document for document in payload.labelled("details")
        }
        read: dict[str, Details] = {}  # a page that several talks share is read once
        drafts: list[EventDraft] = []
        for document in schedules:
            for talk in read_schedule(params, source, document):
                if talk.url in pages and talk.url not in read:
                    read[talk.url] = read_details(params, pages[talk.url])
                details = read.get(talk.url, Details())
                try:
                    drafts.append(make_draft(source, talk, details))
                except ValidationError:
                    logger.warning("source %s: a malformed talk was skipped", source.id)
        return drafts
