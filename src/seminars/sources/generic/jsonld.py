"""Generic adapter ``jsonld``: schema.org Event objects embedded in pages as application/ld+json.

``fetch`` requests every URL of the source once. ``parse`` reads every
``<script type="application/ld+json">`` block of every page and turns each
Event object into a draft: top-level objects, arrays, ``@graph`` lists and
any nesting (``itemListElement`` of an ItemList, ``item`` of a ListItem).
A type counts as an event when its name is ``Event`` or ends in ``Event``
(EducationEvent, SocialEvent, ...), written plainly or as an IRI. The
``subEvent`` objects of an event are events of their own. A node that is
only ``{"@id": ...}`` is replaced by the node with that id from the same
block.

How an Event maps to a draft:

* ``name`` -> title; ``description`` -> abstract (``abstract_html`` when it
  holds markup); ``url`` -> url (the page itself when the page has exactly
  one event and the event names no url); ``@id`` -> native id;
  ``superEvent`` -> series; ``keywords`` -> categories.
* ``startDate`` / ``endDate``: ISO 8601 with or without offset (a naive
  time is local to the source) or a date only (an all-day event whose
  ``endDate`` is its last day). Other text is passed on for normalization.
* ``location``: a Place (name and address), a PostalAddress, plain text, a
  VirtualLocation (online; its url is the stream url) or a list of them
  (hybrid).
* ``eventAttendanceMode``: Online and Mixed mean online, Offline means not.
* ``eventStatus``: EventCancelled and EventPostponed mean cancelled (a
  postponed date does not take place), EventScheduled, EventRescheduled and
  EventMovedOnline confirmed (the last one also online).
* ``performer`` -> speakers (name, ``affiliation`` or ``worksFor``, url);
  ``organizer`` -> host_org.

A page without any JSON-LD block, or whose blocks are all invalid JSON, is
not understood (``ParseError``); one invalid block among valid ones is
skipped. Pages whose JSON-LD describes no event yield nothing.

``params`` has no keys of its own (``extra_hosts`` is read by the shared
host check); any other key is an error that names it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Mapping
from typing import Any, Final

from pydantic import ValidationError
from selectolax.lexbor import LexborHTMLParser

from seminars.http import HttpClient
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
from seminars.sources.generic.html_list import collapse, load_params, parse_moment
from seminars.textrules import ascii_printable, without_surrogates

logger = logging.getLogger(__name__)

MAX_NODES: Final = 100_000
"""Nodes of one JSON-LD block that are looked at; bounds the work on a hostile page."""

_MARKUP: Final = re.compile(r"<[a-zA-Z/][^<>]{0,200}>")
_STATUS: Final[dict[str, EventStatus]] = {
    "EventCancelled": "cancelled",
    "EventPostponed": "cancelled",
    "EventScheduled": "confirmed",
    "EventRescheduled": "confirmed",
    "EventMovedOnline": "confirmed",
}
_ONLINE_MODES: Final = {
    "OnlineEventAttendanceMode": True,
    "MixedEventAttendanceMode": True,
    "OfflineEventAttendanceMode": False,
}
_ADDRESS_PARTS: Final = (
    "streetAddress",
    "addressLocality",
    "addressRegion",
    "postalCode",
    "addressCountry",
)


class JsonLdParams(Model):
    """No keys besides ``extra_hosts``."""


# --- reading the blocks ------------------------------------------------------------------


def script_blocks(document: FetchedDocument) -> list[str]:
    """The texts of the application/ld+json scripts of a page."""
    tree = LexborHTMLParser(document.text)
    blocks = []
    for script in tree.css("script[type]"):
        kind = (script.attributes.get("type") or "").split(";")[0].strip().lower()
        if kind == "application/ld+json":
            blocks.append(script.text(deep=True, strip=False))
    return blocks


def decode(text: str) -> Any:
    """A JSON-LD block, or ``None`` when it is not JSON (lone surrogates dropped)."""
    try:
        return without_surrogates(json.loads(text))
    except (ValueError, RecursionError):
        return None


def last_segment(value: object) -> str | None:
    """``Event`` for "Event", "https://schema.org/Event" or "schema:Event"."""
    if not isinstance(value, str):
        return None
    return re.split(r"[/#:]", value.strip())[-1] or None


def types_of(node: Mapping[str, Any]) -> list[str]:
    value = node.get("@type")
    values = value if isinstance(value, list) else [value]
    return [name for item in values if (name := last_segment(item))]


def is_event(node: Mapping[str, Any]) -> bool:
    return any(name.endswith("Event") for name in types_of(node))


def walk(root: Any) -> Iterator[Any]:
    """Every list and dict below ``root`` (depth first, bounded by MAX_NODES)."""
    stack = [root]
    seen = 0
    while stack and seen < MAX_NODES:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict | list):
            yield node
            children = node.values() if isinstance(node, dict) else node
            stack.extend(reversed([child for child in children if isinstance(child, dict | list)]))


def find_events(root: Any) -> list[dict[str, Any]]:
    """Event objects in a decoded block, in document order; sub-events included."""
    return [node for node in walk(root) if isinstance(node, dict) and is_event(node)]


def node_ids(root: Any) -> dict[str, dict[str, Any]]:
    return {
        node["@id"]: node
        for node in walk(root)
        if isinstance(node, dict) and isinstance(node.get("@id"), str) and len(node) > 1
    }


# --- values ------------------------------------------------------------------------------


def text_of(value: Any) -> str | None:
    """A plain value: a string, the first string of a list, or ``{"@value": ...}``."""
    if isinstance(value, list):
        return next((text for item in value if (text := text_of(item))), None)
    if isinstance(value, dict):
        return text_of(value.get("@value"))
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return collapse(value) if isinstance(value, str) else None


def as_list(value: Any, ids: Mapping[str, dict[str, Any]]) -> list[Any]:
    """``value`` as a list, references to other nodes resolved."""
    values = value if isinstance(value, list) else [] if value is None else [value]
    return [resolve(item, ids) for item in values]


def resolve(value: Any, ids: Mapping[str, dict[str, Any]]) -> Any:
    if isinstance(value, dict) and set(value) == {"@id"}:
        return ids.get(value["@id"], value)
    return value


def address_text(value: Any) -> str | None:
    if isinstance(value, dict):
        parts = [text_of(value.get(key)) or text_of_name(value.get(key)) for key in _ADDRESS_PARTS]
        return ", ".join(part for part in parts if part) or text_of(value.get("name"))
    return text_of(value)


def text_of_name(value: Any) -> str | None:
    return text_of(value.get("name")) if isinstance(value, dict) else None


def place_text(place: Mapping[str, Any]) -> str | None:
    name = text_of(place.get("name"))
    address = address_text(place.get("address"))
    if "PostalAddress" in types_of(place):
        address = address_text(place)
    if name and address and not address.startswith(name):
        return f"{name}, {address}"
    return name or address


def read_location(
    value: Any, ids: Mapping[str, dict[str, Any]], base: str
) -> tuple[str | None, str | None, bool]:
    """Location text, stream url and whether a virtual location is among them."""
    places: list[str] = []
    stream = None
    virtual = False
    for location in as_list(value, ids):
        if isinstance(location, dict) and "VirtualLocation" in types_of(location):
            virtual = True
            stream = stream or absolute_url(base, text_of(location.get("url")))
        elif isinstance(location, dict):
            if text := place_text(location):
                places.append(text)
        elif text := text_of(location):
            places.append(text)
    return "; ".join(places) or None, stream, virtual


def read_speakers(value: Any, ids: Mapping[str, dict[str, Any]], base: str) -> list[SpeakerDraft]:
    speakers = []
    for performer in as_list(value, ids):
        if isinstance(performer, dict):
            name = text_of(performer.get("name"))
            organisation = resolve(performer.get("affiliation") or performer.get("worksFor"), ids)
            if isinstance(organisation, list):
                organisation = resolve(organisation[0], ids) if organisation else None
            affiliation = text_of_name(organisation) or text_of(organisation)
            link = absolute_url(base, text_of(performer.get("url") or performer.get("sameAs")))
        else:
            name, affiliation, link = text_of(performer), None, None
        if name:
            speakers.append(SpeakerDraft(name=name, affiliation=affiliation, url=link))
    return speakers


def first_name(value: Any, ids: Mapping[str, dict[str, Any]]) -> str | None:
    for item in as_list(value, ids):
        if name := text_of_name(item) or text_of(item):
            return name
    return None


def moment(value: Any) -> Any:
    text = text_of(value)
    return parse_moment(text) or text


def keywords(value: Any) -> list[str]:
    if isinstance(value, str):
        return [word for part in value.split(",") if (word := collapse(part))]
    return [word for item in value if (word := text_of(item))] if isinstance(value, list) else []


def event_draft(
    event: Mapping[str, Any],
    ids: Mapping[str, dict[str, Any]],
    source: SourceConfig,
    page: FetchedDocument,
    *,
    only_event: bool,
) -> EventDraft | None:
    """The draft of one Event object; ``None`` for an object without a start."""
    start = moment(event.get("startDate"))
    if start is None:
        return None
    base = page.final_url
    location, stream, virtual = read_location(event.get("location"), ids, base)
    mode = last_segment(text_of(event.get("eventAttendanceMode")))
    status = last_segment(text_of(event.get("eventStatus")))
    online = _ONLINE_MODES.get(mode or "")
    if virtual or status == "EventMovedOnline":
        online = True
    description = text_of(event.get("description"))
    markup = description is not None and _MARKUP.search(description) is not None
    url = absolute_url(base, text_of(event.get("url"))) or (base if only_event else None)
    native_id = event.get("@id")
    return EventDraft(
        source_id=source.id,
        extraction="jsonld",
        native_id=native_id if isinstance(native_id, str) and native_id.strip() else None,
        url=url,
        title=text_of(event.get("name")),
        start=start,
        end=moment(event.get("endDate")),
        speakers=read_speakers(event.get("performer") or event.get("performers"), ids, base),
        series=first_name(event.get("superEvent"), ids),
        host_org=first_name(event.get("organizer"), ids),
        location=location,
        is_online=online,
        stream_url=stream,
        abstract=None if markup else description,
        abstract_html=description if markup else None,
        categories=keywords(event.get("keywords")),
        status_hint=_STATUS.get(status or ""),
    )


# --- the adapter -------------------------------------------------------------------------


def page_events(
    document: FetchedDocument,
) -> list[tuple[dict[str, Any], dict[str, dict[str, Any]]]]:
    """Every Event of a page with the id table of its block; ``ParseError`` when unreadable."""
    blocks = script_blocks(document)
    url = ascii_printable(document.url)
    if not blocks:
        raise ParseError(f"no application/ld+json block on {url}")
    decoded = [data for data in map(decode, blocks) if data is not None]
    if not decoded:
        raise ParseError(f"no application/ld+json block on {url} is valid JSON")
    if len(decoded) < len(blocks):
        logger.warning("%d invalid JSON-LD block(s) skipped on %s", len(blocks) - len(decoded), url)
    events = []
    for data in decoded:
        ids = node_ids(data)
        events.extend((event, ids) for event in find_events(data))
    return events


@register_adapter("jsonld")
class JsonLdAdapter:
    """schema.org Events in JSON-LD blocks; see the module docstring."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        load_params(JsonLdParams, source)
        return fetch_urls(http, source, window=window, label="page")

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        load_params(JsonLdParams, source)
        drafts: list[EventDraft] = []
        for document in payload.documents:
            events = page_events(document)
            for event, ids in events:
                try:
                    draft = event_draft(event, ids, source, document, only_event=len(events) == 1)
                except ValidationError:
                    logger.debug("source %s: a malformed Event was skipped", source.id)
                    continue
                if draft is not None:
                    drafts.append(draft)
        return drafts
