"""Generic adapter ``rss``: RSS and Atom feeds, optionally completed from list and detail pages.

The feed is the first URL of the source (``url``, else the first of
``urls``). How the rest is read depends on ``params``:

* Feed only (no ``list_page``): every URL is a feed and every entry is a
  draft. ``feed.start`` says which element holds the START of the event (a
  WordPress feed's ``pubDate`` is the publication time, so this is never
  assumed); ``feed.end`` / ``location`` / ``speaker`` / ``abstract`` name
  further elements (extension fields), and the abstract defaults to the
  entry's description.
* Feed as index (``list_page`` without ``primary``): the remaining URLs are
  list pages. Every feed item is a draft, completed by the list card it
  joins (``list_page.join_on``: title and start agree) and, when
  ``detail_page.fetch`` asks for it, by the detail page of that card.
* List first (``list_page.primary: true``): every card is a draft, whether
  or not a feed item joins it; feed items without a card are dropped. The
  list is paged through ``list_page.pager`` (at most ``max_list_pages``
  pages), stopping at the first page without a pager link or a card, or
  whose last card starts at or after the end of the run window.

Keys of ``params`` (an unknown key is an error that names it;
``extra_hosts`` is read by the shared host check):

* ``feed``: ``start`` (required; an element name as feedparser reports it,
  ``pubDate`` for ``published``), ``end``, ``location``, ``speaker``,
  ``abstract``, ``native_id`` (``guid``, ``link`` or ``none``; default
  ``guid``), ``title_is_markup`` (the title holds unescaped HTML such as
  ``<h2><a href>Title</a></h2>``: take its text).
* ``list_page``: ``item`` (one card), ``title``, ``link`` (its ``href``),
  ``external_link_class`` (a link with this class points at an event taken
  from elsewhere: it becomes the ``url`` and is never fetched), ``type``,
  ``times`` (``datetime`` attributes: the first is the start, a second the
  end; date-only values make an all-day event whose second date is its
  last day), ``location`` (else ``address``, its lines joined with ", "),
  ``subheadline``, ``dek``, ``join_on``, ``primary``, ``pager``.
* ``detail_page``: ``fetch`` (``never``, default, or ``native_only``: the
  pages of cards whose link stays on the source's hosts and that overlap the
  run window, at most ``max_pages``, default 6; a link to another host is
  not fetched, and a list page redirected to a host the source does not
  name raises ``ForeignHostError``). Inside ``root`` (default:
  the body), matches inside ``exclude`` are ignored: ``end_attribute`` (an
  attribute of the root, Unix seconds: the end when the card has none),
  ``type``, ``title``, ``subheadline``, ``dek``, ``location`` (fallbacks for
  what the card lacks), ``date_text`` / ``time_text`` (the end as text, a
  last resort), ``audience`` (categories), ``body`` (the abstract) and
  ``schedule`` (``date`` headings, each followed by ``item`` elements with
  ``time``, ``headline`` and ``description``).
* ``expand_schedule``: one draft per session of the detail page's schedule
  instead of one draft for the whole series (the series title becomes the
  series, the description the speaker text); sessions whose headline
  matches one of ``skip_session_patterns`` ("No class") are left out.
* ``max_list_pages`` (default 1), ``exclude_types`` (card types dropped),
  ``event_type_names`` (card type -> event type), ``series_from: type`` (the
  card type is the series).
* ``card_fields``: which card part (``title``, ``subheadline``, ``dek``)
  holds which field (``speakers``, ``affiliation``, ``title``, ``host``).
  ``type_card_fields`` replaces it for one card type, and
  ``colloquium_fields`` (``speaker``, ``affiliation``, ``talk_title``) for
  cards whose type maps to ``colloquium``. A mapped title that is empty
  means the talk has no title yet. Parts that name no field (and the host,
  which ``EventDraft`` has no field for) are the abstract when nothing else
  is, unless they repeat the title or the location.
* ``speaker_patterns``: the first pattern that matches the speakers text is
  applied repeatedly; each match is one speaker (groups ``speaker`` and
  ``affiliation``), and a group of names ("A, B, and C") is split.
* ``title_patterns``: the first that matches sets ``title``, ``series``,
  ``speaker`` and ``affiliation`` from its groups; ``tba_title_patterns``:
  a title that matches one of them is no title yet (``TBA``).

The feed's own text is only read where nothing better exists: its title for
an item without a card, its start for joining and for items without a card.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Annotated, Any, Final, Literal
from zoneinfo import ZoneInfo

import feedparser
from pydantic import Field, ValidationError
from selectolax.lexbor import LexborHTMLParser, LexborNode

from seminars.http import HttpClient
from seminars.models import (
    TBA_TITLE,
    EventDraft,
    EventType,
    FetchedDocument,
    Model,
    RawPayload,
    SourceConfig,
    SpeakerDraft,
    TimeWindow,
)
from seminars.sources.base import (
    ForeignHostError,
    ParseError,
    absolute_url,
    make_payload,
    on_source_host,
    register_adapter,
)
from seminars.sources.generic.html_list import (
    PATTERN_INPUT_LIMIT,
    AdapterConfigError,
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
    outside_window,
    parse_day,
    parse_moment,
    search,
    split_range,
    time_range,
)
from seminars.textrules import ascii_printable

logger = logging.getLogger(__name__)

Moment = datetime | date
CardPart = Literal["title", "subheadline", "dek"]
CardField = Literal["speakers", "affiliation", "host", "title"]
_COLLOQUIUM_FIELDS: Final[dict[str, CardField]] = {
    "speaker": "speakers",
    "affiliation": "affiliation",
    "talk_title": "title",
}
_FEED_ALIASES: Final = {"pubDate": "published", "pubdate": "published"}


# --- params ------------------------------------------------------------------------------


class FeedParams(Model):
    start: str
    end: str | None = None
    location: str | None = None
    speaker: str | None = None
    abstract: str | None = None
    native_id: Literal["guid", "link", "none"] = "guid"
    title_is_markup: bool = False


class ListPageParams(Model):
    item: Selector
    title: Selector | None = None
    link: Selector | None = None
    external_link_class: str | None = None
    type: Selector | None = None
    times: Selector | None = None
    location: Selector | None = None
    address: Selector | None = None
    subheadline: Selector | None = None
    dek: Selector | None = None
    join_on: list[Literal["title", "start"]] = ["title", "start"]
    primary: bool = False
    pager: Selector | None = None


class ScheduleParams(Model):
    date: Selector
    item: Selector
    time: Selector | None = None
    headline: Selector | None = None
    description: Selector | None = None


class DetailPageParams(Model):
    fetch: Literal["never", "native_only"] = "never"
    max_pages: Annotated[int, Field(ge=1, le=20)] = 6
    root: Selector | None = None
    exclude: Selector | None = None
    end_attribute: str | None = None
    type: Selector | None = None
    title: Selector | None = None
    subheadline: Selector | None = None
    dek: Selector | None = None
    date_text: Selector | None = None
    time_text: Selector | None = None
    location: Selector | None = None
    audience: Selector | None = None
    body: Selector | None = None
    schedule: ScheduleParams | None = None


class RssParams(Model):
    """``params`` of an ``rss`` source; see the module docstring."""

    feed: FeedParams
    list_page: ListPageParams | None = None
    detail_page: DetailPageParams = DetailPageParams()
    expand_schedule: bool = False
    skip_session_patterns: list[Pattern] = []
    max_list_pages: Annotated[int, Field(ge=1, le=10)] = 1
    exclude_types: list[str] = []
    event_type_names: dict[str, EventType] = {}
    series_from: Literal["type"] | None = None
    card_fields: dict[CardField, CardPart] = {}
    type_card_fields: dict[str, dict[CardField, CardPart]] = {}
    colloquium_fields: dict[Literal["speaker", "affiliation", "talk_title"], CardPart] = {}
    speaker_patterns: list[Pattern] = []
    title_patterns: list[Pattern] = []
    tba_title_patterns: list[Pattern] = []

    def type_of(self, name: str | None) -> EventType | None:
        names: dict[str | None, EventType] = {
            collapse(key): value for key, value in self.event_type_names.items()
        }
        return names.get(name) if name else None

    def mapping(self, type_name: str | None) -> Mapping[CardField, CardPart]:
        by_type = {collapse(key): value for key, value in self.type_card_fields.items()}
        if type_name in by_type:
            return by_type[type_name]
        if self.colloquium_fields and self.type_of(type_name) == "colloquium":
            return {_COLLOQUIUM_FIELDS[key]: part for key, part in self.colloquium_fields.items()}
        return self.card_fields


def _load(source: SourceConfig) -> RssParams:
    params = load_params(RssParams, source)
    if params.list_page is not None and len(source.all_urls) < 2:
        raise AdapterConfigError(f"source {source.id}: list_page needs the list in 'urls'")
    if params.expand_schedule and params.detail_page.schedule is None:
        raise AdapterConfigError(f"source {source.id}: expand_schedule needs detail_page.schedule")
    return params


# --- the feed ----------------------------------------------------------------------------


@dataclass(frozen=True)
class FeedItem:
    title: str | None
    link: str | None
    start: Moment | str | None
    native_id: str | None
    fields: Mapping[str, str]  # end, location, speaker, abstract as the feed writes them


def feed_moment(text: str | None) -> Moment | str | None:
    """A feed date: ISO 8601 or RFC 822 with its offset; other text stays text."""
    value = collapse(text)
    if value is None:
        return None
    moment = parse_moment(value)
    if moment is not None:
        return moment
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return value


def markup_text(text: str | None) -> str | None:
    """The text of an HTML fragment."""
    if not text:
        return None
    return node_text(LexborHTMLParser(text).body)


def read_feed(
    params: FeedParams, document: FetchedDocument, *, abstract: str | None
) -> list[FeedItem]:
    """The entries of one feed. feedparser gets a stream: a string could be read as a URL."""
    parsed: Any = feedparser.parse(io.BytesIO(document.text.encode("utf-8")))
    if not parsed.get("version") and not parsed.entries:
        raise ParseError(f"not an RSS or Atom feed: {ascii_printable(document.url)}")
    keys = {
        "end": params.end,
        "location": params.location,
        "speaker": params.speaker,
        "abstract": abstract,
    }
    items = []
    for entry in parsed.entries:
        title = entry.get("title")
        kind = (entry.get("title_detail") or {}).get("type")
        markup = params.title_is_markup or kind in ("text/html", "application/xhtml+xml")
        link = absolute_url(document.final_url, entry.get("link"))
        native = {"guid": entry.get("id"), "link": link, "none": None}[params.native_id]
        values = {name: entry.get(_FEED_ALIASES.get(key, key)) for name, key in keys.items() if key}
        items.append(
            FeedItem(
                title=markup_text(title) if markup else collapse(title),
                link=link,
                start=feed_moment(entry.get(_FEED_ALIASES.get(params.start, params.start))),
                native_id=collapse(native),
                fields={name: value for name, value in values.items() if isinstance(value, str)},
            )
        )
    return items


# --- list pages --------------------------------------------------------------------------


@dataclass(frozen=True)
class Card:
    page: str  # final URL of the list page
    title: str | None
    href: str | None
    external: bool
    type: str | None
    start: Moment | None
    end: Moment | None
    location: str | None
    subheadline: str | None
    dek: str | None


def read_cards(params: ListPageParams, document: FetchedDocument) -> list[Card]:
    tree = LexborHTMLParser(document.text)
    cards = []
    for node in tree.css(params.item):

        def first(selector: str | None, scope: LexborNode = node) -> LexborNode | None:
            return scope.css_first(selector) if selector else None

        link = first(params.link)
        classes = (link.attributes.get("class") or "").split() if link else []
        times = [
            parse_moment(element.attributes.get("datetime") or node_text(element))
            for element in (node.css(params.times) if params.times else [])
        ]
        cards.append(
            Card(
                page=document.final_url,
                title=node_text(first(params.title)),
                href=absolute_url(document.final_url, link.attributes.get("href"))
                if link
                else None,
                external=params.external_link_class in classes,
                type=node_text(first(params.type)),
                start=times[0] if times else None,
                end=times[1] if len(times) > 1 else None,
                location=node_lines(first(params.location)) or node_lines(first(params.address)),
                subheadline=node_text(first(params.subheadline)),
                dek=node_text(first(params.dek)),
            )
        )
    return cards


def starts_before(moment: Moment | None, limit: datetime, zone: ZoneInfo) -> bool:
    if moment is None:
        return False
    if isinstance(moment, datetime):
        aware = moment if moment.tzinfo else moment.replace(tzinfo=zone)
        return aware < limit
    return moment < local_day(limit, zone)


# --- joining feed items and cards --------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    item: FeedItem | None
    card: Card | None

    @property
    def start(self) -> Moment | None:
        if self.card is not None and self.card.start is not None:
            return self.card.start
        start = self.item.start if self.item else None
        return start if isinstance(start, date) else None


def same_start(card: Moment | None, item: Moment | str | None) -> bool:
    if card is None or item is None or isinstance(item, str):
        return False
    if isinstance(card, datetime) and isinstance(item, datetime):
        return (card.tzinfo is None) == (item.tzinfo is None) and card == item
    card_day = card.date() if isinstance(card, datetime) else card
    item_day = item.date() if isinstance(item, datetime) else item
    return card_day == item_day


def joins(params: ListPageParams, item: FeedItem, card: Card) -> bool:
    if "title" in params.join_on:
        if item.title is None or card.title is None:
            return False
        if item.title.casefold() != card.title.casefold():
            return False
    return "start" not in params.join_on or same_start(card.start, item.start)


def join(params: RssParams, items: Sequence[FeedItem], cards: Sequence[Card]) -> list[Entry]:
    """Feed items and cards paired up, in the order of the side that makes the drafts."""
    if params.list_page is None:
        return [Entry(item, None) for item in items]
    pairs: dict[int, int] = {}  # card index -> item index
    used: set[int] = set()
    for card_index, card in enumerate(cards):
        for item_index, item in enumerate(items):
            if item_index not in used and joins(params.list_page, item, card):
                pairs[card_index] = item_index
                used.add(item_index)
                break
    if params.list_page.primary:
        entries = [
            Entry(items[pairs[i]] if i in pairs else None, card) for i, card in enumerate(cards)
        ]
    else:
        by_item = {item_index: card_index for card_index, item_index in pairs.items()}
        entries = [
            Entry(item, cards[by_item[i]] if i in by_item else None) for i, item in enumerate(items)
        ]
    excluded = {collapse(name) for name in params.exclude_types}
    return [entry for entry in entries if not (entry.card and entry.card.type in excluded)]


def read_entries(
    params: RssParams, feeds: Sequence[FetchedDocument], lists: Sequence[FetchedDocument]
) -> tuple[list[FeedItem], list[Card], list[Entry]]:
    abstract = params.feed.abstract or ("summary" if params.list_page is None else None)
    items = [item for doc in feeds for item in read_feed(params.feed, doc, abstract=abstract)]
    cards = (
        [card for doc in lists for card in read_cards(params.list_page, doc)]
        if params.list_page
        else []
    )
    return items, cards, join(params, items, cards)


# --- detail pages ------------------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    day: date | None
    time: str | None
    headline: str | None
    description: str | None


@dataclass(frozen=True)
class Detail:
    texts: Mapping[str, str]  # type, title, subheadline, dek, location, date_text, time_text
    end: datetime | None = None
    body: str | None = None
    audience: tuple[str, ...] = ()
    sessions: tuple[Session, ...] = ()


def read_detail(params: DetailPageParams, document: FetchedDocument, reference: date) -> Detail:
    tree = LexborHTMLParser(document.text)
    root = tree.css_first(params.root) if params.root else tree.body
    if root is None:
        return Detail(texts={})
    hidden = {node.mem_id for node in root.css(params.exclude)} if params.exclude else set()

    def visible(node: LexborNode) -> bool:
        current: LexborNode | None = node
        while current is not None and current.mem_id != root.mem_id:
            if current.mem_id in hidden:
                return False
            current = current.parent
        return True

    def matches(selector: str | None) -> list[LexborNode]:
        return [node for node in root.css(selector) if visible(node)] if selector else []

    texts = {}
    for name in ("type", "title", "subheadline", "dek", "location", "date_text", "time_text"):
        found = matches(getattr(params, name))
        value = node_lines(found[0]) if found else None
        if value:
            texts[name] = value
    body = matches(params.body)
    audience = tuple(text for node in matches(params.audience) if (text := node_text(node)))
    return Detail(
        texts=texts,
        end=unix_time(root.attributes.get(params.end_attribute) if params.end_attribute else None),
        body=inner_html(body[0]) if body else None,
        audience=audience,
        sessions=read_sessions(params.schedule, root, matches, reference),
    )


def unix_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value or ""), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def read_sessions(
    params: ScheduleParams | None, root: LexborNode, matches: Any, reference: date
) -> tuple[Session, ...]:
    """Sessions of a schedule, each with the date of the heading before it."""
    if params is None:
        return ()
    headings = {node.mem_id for node in matches(params.date)}
    items = {node.mem_id for node in matches(params.item)}
    sessions = []
    day: date | None = None
    for node in root.traverse():
        if node.mem_id in headings:
            day = parse_day(node_text(node), None, reference)
        elif node.mem_id in items:

            def text(selector: str | None, scope: LexborNode = node) -> str | None:
                return node_text(scope.css_first(selector)) if selector else None

            sessions.append(
                Session(day, text(params.time), text(params.headline), text(params.description))
            )
    return tuple(sessions)


def detail_targets(
    params: RssParams, source: SourceConfig, entries: Sequence[Entry], window: TimeWindow
) -> list[str]:
    """Detail pages of native cards in the window, on the source's hosts.

    A native card that links to another host keeps its draft without a
    detail page; a list page redirected off the source's hosts is
    ``ForeignHostError``.
    """
    zone = ZoneInfo(source.timezone)
    wanted: list[str] = []
    for entry in entries:
        card = entry.card
        if card is None or card.href is None or card.external:
            continue
        if outside_window(entry.start, card.end, window, zone):
            continue
        if not on_source_host(source, card.page):
            raise ForeignHostError(source.id, card.page)
        if on_source_host(source, card.href) and card.href not in wanted:
            wanted.append(card.href)
    return wanted[: params.detail_page.max_pages]


# --- drafts ------------------------------------------------------------------------------


def speakers_from(
    text: str | None, patterns: Sequence[Pattern], affiliation: str | None
) -> list[SpeakerDraft]:
    """Speakers in ``text``: every match of the first matching pattern is one."""
    if text is None:
        return []
    for pattern in patterns:
        if search(pattern, text) is None:
            continue
        speakers: list[SpeakerDraft] = []
        for match in pattern.finditer(text[:PATTERN_INPUT_LIMIT]):
            groups = match.groupdict()
            speakers.extend(
                make_speakers(groups.get("speaker"), groups.get("affiliation") or affiliation)
            )
        return speakers
    return make_speakers(text, affiliation)


@dataclass(frozen=True)
class Fields:
    """What the card, the detail page and the feed say about one event."""

    title: str | None
    series: str | None
    speakers: list[SpeakerDraft]
    summary: str | None
    type_name: str | None


def card_fields(params: RssParams, entry: Entry, detail: Detail, location: str | None) -> Fields:
    card, item = entry.card, entry.item
    type_name = (card.type if card else None) or detail.texts.get("type")
    parts: dict[str, str | None] = {
        "title": (card.title if card else None)
        or (item.title if item else None)
        or detail.texts.get("title"),
        "subheadline": (card.subheadline if card else None) or detail.texts.get("subheadline"),
        "dek": (card.dek if card else None) or detail.texts.get("dek"),
    }
    mapping = params.mapping(type_name)
    title = parts[mapping["title"]] if "title" in mapping else parts["title"]
    if "title" in mapping and not title:
        title = TBA_TITLE
    affiliation = parts[mapping["affiliation"]] if "affiliation" in mapping else None
    speakers_text = parts[mapping["speakers"]] if "speakers" in mapping else None
    speakers = speakers_from(speakers_text, params.speaker_patterns, affiliation)
    assigned = {mapping[key] for key in ("title", "speakers", "affiliation") if key in mapping}
    leftover = [
        text
        for part in ("subheadline", "dek")
        if part not in assigned and (text := parts[part]) and text not in (title, location)
    ]
    series = type_name if params.series_from == "type" else None
    if title != TBA_TITLE and any_match(params.tba_title_patterns, title):
        title = TBA_TITLE
    for pattern in params.title_patterns if title and title != TBA_TITLE else []:
        match = search(pattern, title or "")
        if match is None:
            continue
        groups = {key: collapse(value) for key, value in match.groupdict().items()}
        title = groups.get("title") or title
        series = groups.get("series") or series
        if not speakers:
            speakers = make_speakers(groups.get("speaker"), groups.get("affiliation"))
        break
    if any_match(params.tba_title_patterns, title):
        title = TBA_TITLE
    return Fields(title, series, speakers, "\n".join(leftover) or None, type_name)


def entry_times(entry: Entry, detail: Detail) -> tuple[Moment | str | None, Moment | str | None]:
    card, item = entry.card, entry.item
    start: Moment | str | None = card.start if card else None
    end: Moment | str | None = card.end if card else None
    if start is None and item is not None:
        start, end = item.start, feed_moment(item.fields.get("end"))
    if end is None and isinstance(start, datetime):
        end = detail.end
        if end is None and "time_text" in detail.texts:
            finish = time_range(*split_range(detail.texts["time_text"]))[1]
            end = datetime.combine(start.date(), finish) if finish else None
    return start, end


def build_drafts(
    params: RssParams, source: SourceConfig, entry: Entry, detail: Detail
) -> list[EventDraft]:
    item = entry.item
    location = (entry.card.location if entry.card else None) or detail.texts.get("location")
    location = location or (item.fields.get("location") if item else None)
    fields = card_fields(params, entry, detail, location)
    start, end = entry_times(entry, detail)
    url = (entry.card.href if entry.card else None) or (item.link if item else None)
    feed_abstract = item.fields.get("abstract") if item else None
    common: dict[str, Any] = {
        "source_id": source.id,
        "extraction": "feed",
        "url": url,
        "location": location,
        "categories": [name for name in (fields.type_name, *detail.audience) if name],
        "event_type_hint": params.type_of(fields.type_name),
    }
    speakers = fields.speakers
    speaker_text = None if speakers or item is None else item.fields.get("speaker")
    native_id = item.native_id if item else None
    if params.expand_schedule and detail.sessions:
        return session_drafts(params, common, fields, detail, native_id)
    draft = {
        **common,
        "native_id": native_id,
        "title": fields.title,
        "series": fields.series,
        "start": start,
        "end": end,
        "speakers": speakers,
        "speaker_text": speaker_text,
        "abstract_html": detail.body or feed_abstract,
        "abstract": None if detail.body or feed_abstract else fields.summary,
    }
    return [EventDraft.model_validate(draft)] if start is not None else []


def session_drafts(
    params: RssParams,
    common: Mapping[str, Any],
    fields: Fields,
    detail: Detail,
    native_id: str | None,
) -> list[EventDraft]:
    """One draft per session of the schedule; skipped headlines and dateless sessions drop."""
    drafts = []
    for index, session in enumerate(detail.sessions, start=1):
        if session.day is None or any_match(params.skip_session_patterns, session.headline):
            continue
        begin, finish = time_range(*split_range(session.time))
        start = datetime.combine(session.day, begin) if begin else session.day
        end = datetime.combine(session.day, finish) if begin and finish else None
        drafts.append(
            EventDraft.model_validate(
                {
                    **common,
                    "native_id": f"{native_id}/{index}" if native_id else None,
                    "title": session.headline,
                    "series": fields.series
                    or (fields.title if fields.title != TBA_TITLE else None),
                    "start": start,
                    "end": end,
                    "speaker_text": session.description,
                }
            )
        )
    return drafts


# --- the adapter -------------------------------------------------------------------------


@register_adapter("rss")
class RssAdapter:
    """RSS / Atom feeds with optional list and detail pages; see the module docstring."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        params = _load(source)
        urls = source.all_urls
        feed_urls = urls if params.list_page is None else urls[:1]
        feeds = [get_document(http, source, url, "feed") for url in feed_urls]
        lists: list[FetchedDocument] = []
        for url in urls[1:] if params.list_page else []:
            lists.extend(self._fetch_list(http, source, params, url, window))
        details: list[FetchedDocument] = []
        if params.list_page is not None and params.detail_page.fetch == "native_only":
            entries = read_entries(params, feeds, lists)[2]
            targets = detail_targets(params, source, entries, window)
            details = fetch_optional(http, source, targets, "detail")
        return make_payload(source, [*feeds, *lists, *details], window=window)

    @staticmethod
    def _fetch_list(
        http: HttpClient, source: SourceConfig, params: RssParams, url: str, window: TimeWindow
    ) -> list[FetchedDocument]:
        """A list page and the pages its pager leads to (bounded by max_list_pages)."""
        assert params.list_page is not None
        pages = [get_document(http, source, url, "list")]
        zone = ZoneInfo(source.timezone)
        while params.list_page.pager and len(pages) < params.max_list_pages:
            page = pages[-1]
            cards = read_cards(params.list_page, page)
            if not cards or not starts_before(cards[-1].start, window.end, zone):
                break
            pager = LexborHTMLParser(page.text).css_first(params.list_page.pager)
            following = absolute_url(
                page.final_url, pager.attributes.get("href") if pager else None
            )
            if following is None or following in {doc.url for doc in pages}:
                break
            if not on_source_host(source, following):
                raise ForeignHostError(source.id, following)
            pages.append(get_document(http, source, following, "list"))
        return pages

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        params = _load(source)
        feeds = payload.labelled("feed")
        if not feeds:
            raise ParseError(f"source {source.id}: the payload has no feed")
        items, cards, entries = read_entries(params, feeds, payload.labelled("list"))
        if params.list_page is not None and items and not cards:
            raise ParseError(
                f"source {source.id}: list_page.item {params.list_page.item!r} matches nothing, "
                f"although the feed lists {len(items)} items"
            )
        zone = ZoneInfo(source.timezone)
        reference = local_day(payload.fetched_at, zone)
        details = {
            document.url: read_detail(params.detail_page, document, reference)
            for document in payload.labelled("detail")
        }
        drafts: list[EventDraft] = []
        for entry in entries:
            href = entry.card.href if entry.card else None
            detail = details.get(href or "", Detail(texts={}))
            try:
                drafts.extend(build_drafts(params, source, entry, detail))
            except ValidationError:
                logger.debug("source %s: a malformed feed item was skipped", source.id)
        return drafts
