"""Custom adapter ``hai_events``: events embedded in the server component payload of a page.

The list page is rendered by Next.js. Its inline scripts carry the React server component
payload ("flight" data), which holds the events as JSON objects; the same events are also
rendered as cards. Location and speakers are only on the detail page of an event.

``fetch`` requests every URL of the source (the list) and, with ``fetch_details: upcoming``,
the detail pages of the kept events that start on or after the day the list was fetched and
lie in the run window (at most ``max_detail_pages``). An event with an organiser address
(``keys.organiser_url``) has none: its own page only forwards there, and the organiser's
page has a layout of its own, so neither is requested, whatever its host. Nor is a detail
page on another host (a warning counts those). Such events are built from the list alone.
``parse`` reads the payload (the cards when it cannot be read) and completes each event from
its detail page when one was fetched and is usable.

Keys of ``params`` (an unknown key is an error that names it; ``extra_hosts`` is read by the
shared host check):

* ``flight``: how to read the payload.

  - ``script_pattern`` (group ``json_string``): the scripts whose text it matches carry one
    JSON string each; they are decoded and joined in document order.
  - The joined text is cut into rows on its UTF-8 bytes, from the start: a row begins with
    ``row_start_pattern`` (group ``id``, may be empty). A row that begins with
    ``text_row_pattern`` (groups ``id`` and ``length``, hexadecimal) holds exactly
    ``length`` bytes after it and is followed directly by the next row; every other row ends
    at a line break. A row whose body matches ``special_row_pattern`` is not JSON; every
    other row with an id is one JSON value.
  - String values: ``undefined_marker`` is a missing value, ``escaped_dollar_prefix`` starts
    a text that begins with "$" (one "$" is dropped), ``reference_pattern`` (group ``id``,
    optional ``path``) is the value of another row, or a value inside it reached key by key
    (``:``; ``props`` in a React element is its fourth item). Any other string that starts
    with "$" belongs to the component tree and counts as missing.
  - ``event_marker_key``: an object with this key is an event. ``upcoming_key`` and
    ``previous_key``: dotted paths (from any object that has the first key) of the lists
    that hold the events. When neither is found, the payload cannot be read.
  - ``keys``: dotted paths inside an event (``name[]`` steps into every item of a list):
    ``title``, ``start``, ``end``, ``link`` (the event's page), ``summary_html``,
    ``type_label``, ``topics`` (categories) and ``organiser_url``.

* ``times_are_local``: the times of the payload carry an offset ("Z") but are wall times of
  the source; the offset is dropped.
* ``type_labels``: type label -> event type. ``keep_type_labels``: only events with one of
  these labels are returned (empty: all).
* ``title_separator``: in a title "<prefix><separator><rest>" the prefix is the speakers
  when it names those of the detail page or, without them, reads like person names (then it
  is the speaker text, for normalization to split); otherwise it is the series. The title is
  the rest.
* ``card_selectors`` (``item``, ``title``, ``link``, ``type``, ``date``, ``time``,
  ``summary``), ``card_date_format`` and ``card_time_pattern`` (groups ``start`` and
  ``end``): the cards, read when the payload cannot be. A card without a time is an all-day
  event.
* ``fetch_details`` (``upcoming`` or ``never``, default) and ``max_detail_pages`` (default
  10).
* ``detail_redirect_selector``: a page with a match only forwards elsewhere. Such a page,
  one without ``detail_selectors.title`` and a failed request leave the event as the list
  describes it.
* ``detail_selectors``: ``title``, ``meta_row`` (label and value elements), ``person_list``
  with ``person_list_title``, ``person`` cards with ``person_name``, ``person_title`` (the
  affiliation; several are joined with "; ") and ``person_link``, ``stream_link`` (the
  address to attend online) and ``description`` (the abstract).
* ``detail_meta_labels``: the labels of the ``date`` (its end time fills in a missing end),
  ``location``, ``status`` ("cancelled" or "postponed" cancel the event) and ``topics``
  rows.
* ``speaker_list_titles``: headings of the person lists that are speakers (compared without
  case); other lists (moderators) are not.

An event's ``url`` is its page on the site, also when it has an organiser address, which
several events can share. Times without an offset are wall times of the source.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import islice
from typing import Annotated, Any, Final, Literal
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, Field, ValidationError, model_validator
from selectolax.lexbor import LexborHTMLParser, LexborNode

from seminars.http import HttpClient
from seminars.models import (
    EventDraft,
    EventStatus,
    EventType,
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
from seminars.sources.generic.html_list import (
    PATTERN_INPUT_LIMIT,
    Pattern,
    Selector,
    collapse,
    fetch_optional,
    get_document,
    inner_html,
    load_params,
    local_day,
    node_text,
    outside_window,
    parse_clock,
    parse_day,
    parse_moment,
    search,
    split_range,
    time_range,
)
from seminars.sources.generic.jsonld import walk
from seminars.textrules import ascii_printable, without_surrogates

logger = logging.getLogger(__name__)

MAX_REFERENCE_DEPTH: Final = 8
"""References resolved inside one another; a deeper one counts as missing."""
MAX_REFERENCE_LENGTH: Final = 256
MAX_LIST_ITEMS: Final = 500
"""Items of one list in the payload that are looked at."""
MAX_EVENTS: Final = 1000  # events taken from one payload (the page shows 20)
# Characters kept of a text of an event, cut before anything else: many events can refer
# to one long text and each keeps a copy (normalization keeps less). A longer link is missing.
MAX_TEXT_LENGTH: Final = PATTERN_INPUT_LIMIT  # title, times, label, links
MAX_SUMMARY_LENGTH: Final = 20_000
MAX_TOPIC_LENGTH: Final = 200
MAX_TOPICS: Final = 50  # of one event
_PENDING: Final = object()  # marks a reference that is being resolved
_STATUS_CANCELLED: Final = re.compile(r"cancel|postpone", re.IGNORECASE)
_NAME_SEPARATOR: Final = re.compile(r"\s*(?:&|,|\band\b)\s*", re.IGNORECASE)
_NICKNAME: Final = re.compile(r"\([^()]{0,40}\)")
_INITIAL: Final = re.compile(r"[A-Z]\.")


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_PARTICLES: Final = _words("al bin da de del della den der di du el ibn la le ten ter van von y zu")
_NOT_NAME_WORDS: Final = _words(
    "center centre chat club college colloquium conference conversation council data day "
    "department economy forum foundation governance group hub initiative institute lab labs "
    "lecture lectures network office panel policy program programme research school science "
    "sciences seminar seminars series society studies summit symposium talk talks university week "
    "workshop"
)


# --- params ------------------------------------------------------------------------------


def _require_groups(pattern: Pattern | None, name: str, *groups: str) -> None:
    if pattern is not None and (missing := [g for g in groups if g not in pattern.groupindex]):
        raise ValueError(f"{name} needs the group(s) {', '.join(missing)}")


def _check_path(value: str) -> str:
    names = [step.removesuffix("[]") for step in value.split(".")]
    if any(not name or "[" in name for name in names):
        raise ValueError(f"invalid key path {value!r}")
    return value


KeyPath = Annotated[str, AfterValidator(_check_path)]


class FlightKeys(Model):
    title: KeyPath
    start: KeyPath
    end: KeyPath | None = None
    link: KeyPath
    summary_html: KeyPath | None = None
    type_label: KeyPath | None = None
    topics: KeyPath | None = None
    organiser_url: KeyPath | None = None


class FlightParams(Model):
    script_pattern: Pattern
    row_start_pattern: Pattern
    text_row_pattern: Pattern
    special_row_pattern: Pattern
    reference_pattern: Pattern
    escaped_dollar_prefix: Annotated[str, Field(min_length=1)] = "$$"
    undefined_marker: Annotated[str, Field(min_length=1)] = "$undefined"
    event_marker_key: Annotated[str, Field(min_length=1)]
    upcoming_key: KeyPath
    previous_key: KeyPath
    keys: FlightKeys

    @model_validator(mode="after")
    def _check_patterns(self) -> FlightParams:
        _require_groups(self.script_pattern, "flight.script_pattern", "json_string")
        _require_groups(self.row_start_pattern, "flight.row_start_pattern", "id")
        _require_groups(self.text_row_pattern, "flight.text_row_pattern", "id", "length")
        _require_groups(self.reference_pattern, "flight.reference_pattern", "id")
        for pattern in (self.row_start_pattern, self.text_row_pattern):
            if not pattern.pattern.isascii():
                raise ValueError("the row patterns must be ASCII: they are matched on bytes")
        return self


class CardSelectors(Model):
    item: Selector
    title: Selector
    link: Selector
    type: Selector | None = None
    date: Selector
    time: Selector | None = None
    summary: Selector | None = None


class DetailSelectors(Model):
    title: Selector
    meta_row: Selector | None = None
    person_list: Selector | None = None
    person_list_title: Selector | None = None
    person: Selector | None = None
    person_name: Selector | None = None
    person_title: Selector | None = None
    person_link: Selector | None = None
    stream_link: Selector | None = None
    description: Selector | None = None


class MetaLabels(Model):
    date: str | None = None
    location: str | None = None
    status: str | None = None
    topics: str | None = None


class HaiParams(Model):
    """``params`` of a ``hai_events`` source; see the module docstring."""

    flight: FlightParams
    times_are_local: bool = False
    type_labels: dict[str, EventType] = {}
    keep_type_labels: list[str] = []
    title_separator: Annotated[str, Field(min_length=1)] | None = None
    card_selectors: CardSelectors | None = None
    card_date_format: str | None = None
    card_time_pattern: Pattern | None = None
    fetch_details: Literal["upcoming", "never"] = "never"
    max_detail_pages: Annotated[int, Field(ge=0, le=20)] = 10
    detail_redirect_selector: Selector | None = None
    detail_selectors: DetailSelectors | None = None
    detail_meta_labels: MetaLabels = MetaLabels()
    speaker_list_titles: list[str] = []

    @model_validator(mode="after")
    def _check_consistency(self) -> HaiParams:
        _require_groups(self.card_time_pattern, "card_time_pattern", "start")
        if self.fetch_details != "never" and self.detail_selectors is None:
            raise ValueError("fetch_details needs detail_selectors")
        return self

    def keeps(self, label: str | None) -> bool:
        kept = {collapse(name) for name in self.keep_type_labels}
        return not kept or collapse(label) in kept

    def event_type(self, label: str | None) -> EventType | None:
        types: dict[str | None, EventType] = {
            collapse(name): value for name, value in self.type_labels.items()
        }
        return types.get(collapse(label)) if label else None


# --- the flight payload ------------------------------------------------------------------


class FlightError(ValueError):
    """The payload cannot be cut into rows."""


@dataclass(frozen=True)
class TextRow:
    """The text of a text row: taken as it is, never read as a reference."""

    text: str


def flight_text(params: FlightParams, tree: LexborHTMLParser) -> str | None:
    """The joined JSON strings of the payload scripts; ``None`` when the page has none."""
    parts: list[str] = []
    for script in tree.css("script"):
        match = params.script_pattern.search(script.text(deep=True, strip=False))
        if match is None:
            continue
        try:
            value = json.loads(match["json_string"])
        except (ValueError, RecursionError):
            continue
        if isinstance(value, str):
            parts.append(without_surrogates(value))
    return "".join(parts) if parts else None


def bytes_pattern(pattern: Pattern) -> re.Pattern[bytes]:
    return re.compile(pattern.pattern.encode("ascii"), pattern.flags & ~re.UNICODE)


def cut_rows(params: FlightParams, text: str | None) -> dict[str, Any]:
    """Row id -> JSON value or ``TextRow``; ``FlightError`` when there is nothing to cut."""
    if text is None:
        raise FlightError("the page has no payload")
    data = text.encode("utf-8")
    row_start = bytes_pattern(params.row_start_pattern)
    text_row = bytes_pattern(params.text_row_pattern)
    rows: dict[str, Any] = {}
    position = 0
    while position < len(data):
        match = text_row.match(data, position)
        if match is not None and match.end() > position:
            try:
                end = match.end() + int(match["length"], 16)
            except ValueError as error:
                raise FlightError(f"text row length at byte {position}") from error
            text = data[match.end() : end].decode("utf-8", "replace")
            rows[match["id"].decode("ascii")] = TextRow(text)
            position = end
            continue
        match = row_start.match(data, position)
        if match is None or match.end() == position:
            raise FlightError(f"no row starts at byte {position}")
        newline = data.find(b"\n", match.end())
        end = len(data) if newline < 0 else newline
        identifier, body = match["id"].decode("ascii"), data[match.end() : end]
        head = body[:PATTERN_INPUT_LIMIT].decode("utf-8", "replace")
        if identifier and params.special_row_pattern.search(head) is None:
            with contextlib.suppress(ValueError, RecursionError):  # not JSON: skipped
                rows[identifier] = without_surrogates(json.loads(body))
        position = end + 1
    return rows


@dataclass(frozen=True)
class Flight:
    """The rows of a payload and the rules for its string values.

    Each reference is resolved once, so the work stays linear in the payload, and the
    texts taken from it are cut, so events that share a long text hold short copies. A
    cycle is missing, and so is a reference not yet resolved when met MAX_REFERENCE_DEPTH deep.
    """

    params: FlightParams
    rows: Mapping[str, Any]
    resolved: dict[str, Any] = field(default_factory=dict, init=False, repr=False, compare=False)

    def value(self, value: Any, depth: int = 0) -> Any:
        """``value`` with a reference resolved; ``None`` for markers and tree values."""
        if not isinstance(value, str) or not value.startswith("$"):
            return value
        if value.startswith(self.params.escaped_dollar_prefix):
            return value[1 : MAX_SUMMARY_LENGTH + 1]  # no text is kept longer
        if value == self.params.undefined_marker or len(value) > MAX_REFERENCE_LENGTH:
            return None
        if value not in self.resolved:
            if depth >= MAX_REFERENCE_DEPTH:
                return None
            self.resolved[value] = _PENDING
            self.resolved[value] = self.resolve(value, depth)
        found = self.resolved[value]
        return None if found is _PENDING else found

    def resolve(self, reference: str, depth: int) -> Any:
        match = self.params.reference_pattern.fullmatch(reference)
        if match is None or match["id"] not in self.rows:
            return None
        target = self.rows[match["id"]]
        path = match.groupdict().get("path")
        if isinstance(target, TextRow):
            return None if path else target.text
        for key in path.split(":") if path else []:
            target = step(self.value(target, depth + 1), key)
        return self.value(target, depth + 1)

    def follow(self, node: Any, path: str) -> list[Any]:
        """The values at a dotted ``path`` below ``node``; ``name[]`` fans out over a list.

        Every value is resolved exactly once, so an escaped "$$" text stays text.
        """
        current = [self.value(node)]
        for part in path.split("."):
            fan, name = part.endswith("[]"), part.removesuffix("[]")
            found: list[Any] = []
            for item in current:
                value = self.value(item.get(name)) if isinstance(item, dict) else None
                if fan and isinstance(value, list):
                    found.extend(self.value(entry) for entry in value[:MAX_LIST_ITEMS])
                elif not fan and value is not None:
                    found.append(value)
            current = found
        return [value for value in current if value is not None]

    def text(self, node: Any, path: str | None, length: int = MAX_TEXT_LENGTH) -> str | None:
        return next(iter(self.texts(node, path, length, 1)), None)

    def texts(self, node: Any, path: str | None, length: int, count: int) -> list[str]:
        """The first ``count`` texts at ``path``, each cut to ``length`` characters first."""
        values = self.follow(node, path) if path is not None else []
        texts = (collapse(value[:length]) for value in values if isinstance(value, str))
        return list(islice(filter(None, texts), count))

    def events(self) -> list[dict[str, Any]] | None:
        """Event objects of the lists at ``upcoming_key`` / ``previous_key``; ``None``: no list.

        A list or event that several objects refer to counts once; at most
        MAX_EVENTS events are taken.
        """
        found: dict[int, dict[str, Any]] = {}
        lists: set[int] = set()
        marker = self.params.event_marker_key
        for root in self.rows.values():
            for node in walk(root):
                for path in (self.params.upcoming_key, self.params.previous_key):
                    if not isinstance(node, dict) or path.split(".", 1)[0] not in node:
                        continue
                    target = self.at(node, path)
                    if not isinstance(target, list) or id(target) in lists:
                        continue
                    lists.add(id(target))
                    for item in map(self.value, target[:MAX_LIST_ITEMS]):
                        if isinstance(item, dict) and marker in item and len(found) < MAX_EVENTS:
                            found.setdefault(id(item), item)
        return list(found.values()) if lists else None

    def at(self, node: Any, path: str) -> Any:
        """The resolved value at a dotted ``path`` (no fan-out) below ``node``."""
        for name in path.split("."):
            node = self.value(node.get(name)) if isinstance(node, dict) else None
        return node


def step(node: Any, key: str) -> Any:
    """One step of a reference path; ``props`` of a React element is its fourth item."""
    if isinstance(node, dict):
        return node.get(key)
    if isinstance(node, list):
        if key == "props" and len(node) >= 4 and node[0] == "$":
            return node[3]
        if key.isdigit() and int(key) < len(node):
            return node[int(key)]
    return None


# --- events of the list ------------------------------------------------------------------


@dataclass(frozen=True)
class Listed:
    """One event as the list page describes it."""

    title: str | None
    start: datetime | date
    end: datetime | date | None
    link: str | None
    organiser_url: str | None
    summary_html: str | None
    type_label: str | None
    topics: tuple[str, ...] = ()

    @property
    def detail_url(self) -> str | None:
        """The event's own page; ``None`` when it only forwards to the organiser address."""
        return None if self.organiser_url else self.link


