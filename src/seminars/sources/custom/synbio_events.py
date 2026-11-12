"""Custom adapter ``synbio_events``: events typed as free text into a site builder page.

The page has no structured data. An event is a block: a heading (its name)
followed by paragraphs up to the next heading. Paragraphs start with labels
typed by hand ("When:", "Where:", "Title:", ...); the rest is description.
``fetch`` requests every URL of the source; ``parse`` reads every block of
every page. A page without any block that has a "When" line is not
understood (``ParseError``), so the LLM fallback can take over.

Keys of ``params`` (an unknown key is an error that names it;
``extra_hosts`` is read by the shared host check):

* ``selectors``: ``container`` (the elements that hold blocks; a block never
  spans two of them), ``heading`` and ``line`` (headings and paragraphs
  inside a container, in document order), ``heading_link`` (links inside a
  heading that may name a speaker). The text of a node is joined without a
  separator and its white space collapsed.
* ``group_headings`` and ``skip_headings``: heading texts that are not the
  name of an event ("Upcoming Events", the page title, a footer). The lines
  after them belong to no block.
* ``heading_id_pattern``: group ``key`` in the ``id`` of a heading. The key
  is the native id of the block's first event and fills ``{key}`` in
  ``anchor_url_template``, the ``url`` of every event of the block.
* ``line_labels``: ``when``, ``where`` and ``title``, each a list of labels
  (compared without case) that a line starts with, followed by a colon.
* ``max_title_length`` (default 160): a "Title" line longer than this is a
  description written under a title label, not the title of the event.
* ``when_range_pattern``: tried first on a "When" line; groups
  ``start_month``, ``start_day``, ``end_month``, ``end_day`` and ``year``
  make an all-day event over several days (the last day inclusive).
* ``when_pattern``: groups ``times``, ``month``, ``day`` and ``year``. An
  empty ``times`` is an all-day event, and so is one that
  ``times_pattern`` does not read.
* ``times_pattern``: groups ``start``, ``start_meridiem``, ``end`` and
  ``end_meridiem``; a start without AM / PM takes the one of the end.
* ``month_aliases``: misspelt month names and their correction; months are
  compared on their first three letters ("Sept").
* ``speaker_heading_patterns``: the first that matches the name of an
  event gives its speaker text (group ``speaker``, raw, for normalization
  to split) and, with a group ``affiliation``, one affiliation for all of
  them. A heading link whose text stands in the name part of the speaker
  text (before any parenthesis, comma or "@") is a speaker with that page.

A block holds one event per "When" line. A second "When" line starts another
event, named by the unlabelled paragraph right before it; its native id is
``<key>/<n>`` for the n-th "When" line. The title of an event is its
"Title" line, or its name when that line is missing, says TBA or is too
long to be a title (it is then part of the description). The
"Where" line is the location as text: no link of it is copied, because
such links can be meeting links with a passcode. Weekdays in "When" lines
are ignored (they do not always fit the date). Times are naive wall times
of the source.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Annotated, Final, Literal

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
    fetch_urls,
    register_adapter,
)
from seminars.sources.generic.common import (
    PATTERN_INPUT_LIMIT,
    Pattern,
    Selector,
    collapse,
    is_tba,
    load_params,
    make_speakers,
    month_number,
    node_text,
    require_groups,
    search,
    time_range,
)
from seminars.textrules import ascii_printable

logger = logging.getLogger(__name__)

MAX_LABEL_LENGTH: Final = 40
LineKind = Literal["when", "where", "title"]
_SPEAKER_SEPARATOR: Final = re.compile(r"\s+(?:&|and)\s+", re.IGNORECASE)
_NAME_END: Final = re.compile(r"[(,@]")


# --- params ------------------------------------------------------------------------------


class SynbioSelectors(Model):
    container: Selector
    heading: Selector
    line: Selector
    heading_link: Selector | None = None


class LineLabels(Model):
    when: list[str]
    where: list[str] = []
    title: list[str] = []

    def kinds(self) -> dict[str, LineKind]:
        """Folded label -> kind of line."""
        found: dict[str, LineKind] = {}
        kinds: tuple[tuple[LineKind, list[str]], ...] = (
            ("when", self.when),
            ("where", self.where),
            ("title", self.title),
        )
        for kind, labels in kinds:
            for label in labels:
                found.setdefault(fold(label), kind)
        return found


class SynbioParams(Model):
    """``params`` of a ``synbio_events`` source; see the module docstring."""

    selectors: SynbioSelectors
    group_headings: list[str] = []
    skip_headings: list[str] = []
    heading_id_pattern: Pattern | None = None
    anchor_url_template: str | None = None
    line_labels: LineLabels
    when_range_pattern: Pattern | None = None
    when_pattern: Pattern
    times_pattern: Pattern
    month_aliases: dict[str, str] = {}
    speaker_heading_patterns: list[Pattern] = []
    max_title_length: Annotated[int, Field(ge=1)] = 160

    @model_validator(mode="after")
    def _check_groups(self) -> SynbioParams:
        require_groups(self.heading_id_pattern, "heading_id_pattern", "key")
        require_groups(
            self.when_range_pattern,
            "when_range_pattern",
            "start_month",
            "start_day",
            "end_month",
            "end_day",
            "year",
        )
        require_groups(self.when_pattern, "when_pattern", "times", "month", "day", "year")
        require_groups(self.times_pattern, "times_pattern", "start")
        for pattern in self.speaker_heading_patterns:
            require_groups(pattern, "speaker_heading_patterns", "speaker")
        if self.anchor_url_template is not None and "{key}" not in self.anchor_url_template:
            raise ValueError("anchor_url_template needs {key}")
        return self


# --- reading the blocks ------------------------------------------------------------------


def fold(text: str | None) -> str:
    return (collapse(text) or "").casefold()


@dataclass(frozen=True)
class Line:
    """One paragraph of a block: its text, and its kind and value when it is labelled."""

    text: str
    kind: LineKind | None = None
    value: str | None = None


@dataclass(frozen=True)
class Block:
    """A heading and the paragraphs after it."""

    name: str
    key: str | None
    links: tuple[tuple[str, str], ...]
    lines: tuple[Line, ...]


def read_line(kinds: Mapping[str, LineKind], text: str) -> Line:
    label, colon, rest = text.partition(":")
    kind = kinds.get(fold(label)) if colon and len(label) <= MAX_LABEL_LENGTH else None
    return Line(text=text, kind=kind, value=collapse(rest) if kind else None)


def heading_key(params: SynbioParams, node: LexborNode) -> str | None:
    identifier = node.attributes.get("id")
    if params.heading_id_pattern is None or not identifier:
        return None
    match = search(params.heading_id_pattern, identifier)
    return collapse(match["key"]) if match is not None else None


def heading_links(
    params: SynbioParams, document: FetchedDocument, node: LexborNode
) -> list[tuple[str, str]]:
    if params.selectors.heading_link is None:
        return []
    links: list[tuple[str, str]] = []
    for link in node.css(params.selectors.heading_link):
        text = node_text(link)
        url = absolute_url(document.final_url, link.attributes.get("href"))
        if text and url:
            links.append((text, url))
    return links


def read_blocks(
    params: SynbioParams, source: SourceConfig, document: FetchedDocument
) -> list[Block]:
    """Every block of a page, in document order."""
    tree = LexborHTMLParser(document.text)
    containers = tree.css(params.selectors.container)
    if not containers:
        selector, url = params.selectors.container, ascii_printable(document.url)
        raise ParseError(f"source {source.id}: {selector!r} matches nothing on {url}")
    kinds = params.line_labels.kinds()
    not_events = {fold(text) for text in (*params.group_headings, *params.skip_headings)}
    nodes = f"{params.selectors.heading}, {params.selectors.line}"
    blocks: list[Block] = []
    for container in containers:
        current: Block | None = None  # its lines are collected in ``lines``
        lines: list[Line] = []
        for node in container.css(nodes):
            text = node_text(node)
            if node.css_matches(params.selectors.heading):
                if current is not None:
                    blocks.append(replace(current, lines=tuple(lines)))
                current, lines = None, []
                if text and fold(text) not in not_events:
                    links = tuple(heading_links(params, document, node))
                    current = Block(text, heading_key(params, node), links, ())
            elif current is not None and text:
                lines.append(read_line(kinds, text))
        if current is not None:
            blocks.append(replace(current, lines=tuple(lines)))
    return blocks


# --- events of a block -------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """The part of a block that describes one event."""

    name: str
    links: tuple[tuple[str, str], ...]
    native_id: str | None
    lines: tuple[Line, ...]


def segments(block: Block) -> list[Segment]:
    """One segment per "When" line; a later one is named by the paragraph before it.

    The first segment starts with the block, so labelled lines before its
    "When" line count. A later one starts at its "When" line; the unlabelled
    paragraph right before that line is its name and belongs to no segment.
    """
    whens = [index for index, line in enumerate(block.lines) if line.kind == "when"]
    starts: list[tuple[int, bool]] = []  # (first line, named by the line before it)
    for number, index in enumerate(whens):
        previous = whens[number - 1] if number else -1
        named = number > 0 and index - 1 > previous and block.lines[index - 1].kind is None
        starts.append((0 if number == 0 else index, named))
    found: list[Segment] = []
    for number, (start, named) in enumerate(starts):
        if number + 1 < len(starts):
            following, following_named = starts[number + 1]
            end = following - 1 if following_named else following
        else:
            end = len(block.lines)
        name, links = (block.lines[start - 1].text, ()) if named else (block.name, block.links)
        native_id = block.key if number == 0 or block.key is None else f"{block.key}/{number + 1}"
        found.append(Segment(name, links, native_id, block.lines[start:end]))
    return found


def aliased_month(params: SynbioParams, name: str | None) -> int | None:
    """1 to 12 for a month name, after ``month_aliases`` (a misspelt name of the page)."""
    aliases = {fold(key): value for key, value in params.month_aliases.items()}
    return month_number(aliases.get(fold(name), name or ""))


def make_date(
    params: SynbioParams, month: str | None, day: str | None, year: str | None
) -> date | None:
    number = aliased_month(params, month)
    if number is None or not (day or "").isdigit() or not (year or "").isdigit():
        return None
    try:
        return date(int(year or ""), number, int(day or ""))
    except ValueError:
        return None


def range_days(params: SynbioParams, match: re.Match[str]) -> tuple[date, date] | None:
    """First and last day of a "When" line that spans several days."""
    last = make_date(params, match["end_month"], match["end_day"], match["year"])
    if last is None:
        return None
    start_month = aliased_month(params, match["start_month"]) or 0
    year = last.year - 1 if start_month > last.month else last.year  # "December 30 to January 2"
    first = make_date(params, match["start_month"], match["start_day"], str(year))
    return (first, last) if first is not None and first <= last else None


def when_times(
    params: SynbioParams, text: str
) -> tuple[datetime | date, datetime | date | None] | None:
    """Start and end of a "When" line; ``None`` when it cannot be read."""
    if params.when_range_pattern is not None:
        match = search(params.when_range_pattern, text)
        if match is not None:
            return range_days(params, match)
    match = search(params.when_pattern, text)
    if match is None:
        return None
    day = make_date(params, match["month"], match["day"], match["year"])
    if day is None:
        return None
    times = search(params.times_pattern, collapse(match["times"]) or "")
    if times is None:
        return day, None
    groups = times.groupdict()
    start = f"{groups['start']}{groups.get('start_meridiem') or ''}"
    end = f"{groups['end']}{groups.get('end_meridiem') or ''}" if groups.get("end") else None
    begin, finish = time_range(start, end)
    if begin is None:
        return day, None
    moment = datetime.combine(day, begin)
    later = datetime.combine(day, finish) if finish is not None else None
    return moment, later if later is not None and later > moment else None


def names_a_speaker(speaker_text: str, link_text: str) -> bool:
    """True when ``link_text`` stands in the name part of one person of ``speaker_text``."""
    for part in _SPEAKER_SEPARATOR.split(speaker_text[:PATTERN_INPUT_LIMIT]):
        name = _NAME_END.split(part, maxsplit=1)[0]
        if link_text in name:
            return True
    return False


def event_speakers(params: SynbioParams, segment: Segment) -> tuple[list[SpeakerDraft], str | None]:
    """Structured speakers and the raw speaker text of an event, from its name."""
    match = next(
        (m for p in params.speaker_heading_patterns if (m := search(p, segment.name))), None
    )
    speaker_text = collapse(match["speaker"]) if match is not None else None
    if speaker_text is None or match is None:
        return [], None
    linked = {
        name: SpeakerDraft(name=name, url=url)
        for name, url in segment.links
        if names_a_speaker(speaker_text, name)
    }
    affiliation = collapse(match.groupdict().get("affiliation"))
    if affiliation is not None:
        return [*linked.values(), *make_speakers(speaker_text, affiliation)], None
    return list(linked.values()), speaker_text


def first_value(lines: Sequence[Line], kind: LineKind) -> str | None:
    return next((line.value for line in lines if line.kind == kind and line.value), None)


def make_draft(
    params: SynbioParams, source: SourceConfig, document: FetchedDocument, segment: Segment
) -> EventDraft | None:
    when = next((line.text for line in segment.lines if line.kind == "when"), None)
    times = when_times(params, when) if when is not None else None
    if times is None:
        logger.warning("source %s: an event with an unreadable When line was skipped", source.id)
        return None
    title = first_value(segment.lines, "title")
    long_title = title is not None and len(title) > params.max_title_length
    speakers, speaker_text = event_speakers(params, segment)
    key = (segment.native_id or "").split("/", 1)[0]
    template = params.anchor_url_template
    link = template.replace("{key}", key) if template is not None and key else None
    description = [
        line.text
        for line in segment.lines
        if line.kind is None or (long_title and line.kind == "title")
    ]
    if long_title or (title is not None and is_tba(title)):
        title = None
    return EventDraft(
        source_id=source.id,
        extraction="html",
        native_id=segment.native_id,
        url=absolute_url(document.final_url, link),
        title=title or segment.name,
        start=times[0],
        end=times[1],
        speakers=speakers,
        speaker_text=speaker_text,
        location=first_value(segment.lines, "where"),
        abstract="\n".join(description) or None,
    )


@register_adapter("synbio_events")
class SynbioEventsAdapter:
    """Free-text event blocks; see the module docstring."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        load_params(SynbioParams, source)
        return fetch_urls(http, source, window=window, label="page")

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        params = load_params(SynbioParams, source)
        pages = payload.labelled("page")
        if not pages:
            raise ParseError(f"source {source.id}: the payload has no page")
        drafts: list[EventDraft] = []
        events = 0
        for document in pages:
            for block in read_blocks(params, source, document):
                for segment in segments(block):
                    events += 1
                    try:
                        draft = make_draft(params, source, document, segment)
                    except ValidationError:
                        logger.warning("source %s: a malformed event was skipped", source.id)
                        continue
                    if draft is not None:
                        drafts.append(draft)
        if not events:
            raise ParseError(f"source {source.id}: no block on the page has a When line")
        return drafts
