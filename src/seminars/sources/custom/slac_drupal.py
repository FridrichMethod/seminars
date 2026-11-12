"""Custom adapter ``slac_drupal``: event lists of the SLAC Drupal theme (``div.c-card``).

SLAC's own sites (the SLAC Colloquium, the Theory Group) run a Drupal theme
that is neither Stanford template: no JSON:API, no RSS, and on the list only
a date as text. Each detail page, though, embeds an "iCal" link whose href
is a ``data:text/calendar`` URI with one VEVENT (UID, SUMMARY, DTSTART and
DTEND with a TZID, DESCRIPTION).

``fetch`` requests the list page(s) of the source (``url`` / ``urls``) and,
when ``params.detail`` is configured, the detail page of every card that
is kept (not matched by ``skip_title_patterns``), whose date does not lie
outside the run window, and whose link stays on the source's hosts, at
most ``max_detail_pages`` of them in list order. A detail page that cannot
be fetched is skipped. A link to another host is not fetched (a warning
counts such cards); when no wanted link can be fetched at all and the
drafts need their detail pages (``native_id: vevent_uid``), that is a
``ForeignHostError``, and so is a list page that was redirected to a host
the source does not name.

``parse`` turns every kept card into a draft:

* times: DTSTART / DTEND of the VEVENT. Their TZID is resolved with
  ``zoneinfo``, never with the VTIMEZONE of the URI, which has a STANDARD
  part only (-0800 all year) and would put every summer talk an hour off;
  a TZID that ``zoneinfo`` does not know gives a floating local time. A
  value without a TZID of its own (UTC) is kept as it is.
  Without a VEVENT the card's date text gives a naive local time ("Monday,
  November 16, 2026", a middle dot, "4:00 p.m." an en dash "5:00 p.m. PT";
  the zone is the source's), or a date only (an all-day event).
* ``native_id``: the VEVENT UID with ``native_id: vevent_uid``. A card
  without one (no detail page, no iCal link, a URI that is no calendar) is
  then dropped for this run, never emitted under another key. Without the
  key the native id stays empty (the key is the detail URL).
* ``title``: of the detail page, else of the card, else the SUMMARY. A
  title matched by ``tba_title_patterns`` is "TBA"; the groups ``speaker``
  and ``affiliation`` of the pattern name the speaker when nothing else
  does ("Talk by Jane Doe").
* speakers: the card's speaker and affiliation; else the detail page's
  speaker line ("Name" or "Name, Affiliation"); else the TBA title.
* abstract: the detail page's (as HTML), else the VEVENT DESCRIPTION
  without its trailing "Link to event page: <url>" (plain text), else the
  card's teaser (HTML). An abstract matched by ``tba_abstract_patterns``
  ("Info coming soon!") is none.
* ``url``: the card's link; there is no location on these sites.

A list page without any card is a ``ParseError``, unless ``list.empty``
matches on it (the "No items to display." block of the theme): then it
holds no event. A card without a start is skipped, as is a draft that
fails validation.

Parameters (``params``; an unknown key is an error that names it,
``extra_hosts`` is read by the shared host check):

* ``list``: CSS selectors of the list page. ``card`` (required) is one
  event; inside it ``title``, ``link`` (its ``href``), ``speaker``,
  ``affiliation``, ``abstract``, ``date_text`` (SVG icons are left out of
  its text). ``empty``: an element of the page that says the list is empty.
* ``detail``: CSS selectors of a detail page. ``exclude``: blocks to
  remove first (the theme shows the cards of three OTHER events below the
  event, with the classes of the list); ``ical_link``: the link with the
  ``data:text/calendar`` URI; ``title``, ``speaker``, ``abstract``.
* ``native_id``: ``vevent_uid`` (needs ``detail.ical_link``), see above.
* ``max_detail_pages``: most detail pages per run (1 to 19, default 10),
  so that the list and the details stay within the request budget of a
  host.
* ``skip_title_patterns``: cards whose title matches one are no event
  ("No SLAC Colloquium 11/23"); also checked against the detail title.
* ``tba_title_patterns``, ``tba_abstract_patterns``: see above.
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Final, Literal
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import icalendar
from pydantic import Field, ValidationError, field_validator, model_validator
from selectolax.lexbor import LexborHTMLParser, LexborNode

from seminars.httpbase import HttpClient
from seminars.models import (
    TBA_TITLE,
    EventDraft,
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
    node_text,
    outside_window,
    parse_day,
    search,
    split_range,
    time_range,
)
from seminars.sources.generic.ics import read_calendar, vevent_draft
from seminars.textrules import ascii_printable

logger = logging.getLogger(__name__)

DATE_TIME_SEPARATOR: Final = "\u00b7"  # the middle dot between date and time on a card
PAGE_LINK_LABEL: Final = "Link to event page:"  # how the theme ends a VEVENT DESCRIPTION
CALENDAR_URI: Final = "data:text/calendar"
TBA_GROUPS: Final = frozenset({"speaker", "affiliation"})

Moment = datetime | date
DraftTime = datetime | date | str | None


# --- params ------------------------------------------------------------------------------


class ListSelectors(Model):
    card: Selector
    title: Selector | None = None
    link: Selector | None = None
    speaker: Selector | None = None
    affiliation: Selector | None = None
    abstract: Selector | None = None
    date_text: Selector | None = None
    empty: Selector | None = None


class DetailSelectors(Model):
    exclude: Selector | None = None
    ical_link: Selector | None = None
    title: Selector | None = None
    speaker: Selector | None = None
    abstract: Selector | None = None


class SlacDrupalParams(Model):
    """``params`` of a ``slac_drupal`` source; see the module docstring."""

    cards: ListSelectors = Field(alias="list")
    detail: DetailSelectors | None = None
    native_id: Literal["vevent_uid"] | None = None
    max_detail_pages: Annotated[int, Field(ge=1, le=19)] = 10
    skip_title_patterns: list[Pattern] = []
    tba_title_patterns: list[Pattern] = []
    tba_abstract_patterns: list[Pattern] = []

    @field_validator("tba_title_patterns")
    @classmethod
    def _known_groups(cls, value: list[Pattern]) -> list[Pattern]:
        for pattern in value:
            if unknown := sorted(set(pattern.groupindex) - TBA_GROUPS):
                raise ValueError(f"unknown named group(s) {', '.join(unknown)}")
        return value

    @model_validator(mode="after")
    def _native_id_needs_the_ical_link(self) -> SlacDrupalParams:
        if self.native_id == "vevent_uid" and (self.detail is None or not self.detail.ical_link):
            raise ValueError("native_id vevent_uid needs detail.ical_link")
        return self


# --- cards -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Card:
    """What one card of a list page says."""

    title: str | None = None
    link: str | None = None
    speaker: str | None = None
    affiliation: str | None = None
    abstract_html: str | None = None
    abstract_text: str | None = None
    date_text: str | None = None


def _first(node: LexborNode | LexborHTMLParser, selector: str | None) -> LexborNode | None:
    return node.css_first(selector) if selector else None


def text_without_icons(node: LexborNode | None) -> str | None:
    """The text of ``node`` without the text of its SVG icons (their ``<title>``)."""
    if node is None:
        return None
    copy = node.clone()
    for icon in copy.css("svg"):
        icon.decompose()
    return node_text(copy)


def read_card(selectors: ListSelectors, node: LexborNode) -> Card:
    link = _first(node, selectors.link)
    abstract = _first(node, selectors.abstract)
    return Card(
        title=node_text(_first(node, selectors.title)),
        link=link.attributes.get("href") if link is not None else None,
        speaker=node_text(_first(node, selectors.speaker)),
        affiliation=node_text(_first(node, selectors.affiliation)),
        abstract_html=inner_html(abstract),
        abstract_text=node_text(abstract),
        date_text=text_without_icons(_first(node, selectors.date_text)),
    )


def read_cards(params: SlacDrupalParams, tree: LexborHTMLParser) -> list[Card]:
    return [read_card(params.cards, node) for node in tree.css(params.cards.card)]


def card_times(text: str | None, reference: date) -> tuple[Moment | None, Moment | None]:
    """Start and end of a card's date text: naive local times, or a date only."""
    value = collapse(text)
    if value is None:
        return None, None
    day_text, _, time_text = value.partition(DATE_TIME_SEPARATOR)
    day = parse_day(day_text, None, reference)
    if day is None:
        return None, None
    begin, finish = time_range(*split_range(time_text))
    if begin is None:
        return day, None
    start = datetime.combine(day, begin)
    end = datetime.combine(day, finish) if finish is not None else None
    return start, end if end is not None and end > start else None