def kind(value: datetime | date) -> tuple[bool, bool]:
    """(has a time of day, has an offset): only values of one kind can be compared."""
    return isinstance(value, datetime), isinstance(value, datetime) and value.tzinfo is not None


def later(start: datetime | date, end: datetime | date | None) -> datetime | date | None:
    """``end`` when it is of the same kind as ``start`` and after it, else ``None``."""
    return end if end is not None and kind(start) == kind(end) and end > start else None


def moment(params: HaiParams, text: str | None) -> datetime | date | None:
    value = parse_moment(text)
    if isinstance(value, datetime) and value.tzinfo is not None and params.times_are_local:
        return value.replace(tzinfo=None)
    return value


def from_flight(
    params: HaiParams, flight: Flight, event: Mapping[str, Any], base: str
) -> Listed | None:
    keys = params.flight.keys
    start = moment(params, flight.text(event, keys.start))
    if start is None:
        return None

    def link(path: str | None) -> str | None:
        text = flight.text(event, path, MAX_TEXT_LENGTH + 1)
        return absolute_url(base, text) if text and len(text) <= MAX_TEXT_LENGTH else None

    return Listed(
        title=flight.text(event, keys.title),
        start=start,
        end=later(start, moment(params, flight.text(event, keys.end))),
        link=link(keys.link),
        organiser_url=link(keys.organiser_url),
        summary_html=flight.text(event, keys.summary_html, MAX_SUMMARY_LENGTH),
        type_label=flight.text(event, keys.type_label),
        topics=tuple(flight.texts(event, keys.topics, MAX_TOPIC_LENGTH, MAX_TOPICS)),
    )


