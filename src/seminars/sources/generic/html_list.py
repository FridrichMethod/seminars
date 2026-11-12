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
* ``allow_empty`` (default false) with ``empty_marker`` (a selector; each
  needs the other): a page where ``selectors.item`` matches nothing gives no
  drafts, not a ``ParseError`` (an upcoming block that is often empty), when
  ``empty_marker`` matches on it and the request stayed on the source's
  hosts. The marker is an element that shows the page is still the listing
  page, such as the cards of past events. Without it the page was
  redesigned or replaced, and a list that was redirected elsewhere (a move
  to another domain, a login page) is not the list either: both are still
  a ``ParseError``.
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
  (group ``native_id``; needs ``required: true``, so that a talk never changes
  its key between runs); ``tag``: the texts of all matches become
  categories. ``speaker``: every match is one speaker (or a list of
  names), named by the text of ``speaker_name`` inside it (default: the
  text of the match) with the text of ``speaker_affiliation`` inside it;
  a row without speakers of its own takes these. ``body``: the elements
  whose text announces the talk; a speaker without an affiliation takes
  the lines below the line of the page that names them (see "Affiliations
  from the text" below). ``required`` (default true): a row whose detail
  page is missing (not fetched, or failed) is dropped, because its page
  holds what the list lacks; with false the row keeps what the list says.
  Links to other hosts are never fetched; a list page that was redirected
  to a host the source does not name raises ``ForeignHostError``.
* ``keep_tags``: with ``detail_page.tag``, keep only rows with one of these tags.

Affiliations from the text: the lines of ``detail_page.body`` are its
matches, each split at ``<br>`` and at the start and the end of a nested
block element; a match without text is an empty line. The line of a
speaker holds their name (the first and the last word of it, at most two
words between them, such as a middle initial) and at most four other words
("Prof. Dr.", "PhD"), so a sentence that mentions the speaker is not it.
Up to three lines below it are the affiliation, joined with ", ", ending
before an empty line, a weekday ("Monday," "Thu."), a month and a day
("December 3"), a line that starts with a digit (a date or a time), a
label ("Talk Title:", "Host:"), a heading of at most four words with a
word such as "Abstract", "Bio" or "Talk" (a whole word: "Stanford Bio-X"
is no heading), prose (more than 15 words, or a lowercase word and a
period at the end), an e-mail address or a link, or a line that names
the speaker again or another speaker of the talk (the next one of an
announcement of several). The first line that names the speaker decides;
when the line right after it names them too (a heading "Seminar: Jane
Doe" above "Jane Doe, PhD"), that line is the line of the name. Only as
many speakers of a talk as the pipeline keeps are looked for (the first
``seminars.pipeline.normalize.MAX_SPEAKERS``).

Times are naive local wall times (the pipeline applies the source's
timezone) unless the page states an offset. A row without a link has no
``url``: the list page is not the page of a talk, so the uid falls back to
the start and the speaker.

The helpers it shares with the other adapters (params validation, text of
HTML nodes, times of day, lists of speaker names, fetching) are in
``seminars.sources.generic.common``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, Final, Literal, get_args
from zoneinfo import ZoneInfo

from pydantic import Field, ValidationError, model_validator
from selectolax.lexbor import LexborHTMLParser, LexborNode

from seminars.http import HttpClient
from seminars.models import (
    MAX_ROLLOVER,
    EventDraft,
    EventType,
    FetchedDocument,
    Model,
    RawPayload,
    SourceConfig,
    SpeakerDraft,
    TimeWindow,
)
from seminars.pipeline.normalize import MAX_SPEAKERS
from seminars.sources.base import (
    ForeignHostError,
    ParseError,
    absolute_url,
    make_payload,
    on_source_host,
    register_adapter,
)
from seminars.sources.generic.common import (
    PATTERN_INPUT_LIMIT,
    Pattern,
    Selector,
    any_match,
    collapse,
    fetch_optional,
    get_document,
    inner_html,
    load_params,
    local_day,
    make_speakers,
    node_lines,
    node_text,
    node_value,
    outside_window,
    parse_day,
    parse_moment,
    search,
    split_range,
    time_range,
)
from seminars.textrules import ascii_printable

logger = logging.getLogger(__name__)

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
    speaker: Selector | None = None
    speaker_name: Selector | None = None
    speaker_affiliation: Selector | None = None
    body: Selector | None = None
    required: bool = True
    max_pages: Annotated[int, Field(ge=1, le=20)] = 10

    @model_validator(mode="after")
    def _check_parts(self) -> DetailParams:
        for name in ("speaker_name", "speaker_affiliation"):
            if getattr(self, name) is not None and self.speaker is None:
                raise ValueError(f"{name} needs detail_page.speaker")
        if self.native_id is not None and not self.required:
            raise ValueError(
                "native_id needs required: true: a row kept without its page would"
                " change its key between runs"
            )
        return self


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
    allow_empty: bool = False
    empty_marker: Selector | None = None
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
        if self.allow_empty and self.empty_marker is None:
            raise ValueError("allow_empty needs empty_marker (an element of the listing page)")
        if self.empty_marker is not None and not self.allow_empty:
            raise ValueError("empty_marker needs allow_empty: true")
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


def has_marker(params: HtmlListParams, document: FetchedDocument) -> bool:
    """True when ``params.empty_marker`` matches on the page (the page is still the list)."""
    marker = params.empty_marker
    return marker is not None and LexborHTMLParser(document.text).css_first(marker) is not None


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
    speakers: tuple[SpeakerDraft, ...] = ()
    lines: tuple[str, ...] = ()  # of detail_page.body; "" is an empty line


_BLOCK_TAGS: Final = frozenset(
    {
        *("address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption"),
        *("footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "ol", "p"),
        *("section", "table", "td", "th", "tr", "ul"),
    }
)
MAX_AFFILIATION_LINES: Final = 3
MAX_AFFILIATION_WORDS: Final = 15  # a longer line is prose: a bio, an abstract
MAX_HEADING_WORDS: Final = 4
MAX_OTHER_WORDS: Final = 4  # on the line of a name: titles and degrees ("Prof. Dr.", "PhD")
_WEEKDAYS: Final = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_NO_AFFILIATION: Final = re.compile(
    r"\s*(?:\d"  # a date or a time: "12/3/26", "11:00 AM"
    rf"|(?:{_WEEKDAYS})\b|(?:mon|tues?|wed|thu|thurs|fri|sat|sun)[.,]"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d"
    r"|[^\W\d][^:]{0,40}:(?:\s|$))",  # a label: "Talk Title:", "Host:"
    re.IGNORECASE,
)
_HEADING_WORD: Final = re.compile(  # a whole word: "Bio", not the start of "Bio-X"
    r"(?<![\w-])(?:abstract|about|bio|biography|biosketch|description|hosts?|hosted|location"
    r"|overview|register|registration|rsvp|summary|talk|title|venue|when|where)(?![\w-])",
    re.IGNORECASE,
)
_SENTENCE_END: Final = re.compile(r"(?:\b[a-z]{2,}[.!?]|\.\.\.)$")  # "We study things."
_CONTACT: Final = ("@", "://", "www.")  # an e-mail address or a link


def text_lines(node: LexborNode) -> list[str]:
    """The non-empty lines below ``node``.

    ``<br>`` and the start of a block element end a line, and so does the end
    of one: text after a nested block is on a line of its own. The walk is in
    document order, so the parent of a node comes before it: the innermost
    block around every element (``node`` at most) is known when its children
    come, and each node is looked at once.
    """
    top = node.mem_id
    blocks: dict[int, int] = {top: top}  # mem_id of an element -> of the block around it
    lines: list[list[str]] = [[]]
    block = top
    for child in node.traverse(include_text=True):
        parent = child.parent
        around = blocks.get(parent.mem_id, top) if parent is not None else top
        if child.is_text_node:
            if around != block:
                lines.append([])
                block = around
            lines[-1].append(child.text_content or "")
        elif child.tag in _BLOCK_TAGS:
            blocks[child.mem_id] = child.mem_id
            lines.append([])
        else:
            blocks[child.mem_id] = around
    return [text for line in lines if (text := collapse("".join(line)))]


def name_pattern(name: str) -> re.Pattern[str] | None:
    """How a line names ``name``: its first and last word, at most two words between."""
    words = name.split()
    if not words:
        return None
    middle = r"(?:\s+\S+){0,2}?\s+" + re.escape(words[-1]) if len(words) > 1 else ""
    return re.compile(rf"(?<!\w){re.escape(words[0])}{middle}(?!\w)", re.IGNORECASE)


def _name_in(line: str, name: str) -> re.Match[str] | None:
    """Where ``line`` names ``name`` (see ``name_pattern``)."""
    pattern = name_pattern(name)
    return None if pattern is None else pattern.search(line[:PATTERN_INPUT_LIMIT])


def names_line(line: str, name: str) -> bool:
    """True when ``line`` is the line of ``name``: the name and at most a few other words."""
    match = _name_in(line, name)
    if match is None:
        return False
    rest = line[: match.start()] + " " + line[match.end() :]
    return len(re.findall(r"\w+", rest)) <= MAX_OTHER_WORDS


def ends_affiliation(line: str, names: Sequence[re.Pattern[str]]) -> bool:
    """True when ``line`` cannot be part of an affiliation of the speakers ``names``.

    That is an empty line, a date, a time, a label, a heading ("Abstract",
    "Speaker Bio"), prose (a sentence, a long line), an e-mail address or a
    link, and a line that names one of the speakers (the same one again, or
    another one); ``names`` are their ``name_pattern``.
    """
    words = line.split()
    return (
        not words
        or _NO_AFFILIATION.match(line) is not None
        or (len(words) <= MAX_HEADING_WORDS and _HEADING_WORD.search(line) is not None)
        or len(words) > MAX_AFFILIATION_WORDS
        or _SENTENCE_END.search(line) is not None
        or any(mark in line for mark in _CONTACT)
        or any(name.search(line[:PATTERN_INPUT_LIMIT]) is not None for name in names)
    )


def affiliation_after(
    lines: Sequence[str], name: str, names: Sequence[re.Pattern[str]] = ()
) -> str | None:
    """The lines below the line that names ``name``; see the module docstring.

    ``names`` are the ``name_pattern`` of the speakers of the talk: a line
    that names one of them ends the affiliation, like a line that names
    ``name`` again.
    """
    own = name_pattern(name)
    if own is None:
        return None
    stops = (own, *names)
    found: list[str] | None = None  # None until a line names the speaker
    for line in lines:
        if not found and names_line(line, name):
            found = []  # the first line of the name, or the next one right after it
        elif found is not None:
            if len(found) == MAX_AFFILIATION_LINES or ends_affiliation(line, stops):
                break
            found.append(line)
    return ", ".join(found) if found else None


def with_affiliations(speakers: Sequence[SpeakerDraft], lines: Sequence[str]) -> list[SpeakerDraft]:
    """``speakers``, each without an affiliation given the one the text has for them.

    Only the first ``MAX_SPEAKERS`` are looked for: the pipeline keeps no
    more speakers of a talk, and a page that names thousands would otherwise
    cost thousands of walks through its text. The others are kept as they
    are. The names of all of them still end an affiliation, each compiled
    once.
    """
    if not lines:
        return list(speakers)
    names = [pattern for speaker in speakers if (pattern := name_pattern(speaker.name))]
    completed: list[SpeakerDraft] = []
    for speaker in speakers[:MAX_SPEAKERS]:
        found = None if speaker.affiliation else affiliation_after(lines, speaker.name, names)
        completed.append(speaker.evolve(affiliation=found) if found else speaker)
    return [*completed, *speakers[MAX_SPEAKERS:]]


def detail_speakers(params: DetailParams, tree: LexborHTMLParser) -> tuple[SpeakerDraft, ...]:
    """One speaker per match of ``params.speaker`` (a list of names is split)."""
    if params.speaker is None:
        return ()
    found: list[SpeakerDraft] = []
    for node in tree.css(params.speaker):
        name = node_text(node.css_first(params.speaker_name) if params.speaker_name else node)
        part = params.speaker_affiliation
        found.extend(make_speakers(name, node_text(node.css_first(part)) if part else None))
    return tuple(found)


def body_lines(params: DetailParams, tree: LexborHTMLParser) -> tuple[str, ...]:
    if params.body is None:
        return ()
    return tuple(line for node in tree.css(params.body) for line in text_lines(node) or [""])


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
        speakers=detail_speakers(params, tree),
        lines=body_lines(params, tree),
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
    row is then dropped, or kept without it when the page is not required,
    and a warning counts such rows). When the list page
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
        fate = "dropped" if detail.required else "kept without it"
        why = "their detail page is on a host the source does not name (params.extra_hosts)"
        logger.warning("source %s: %d row(s) in the window %s: %s", source.id, foreign, fate, why)
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
        stayed = on_source_host(source, document.final_url)
        if not rows and not (params.allow_empty and stayed and has_marker(params, document)):
            moved = "" if stayed else f" (it led to {ascii_printable(document.final_url)})"
            marker = params.empty_marker
            unmarked = f" and empty_marker {marker!r} is not on it" if marker and stayed else ""
            raise ParseError(
                f"source {source.id}: selectors.item {item!r} matches nothing on {url}"
                f"{moved}{unmarked}"
            )
        if rows and not any(all(row.get(name) for name in params.required) for row in rows):
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
            if page is None and params.detail_page.required:
                return None
            if page is not None:
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
            speakers=with_affiliations(row_speakers(row) or detail.speakers, detail.lines),
            location=row.get("location") or detail.location,
            stream_url=absolute_url(document.final_url, row.get("stream_link")),
            abstract=None if row.abstract_html else row.get("summary"),
            abstract_html=row.abstract_html,
            categories=list(detail.tags),
            event_type_hint=row_event_type(params, title),
        )