# --- detail pages ------------------------------------------------------------------------


@dataclass(frozen=True)
class Detail:
    """What a detail page adds to its card."""

    title: str | None = None
    speaker: str | None = None
    abstract_html: str | None = None
    abstract_text: str | None = None
    vevent: icalendar.Component | None = None


def calendar_text(uri: str | None) -> str | None:
    """The text of a ``data:text/calendar`` URI (percent-encoded or base64), else ``None``."""
    if uri is None or not uri.strip().lower().startswith(CALENDAR_URI):
        return None
    header, separator, data = uri.strip().partition(",")
    if not separator:
        return None
    if header.lower().endswith(";base64"):
        try:
            return base64.b64decode(unquote(data), validate=False).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return None
    return unquote(data, encoding="utf-8", errors="replace")


def embedded_vevent(document: FetchedDocument, uri: str | None) -> icalendar.Component | None:
    """The first VEVENT of the calendar in ``uri``; ``None`` when there is none."""
    text = calendar_text(uri)
    if text is None:
        return None
    embedded = FetchedDocument(
        url=document.url,
        final_url=document.final_url,
        status=document.status,
        content_type="text/calendar",
        text=text,
        fetched_at=document.fetched_at,
    )
    try:
        vevents = read_calendar(embedded).vevents
    except (ParseError, AttributeError, KeyError, IndexError, TypeError):
        # icalendar 7 can fail on a broken VTIMEZONE with an AttributeError; one
        # embedded calendar must not take the other cards of the list down.
        return None
    return vevents[0] if vevents else None