def from_card(params: HaiParams, card: LexborNode, base: str, reference: date) -> Listed | None:
    selectors = params.card_selectors
    if selectors is None:
        return None
    day = parse_day(node_text(card.css_first(selectors.date)), params.card_date_format, reference)
    if day is None:
        return None
    start: datetime | date = day
    end: datetime | None = None
    clock = node_text(card.css_first(selectors.time)) if selectors.time else None
    match = search(params.card_time_pattern, clock) if params.card_time_pattern and clock else None
    if match is not None:
        begin, finish = time_range(match["start"], match.groupdict().get("end"))
        if begin is not None:
            start = datetime.combine(day, begin)
            end = datetime.combine(day, finish) if finish is not None else None
    link = card.css_first(selectors.link)
    return Listed(
        title=node_text(card.css_first(selectors.title)),
        start=start,
        end=later(start, end),
        link=absolute_url(base, link.attributes.get("href") if link is not None else None),
        organiser_url=None,
        summary_html=inner_html(card.css_first(selectors.summary)) if selectors.summary else None,
        type_label=node_text(card.css_first(selectors.type)) if selectors.type else None,
    )


def read_list(params: HaiParams, source: SourceConfig, document: FetchedDocument) -> list[Listed]:
    """The kept events of a list page: from the payload, else from the cards."""
    tree = LexborHTMLParser(document.text)
    base = document.final_url
    listed: Iterator[Listed | None]
    events = flight_events(params, tree)
    if events is not None:
        listed = (from_flight(params, flight, event, base) for flight, event in events)
    else:
        selectors, url = params.card_selectors, ascii_printable(document.url)
        cards = tree.css(selectors.item) if selectors is not None else []
        if not cards:
            raise ParseError(f"source {source.id}: neither the payload nor cards on {url}")
        logger.warning("source %s: the payload could not be read, the cards are used", source.id)
        reference = local_day(document.fetched_at, ZoneInfo(source.timezone))
        listed = (from_card(params, card, base, reference) for card in cards)
    unique: dict[tuple[str | None, str], Listed] = {}
    for item in listed:
        if item is not None and params.keeps(item.type_label):
            unique.setdefault((item.link, item.start.isoformat()), item)
    return list(unique.values())


