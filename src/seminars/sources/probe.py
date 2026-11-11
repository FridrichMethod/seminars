"""The format probe behind ``seminars add-source URL`` (contract ``SourceProber``).

``probe_source(http, url)`` makes exactly one request, ``http.get(url)``,
and recognises the format of the answer from cheap signs only:

* ``ics``: Content-Type ``text/calendar`` or a body that starts with
  ``BEGIN:VCALENDAR``;
* ``rss``: an RSS, RDF or Atom root element (any XML Content-Type);
* ``localist``: a Localist API answer (``events[].event`` with
  ``event_instances``), or a page of a Localist calendar (its
  ``LOCALIST_SETTINGS`` script), for which the API address is suggested;
* ``drupal_jsonapi``: Content-Type ``application/vnd.api+json`` or a JSON
  document with a ``jsonapi`` member or ``data[].type`` such as
  ``node--hs_event``;
* ``jsonld``: an HTML page with schema.org Event objects in
  ``application/ld+json`` blocks;
* ``html``: any other HTML page. The probe never writes selectors: it
  suggests ``llm_page`` and says in the notes that ``html_list`` needs them,
  and it lists the feeds and calendars the page links to (not fetched);
* ``unknown``: anything else.

The answer carries a suggested ``config/sources.d`` entry that loads as a
``SourceConfig`` as it is (group ``new``, institution ``other``, no
fields): ASCII only, with ``CHANGE`` in the values a person has to check.
Nothing a site sent reaches the notes unescaped.
"""

from __future__ import annotations

import re
from typing import Any, Final
from urllib.parse import urlsplit

import yaml
from selectolax.lexbor import LexborHTMLParser

from seminars.http import HttpClient
from seminars.models import FetchedDocument, ProbeResult, ascii_url
from seminars.sources.base import absolute_url
from seminars.sources.generic.jsonld import decode, find_events, script_blocks
from seminars.textrules import ascii_printable

HEAD_CHARACTERS: Final = 4096
"""How much of a body the probe looks at for markers that sit at the top."""

_FEED_ROOT: Final = re.compile(r"<(?:rss|feed|rdf:RDF)\b")
_FEED_TYPES: Final = ("application/rss+xml", "application/atom+xml", "application/rdf+xml")
_XML_TYPES: Final = (*_FEED_TYPES, "application/xml", "text/xml")
_JSON_TYPES: Final = ("application/json", "application/vnd.api+json", "text/json")
_MARKUP_TITLE: Final = re.compile(r"<title>\s*<(?:h\d|a|span|div)\b", re.IGNORECASE)
_LINKED_FEEDS: Final = (
    'link[rel="alternate"][type="application/rss+xml"]',
    'link[rel="alternate"][type="application/atom+xml"]',
    'link[rel="alternate"][type="text/calendar"]',
    'a[href$=".ics"]',
)


def probe_source(http: HttpClient, url: str) -> ProbeResult:
    """Fetch ``url`` once, recognise its format and suggest a sources.d entry."""
    document = http.get(url)
    kind, adapter, target, params, notes = detect(document)
    return ProbeResult(
        url=url,
        format=kind,
        content_type=document.content_type,
        suggested_yaml=suggested_yaml(target or url, adapter, params),
        notes=[ascii_printable(note) for note in notes],
    )


Detection = tuple[Any, str, str | None, dict[str, Any], list[str]]
"""Format, adapter, URL to configure (``None``: the probed one), params, notes."""


def detect(document: FetchedDocument) -> Detection:
    head = document.text.lstrip()[:HEAD_CHARACTERS]
    kind = document.content_type or ""
    if kind == "text/calendar" or head.startswith("BEGIN:VCALENDAR"):
        events = document.text.count("BEGIN:VEVENT")
        return "ics", "ics", None, {}, [f"iCalendar with {events} VEVENT(s)"]
    if kind in _XML_TYPES or head.startswith("<?xml") or _FEED_ROOT.match(head):
        if _FEED_ROOT.search(head):
            return detect_feed(document, head)
        if kind in _XML_TYPES:
            return "unknown", "llm_page", None, {}, ["XML, but no RSS or Atom feed"]
    if kind in _JSON_TYPES or kind == "application/ld+json" or head[:1] in ("{", "["):
        return detect_json(document)
    if kind == "text/html" or "<html" in head.lower() or "<!doctype html" in head.lower():
        return detect_html(document)
    return (
        "unknown",
        "llm_page",
        None,
        {},
        [f"content type {kind or 'unknown'} is not a known format"],
    )