def read_detail(selectors: DetailSelectors, document: FetchedDocument) -> Detail:
    tree = LexborHTMLParser(document.text)
    if selectors.exclude:
        for block in tree.css(selectors.exclude):
            block.decompose()
    link = _first(tree, selectors.ical_link)
    abstract = _first(tree, selectors.abstract)
    return Detail(
        title=node_text(_first(tree, selectors.title)),
        speaker=node_text(_first(tree, selectors.speaker)),
        abstract_html=inner_html(abstract),
        abstract_text=node_text(abstract),
        vevent=embedded_vevent(document, link.attributes.get("href") if link else None),
    )


# --- drafts ------------------------------------------------------------------------------


def _tzid(vevent: icalendar.Component, name: str) -> str | None:
    """The TZID parameter of property ``name``; ``None`` when it has none (UTC, floating)."""
    params = getattr(vevent.get(name), "params", None)
    tzid = params.get("TZID") if isinstance(params, Mapping) else None
    return str(tzid) if tzid is not None else None


def _zoned(moment: DraftTime, tzid: str | None) -> DraftTime:
    """``moment`` as wall time in the zone ``tzid`` names, looked up with ``zoneinfo``.

    The VTIMEZONE of the URI is never used: a TZID that ``zoneinfo`` does not
    know leaves a floating wall time (in the source's timezone).
    """
    if not isinstance(moment, datetime) or tzid is None:
        return moment
    try:
        zone: ZoneInfo | None = ZoneInfo(tzid)
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    return moment.replace(tzinfo=zone)