def flight_events(
    params: HaiParams, tree: LexborHTMLParser
) -> list[tuple[Flight, dict[str, Any]]] | None:
    """The events of the payload with their payload; ``None`` when it cannot be read."""
    try:
        flight = Flight(params.flight, cut_rows(params.flight, flight_text(params.flight, tree)))
    except FlightError:
        return None
    events = flight.events()
    return None if events is None else [(flight, event) for event in events]


# --- detail pages ------------------------------------------------------------------------


@dataclass(frozen=True)
class Detail:
    """What a usable detail page adds to the list."""

    location: str | None = None
    status: EventStatus | None = None
    date_text: str | None = None
    topics: tuple[str, ...] = ()
    speakers: tuple[SpeakerDraft, ...] = ()
    stream_url: str | None = None
    description_html: str | None = None


def meta_rows(selectors: DetailSelectors, tree: LexborHTMLParser) -> dict[str, LexborNode]:
    """Folded label -> value element of the label and value rows."""
    rows: dict[str, LexborNode] = {}
    for row in tree.css(selectors.meta_row) if selectors.meta_row else []:
        cells = [child for child in row.iter() if child.is_element_node]
        label = node_text(cells[0]) if len(cells) >= 2 else None
        if label is not None:
            rows.setdefault(label.casefold(), cells[1])
    return rows