def detect_feed(document: FetchedDocument, head: str) -> Detection:
    atom = "<feed" in head
    items = document.text.count("<entry") if atom else document.text.count("<item")
    markup = _MARKUP_TITLE.search(document.text[:200_000]) is not None
    feed: dict[str, Any] = {"start": "published" if atom else "pubDate", "native_id": "guid"}
    if markup:
        feed["title_is_markup"] = True
    notes = [
        f"{'Atom' if atom else 'RSS'} feed with {items} item(s)",
        "CHANGE feed.start if the feed dates are publication times (WordPress) "
        "and not the start of the events",
    ]
    if markup:
        notes.append("item titles hold HTML markup (Stanford Sites template B)")
    return "rss", "rss", None, {"feed": feed}, notes


def detect_json(document: FetchedDocument) -> Detection:
    try:
        data = document.parse_json()
    except ValueError:
        return "unknown", "llm_page", None, {}, ["the body is not valid JSON"]
    if document.content_type == "application/vnd.api+json" or is_jsonapi(data):
        types = ", ".join(resource_types(data)) or "none"
        return (
            "drupal_jsonapi",
            "drupal_jsonapi",
            None,
            {},
            [
                f"Drupal JSON:API, resource types: {types}",
                "see config/sources.d/stanford-sites.yaml for the params of drupal_jsonapi",
            ],
        )
    if is_localist(data):
        return (
            "localist",
            "localist",
            None,
            {},
            [
                "Localist API answer",
                "see config/sources.d/localist.yaml for the params of localist",
            ],
        )
    if find_events(data):
        return "jsonld", "jsonld", None, {}, ["a JSON-LD document with Event objects"]
    return "unknown", "llm_page", None, {}, ["JSON, but neither Localist nor Drupal JSON:API"]


def resource_types(data: Any) -> list[str]:
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return sorted(
        {str(item["type"]) for item in items if isinstance(item, dict) and "type" in item}
    )


def is_jsonapi(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    items = data.get("data")
    typed = isinstance(items, list) and any(
        isinstance(item, dict) and "--" in str(item.get("type", "")) for item in items
    )
    return "jsonapi" in data or typed


def is_localist(data: Any) -> bool:
    events = data.get("events") if isinstance(data, dict) else None
    return isinstance(events, list) and any(
        isinstance(item, dict)
        and isinstance(item.get("event"), dict)
        and "event_instances" in item["event"]
        for item in events
    )


def detect_html(document: FetchedDocument) -> Detection:
    tree = LexborHTMLParser(document.text)
    notes = linked_feeds(tree, document.final_url)
    events = [event for block in script_blocks(document) for event in find_events(decode(block))]
    if "LOCALIST_SETTINGS" in document.text:
        parts = urlsplit(document.final_url)
        api = f"{parts.scheme}://{parts.netloc}/api/2/events"
        notes = [f"a Localist calendar page; its API is {api}", *notes]
        if events:
            notes.append("the page also has JSON-LD events; the API has more fields")
        return "localist", "localist", api, {}, notes
    if events:
        return "jsonld", "jsonld", None, {}, [f"{len(events)} JSON-LD Event object(s)", *notes]
    generator = tree.css_first('meta[name="generator" i]')
    if generator is not None and "drupal" in (generator.attributes.get("content") or "").lower():
        notes.append(
            "a Drupal site: try /jsonapi/node/hs_event (H&S template) or /events/all/rss "
            "(Stanford Sites template)"
        )
    notes.append("no structured events found; html_list needs selectors written by hand")
    return "html", "llm_page", None, {}, notes


def linked_feeds(tree: LexborHTMLParser, base: str) -> list[str]:
    """Feeds and calendars the page links to, as notes (none of them is fetched)."""
    found: list[str] = []
    for selector in _LINKED_FEEDS:
        for node in tree.css(selector):
            link = absolute_url(base, node.attributes.get("href"))
            if link is not None and link not in found:
                found.append(link)
    return [f"the page links {link}" for link in found[:10]]


def suggested_yaml(url: str, adapter: str, params: dict[str, Any]) -> str:
    """A sources.d entry for ``url`` that loads as it is; ASCII only."""
    address = ascii_url(url.strip())
    host = (urlsplit(address).hostname or "example").lower()
    slug = "-".join(re.findall(r"[a-z0-9]+", host))[:56].strip("-") or "source"
    entry: dict[str, Any] = {
        "id": f"new-{slug}",
        "name": f"CHANGE {host}",
        "group": "new",
        "adapter": adapter,
        "url": address,
        "institution": "other",
        "host_org": f"CHANGE {host}",
        "default_fields": [],
    }
    if params:
        entry["params"] = params
    return yaml.safe_dump({"sources": [entry]}, sort_keys=False, allow_unicode=False, width=100)