@dataclass(frozen=True)
class EventFacts:
    """What the VEVENT of a detail page says, in draft terms."""

    native_id: str | None
    start: DraftTime
    end: DraftTime
    title: str | None
    description: str | None


def event_facts(
    vevent: icalendar.Component, source: SourceConfig, base_url: str
) -> EventFacts | None:
    """Times, UID, SUMMARY and DESCRIPTION of the VEVENT; ``None`` when it cannot be read.

    Each time is re-zoned by the TZID of its own property only: a DTEND
    without one (a UTC value) is kept as it is, whatever DTSTART says. A
    DURATION instead of a DTEND gives an end in the zone of DTSTART.
    """
    try:
        common = vevent_draft(vevent, source, base_url=base_url)
    except (ValueError, TypeError):
        return None
    end_property = "DTEND" if vevent.get("DTEND") is not None else "DTSTART"
    return EventFacts(
        native_id=common.native_id,
        start=_zoned(common.start, _tzid(vevent, "DTSTART")),
        end=_zoned(common.end, _tzid(vevent, end_property)),
        title=common.title,
        description=without_page_link(common.abstract),
    )


def without_page_link(text: str | None) -> str | None:
    """A VEVENT DESCRIPTION without the theme's trailing "Link to event page: <url>"."""
    if text is None:
        return None
    index = text.rfind(PAGE_LINK_LABEL)
    if index >= 0 and len(text[index + len(PAGE_LINK_LABEL) :].split()) <= 1:
        text = text[:index]
    return text.strip() or None


def _tba(params: SlacDrupalParams, title: str | None) -> tuple[bool, list[SpeakerDraft]]:
    """Whether ``title`` is a TBA title, and the speaker its pattern names."""
    for pattern in params.tba_title_patterns:
        match = search(pattern, title) if title else None
        if match is not None:
            groups = match.groupdict()
            return True, make_speakers(groups.get("speaker"), groups.get("affiliation"))
    return False, []


def _line_speakers(line: str | None) -> list[SpeakerDraft]:
    """The speakers of a detail page's speaker line, "Name" or "Name, Affiliation"."""
    names, _, affiliation = (line or "").partition(", ")
    return make_speakers(names, affiliation or None)


def _abstract(
    params: SlacDrupalParams, card: Card, detail: Detail, facts: EventFacts | None
) -> tuple[str | None, str | None]:
    """``(abstract, abstract_html)`` from the first source that has a real one."""
    patterns = params.tba_abstract_patterns
    candidates = (
        (None, detail.abstract_html, detail.abstract_text),
        (facts.description if facts else None, None, facts.description if facts else None),
        (None, card.abstract_html, card.abstract_text),
    )
    for text, html, plain in candidates:
        if (text or html) and not any_match(patterns, plain):
            return text, html
    return None, None


def card_draft(
    params: SlacDrupalParams,
    source: SourceConfig,
    document: FetchedDocument,
    card: Card,
    details: Mapping[str, FetchedDocument],
    reference: date,
) -> EventDraft | None:
    """The draft of one card; ``None`` for a card that is no event or cannot be one."""
    if any_match(params.skip_title_patterns, card.title):
        return None
    url = absolute_url(document.final_url, card.link)
    detail = Detail()
    page = details.get(url) if url is not None else None
    if params.detail is not None and page is not None:
        detail = read_detail(params.detail, page)
    facts = (
        event_facts(detail.vevent, source, page.final_url)
        if detail.vevent is not None and page is not None
        else None
    )
    if params.native_id == "vevent_uid" and (facts is None or facts.native_id is None):
        return None
    title = detail.title or card.title or (facts.title if facts else None)
    if any_match(params.skip_title_patterns, title):
        return None
    start, end = (facts.start, facts.end) if facts else card_times(card.date_text, reference)
    if start is None:
        return None
    tba, tba_speakers = _tba(params, title)
    speakers = (
        make_speakers(card.speaker, card.affiliation)
        or _line_speakers(detail.speaker)
        or tba_speakers
    )
    abstract, abstract_html = _abstract(params, card, detail, facts)
    return EventDraft(
        source_id=source.id,
        extraction="html",
        native_id=facts.native_id if facts and params.native_id == "vevent_uid" else None,
        url=url,
        title=TBA_TITLE if tba else title,
        start=start,
        end=end,
        speakers=speakers,
        abstract=abstract,
        abstract_html=abstract_html,
    )