def people(
    params: HaiParams, selectors: DetailSelectors, tree: LexborHTMLParser, base: str
) -> tuple[SpeakerDraft, ...]:
    titles = {(collapse(title) or "").casefold() for title in params.speaker_list_titles}
    if selectors.person_list is None or selectors.person is None:
        return ()
    speakers: list[SpeakerDraft] = []
    for group in tree.css(selectors.person_list):
        title = selectors.person_list_title
        heading = group.css_first(title) if title else None
        if (node_text(heading) or "").casefold() not in titles:
            continue
        for card in group.css(selectors.person):
            name = node_text(
                card.css_first(selectors.person_name) if selectors.person_name else card
            )
            parts = card.css(selectors.person_title) if selectors.person_title else []
            affiliation = "; ".join(text for part in parts if (text := node_text(part)))
            link = card.css_first(selectors.person_link) if selectors.person_link else None
            href = link.attributes.get("href") if link is not None else None
            if name:
                url = absolute_url(base, href)
                speakers.append(SpeakerDraft(name=name, affiliation=affiliation or None, url=url))
    return tuple(speakers)


def read_detail(params: HaiParams, document: FetchedDocument) -> Detail | None:
    """What a detail page says; ``None`` for a forwarding page or one without a title."""
    selectors = params.detail_selectors
    tree = LexborHTMLParser(document.text)
    redirect = params.detail_redirect_selector
    if selectors is None or (redirect is not None and tree.css_first(redirect) is not None):
        return None
    if tree.css_first(selectors.title) is None:
        return None
    rows = meta_rows(selectors, tree)
    labels = params.detail_meta_labels

    def row(label: str | None) -> LexborNode | None:
        return rows.get(label.casefold()) if label else None

    status = node_text(row(labels.status))
    topics = row(labels.topics)
    links = topics.css("a") if topics is not None else []
    stream = tree.css_first(selectors.stream_link) if selectors.stream_link else None
    description = tree.css_first(selectors.description) if selectors.description else None
    base = document.final_url
    return Detail(
        location=node_text(row(labels.location)),
        status="cancelled" if status and _STATUS_CANCELLED.search(status) else None,
        date_text=node_text(row(labels.date)),
        topics=tuple(text for node in links or [topics] if (text := node_text(node))),
        speakers=people(params, selectors, tree, base),
        stream_url=absolute_url(base, stream.attributes.get("href") if stream else None),
        description_html=inner_html(description),
    )


def detail_end(start: datetime | date, date_text: str | None) -> datetime | date | None:
    """The end time on the start day that a date row gives ("... 12:00 PM - 1:15 PM")."""
    clock = parse_clock(split_range(date_text)[1]) if isinstance(start, datetime) else None
    if clock is None or not isinstance(start, datetime):
        return None
    return later(start, datetime.combine(start.date(), clock[0], tzinfo=start.tzinfo))


# --- titles ------------------------------------------------------------------------------


def name_parts(text: str) -> list[str]:
    return [part for part in _NAME_SEPARATOR.split(text[:PATTERN_INPUT_LIMIT]) if part]


def looks_like_person(text: str) -> bool:
    """Two to five capitalised words that are no initials-only, number or organisation word."""
    words = _NICKNAME.sub(" ", text).split()
    if not 2 <= len(words) <= 5:
        return False
    for word in words:
        if word.casefold() in _PARTICLES or _INITIAL.fullmatch(word):
            continue
        if word.casefold().strip(".,") in _NOT_NAME_WORDS or any(c.isdigit() for c in word):
            return False
        if not word[0].isupper() or not any(c.islower() for c in word):
            return False
    return True


def split_title(
    params: HaiParams, title: str | None, speakers: Sequence[SpeakerDraft]
) -> tuple[str | None, str | None, str | None]:
    """Title, series and speaker text of a "<prefix> | <rest>" title; see the module docstring."""
    separator = params.title_separator
    if separator is None or title is None or separator not in title:
        return title, None, None
    prefix, rest = (collapse(part) for part in title.split(separator, 1))
    if prefix is None or rest is None:
        return title, None, None
    parts = name_parts(prefix)
    if speakers:
        names = {speaker.name.casefold() for speaker in speakers}
        named = bool(parts) and all(part.casefold() in names for part in parts)
        return (rest, None, None) if named else (rest, prefix, None)
    if parts and all(looks_like_person(part) for part in parts):
        return rest, None, prefix
    return rest, prefix, None