# --- fetching ----------------------------------------------------------------------------


def detail_targets(
    params: SlacDrupalParams,
    source: SourceConfig,
    documents: Sequence[FetchedDocument],
    window: TimeWindow,
) -> list[str]:
    """Detail pages to fetch, in list order; see the module docstring."""
    zone = ZoneInfo(source.timezone)
    wanted: list[str] = []
    foreign: list[str] = []
    for document in documents:
        reference = local_day(document.fetched_at, zone)
        for card in read_cards(params, LexborHTMLParser(document.text)):
            url = absolute_url(document.final_url, card.link)
            if url is None or any_match(params.skip_title_patterns, card.title):
                continue
            if outside_window(*card_times(card.date_text, reference), window, zone):
                continue
            if not on_source_host(source, document.final_url):
                raise ForeignHostError(source.id, document.final_url)
            if not on_source_host(source, url):
                foreign.append(url)
            elif url not in wanted:
                wanted.append(url)
    if foreign and not wanted and params.native_id == "vevent_uid":
        raise ForeignHostError(source.id, foreign[0])
    if foreign:
        logger.warning(
            "source %s: %d card(s) skipped: their detail page is on a host the source "
            "does not name (params.extra_hosts)",
            source.id,
            len(foreign),
        )
    if len(wanted) > params.max_detail_pages:
        logger.warning(
            "source %s: %d detail page(s) beyond max_detail_pages (%d) not fetched",
            source.id,
            len(wanted) - params.max_detail_pages,
            params.max_detail_pages,
        )
    return wanted[: params.max_detail_pages]


@register_adapter("slac_drupal")
class SlacDrupalAdapter:
    """Card lists and detail pages of the SLAC Drupal theme; see the module docstring."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        params = load_params(SlacDrupalParams, source)
        lists = [get_document(http, source, url, "list") for url in source.all_urls]
        details: list[FetchedDocument] = []
        if params.detail is not None:
            targets = detail_targets(params, source, lists, window)
            details = fetch_optional(http, source, targets, "detail")
        return make_payload(source, [*lists, *details], window=window)

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        params = load_params(SlacDrupalParams, source)
        lists = payload.labelled("list")
        if not lists:
            raise ParseError(f"source {source.id}: the payload has no list page")
        details = {document.url: document for document in payload.labelled("detail")}
        reference = local_day(payload.fetched_at, ZoneInfo(source.timezone))
        drafts: list[EventDraft] = []
        dropped = 0
        for document in lists:
            for card in self._cards(params, source, document):
                try:
                    draft = card_draft(params, source, document, card, details, reference)
                except (ValidationError, ValueError, OverflowError):
                    draft = None
                if draft is None:
                    dropped += 1
                else:
                    drafts.append(draft)
        if dropped:
            logger.info("source %s: %d card(s) gave no draft", source.id, dropped)
        return drafts

    @staticmethod
    def _cards(
        params: SlacDrupalParams, source: SourceConfig, document: FetchedDocument
    ) -> list[Card]:
        tree = LexborHTMLParser(document.text)
        cards = read_cards(params, tree)
        empty = params.cards.empty
        if not cards and not (empty and tree.css_first(empty) is not None):
            raise ParseError(
                f"source {source.id}: list.card {params.cards.card!r} matches nothing on "
                f"{ascii_printable(document.url)}"
            )
        return cards