def make_draft(
    source: SourceConfig, params: HaiParams, item: Listed, detail: Detail | None
) -> EventDraft:
    found = detail or Detail()
    title, series, speaker_text = split_title(params, item.title, found.speakers)
    return EventDraft(
        source_id=source.id,
        extraction="html",
        url=item.link,
        title=title,
        start=item.start,
        end=item.end or detail_end(item.start, found.date_text),
        speakers=list(found.speakers),
        speaker_text=speaker_text,
        series=series,
        location=found.location,
        is_online=True if found.stream_url else None,
        stream_url=found.stream_url,
        abstract_html=found.description_html or item.summary_html,
        categories=list(dict.fromkeys([*item.topics, *found.topics])),
        event_type_hint=params.event_type(item.type_label),
        status_hint=found.status,
    )


def detail_targets(
    params: HaiParams, source: SourceConfig, lists: Sequence[FetchedDocument], window: TimeWindow
) -> list[str]:
    """Detail pages of kept events from the fetch day on, in the window, on the source's hosts."""
    if params.fetch_details == "never":
        return []
    zone = ZoneInfo(source.timezone)
    wanted: dict[str, None] = {}  # ordered, without repeats
    foreign = 0
    for document in lists:
        today = local_day(document.fetched_at, zone)
        try:
            items = read_list(params, source, document)
        except ParseError:  # parse reports it; the payload stays whole for the pipeline
            continue
        for item in items:
            url = item.detail_url
            if local_day(item.start, zone) < today or url is None:
                continue
            if outside_window(item.start, item.end, window, zone):
                continue
            if not on_source_host(source, url):
                foreign += 1
            else:
                wanted[url] = None
    if foreign:
        logger.warning(
            "source %s: %d event(s) have their details on a host the source does not name; "
            "they are built from the list alone",
            source.id,
            foreign,
        )
    return list(wanted)[: params.max_detail_pages]


@register_adapter("hai_events")
class HaiEventsAdapter:
    """Events of a Next.js list page; see the module docstring."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        params = load_params(HaiParams, source)
        lists = [get_document(http, source, url, "list") for url in source.all_urls]
        targets = detail_targets(params, source, lists, window)
        details = fetch_optional(http, source, targets, "detail")
        return make_payload(source, [*lists, *details], window=window)

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        params = load_params(HaiParams, source)
        lists = payload.labelled("list")
        if not lists:
            raise ParseError(f"source {source.id}: the payload has no list page")
        pages = {document.url: document for document in payload.labelled("detail")}
        drafts: list[EventDraft] = []
        for document in lists:
            for item in read_list(params, source, document):
                url = item.detail_url
                page = pages.get(url) if url is not None else None
                detail = read_detail(params, page) if page is not None else None
                try:
                    drafts.append(make_draft(source, params, item, detail))
                except ValidationError:
                    logger.warning("source %s: a malformed event was skipped", source.id)
        return drafts
