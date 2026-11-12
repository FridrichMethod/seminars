"""The ``drupal_jsonapi`` adapter: events of a Drupal site of the H&S template (node/hs_event).

The source's ``url`` is the JSON:API collection of the event nodes
(``<site>/jsonapi/node/hs_event``); with several ``urls`` each one is read
the same way. One request per page asks for everything at once::

    <url>?page[limit]=<page_limit>
         &sort=<sort, joined with ",">
         &include=<include, joined with ",">
         &filter[start][condition][path]=<window_filter.path>
         &filter[start][condition][operator]=>=
         &filter[start][condition][value]=<start of the run window>
         &filter[end][condition][path]=<window_filter.path>
         &filter[end][condition][operator]=<
         &filter[end][condition][value]=<end of the run window>
         &fields[<resource type>]=<names, joined with ","> for every entry of "fields"

Parameters without a value are left out. The two filter values change with
the day of the run and are recorded as volatile parameters. Then
``links.next.href`` of each answer is followed while there is one: only on
a host of the source (``ForeignHostError`` otherwise, before anything is
requested), never twice, and at most ``max_pages`` requests per URL. A
collection with more pages fails the source rather than returning part of
it.

Parameters (``params``; any other key is a ``ConfigError`` that names it,
``extra_hosts`` excepted):

* ``page_limit`` (1 to 50, default 50), ``max_pages`` (1 to 20, default 4).
* ``sort`` (list of field paths), ``include`` (list of relationships),
  ``fields`` (resource type -> list of field names, the sparse fieldsets).
* ``window_filter``: ``{path, value_format}``. ``path`` is the date
  property to filter on (``field_hs_event_date.value``); ``value_format``
  is ``unix_seconds`` (the format the H&S sites were verified with) or
  ``iso8601`` (UTC). Without it the request has no date filter.
* ``status_names``: name of a status term -> ``EventStatus``.
* ``title_status_pattern`` (regular expression) and ``title_status_names``
  (lower-cased word -> ``EventStatus``): the pattern is searched in the
  title; the text of its first group (or of the whole match), lower-cased,
  is looked up in ``title_status_names``. An event is ``cancelled`` when its
  status term or its title says so; otherwise a known status term gives
  the status, and without one there is no hint.
* ``localist_link_prefix``: events imported from a Localist calendar carry
  the address of their page there in ``field_hs_event_link.uri``, which
  starts with this prefix. ``exclude_localist_imports: true`` drops them
  (another source lists them already); it needs the prefix.
* ``event_type_names``: name of a series, type term or category term ->
  ``EventType``. Series titles are tried first, then type terms, then
  category terms, each in the order of the relationship; the first name
  with an entry gives ``event_type_hint``.
* ``title_patterns``: regular expressions tried in order on the title; the
  first that matches supplies its named groups ``series``, ``speaker``,
  ``affiliation`` and ``title``. ``title`` replaces the draft's title (the
  status is read from the full title before). ``speaker`` and
  ``affiliation`` are used only for an event without a speaker
  relationship, ``series`` only for one without a series relationship
  (and then also for ``event_type_names``).
* ``speaker_affiliation_attribute``: an attribute of the event node that
  holds the affiliation of its speakers; their labels are then names only.

Names (term names, series titles, speaker labels) and the title, before it
is matched, have their whitespace collapsed and stripped: sites write
``"Geometry "`` and ``" Lecture"``. A pattern is never applied to more
than ``MAX_PATTERN_INPUT`` characters.

How one ``data[]`` resource becomes a draft (or several, see dates):

* ``native_id``: the resource ``id`` (a UUID). ``url``: the site root plus
  ``attributes.path.alias``. ``title``: ``attributes.title``.
* Dates: ``attributes.field_hs_event_instances`` when it lists any (events
  imported from Localist: one entry per occurrence), else
  ``attributes.field_hs_event_date`` (one object, or a list of them). Each
  is ``{value, end_value, duration, ...}`` in UTC with ``+00:00``, and the
  draft keeps these timezone-aware times. ``end_value`` equal to ``value``
  (duration 0) means no end time. A date that starts at local midnight and
  ends at 23:59 (duration 1439 for one day) is all day: its draft has the
  local dates, the end being the last day, inclusive. An event with several
  dates gives one draft per date. Its first date (``rrule_index`` 0, or
  the first in the list when the dates have no ``rrule_index``) keeps the
  ``id`` as native id and every other one gets ``<id>/<rrule_index>`` (or
  ``<id>/<position>``), so an event that gains a date keeps the native id
  of the one it had. Without ``rrule_index``, removing a date from the
  list changes the native ids of the dates after it.
* ``location``: ``attributes.field_hs_event_location``; its lines (imports
  have three, joined with CR LF) are joined with ", ".
* ``abstract_html``: ``attributes.body.value``.
* ``speakers``: one per included speaker of the ``field_hs_event_speaker``
  relationship. Its label (``attributes.label``) reads ``Name
  (Affiliation)`` or ``Name, Affiliation`` (``Name, PhD`` stays a name);
  with ``speaker_affiliation_attribute`` it is the name only.
* ``series``: the first series of ``field_hs_event_series``.
* ``categories``: the names of the type, category and audience terms.
* ``is_online``: ``attributes.field_hs_event_experience`` (``inperson``,
  ``hybrid``, ``virtual``). ``stream_url``: ``field_hs_event_stream``.

Included resources are resolved by type and id; a name is the first of
``attributes.name``, ``attributes.title`` and ``attributes.label``. A
relationship to a resource that is not included contributes nothing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Annotated, Any, Final, Literal, Self
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from pydantic import Field, StringConstraints, ValidationError, field_validator, model_validator

from seminars.config import ConfigError
from seminars.httpbase import HttpClient
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
    AdapterError,
    ForeignHostError,
    ParseError,
    absolute_url,
    make_payload,
    on_source_host,
    register_adapter,
)

SHARED_PARAMS: Final = frozenset({"extra_hosts"})
"""Keys of ``params`` that the shared helpers read; they are not checked here."""

MAX_PATTERN_INPUT: Final = 2000
"""Longest text a configured regular expression is applied to."""

MAX_DATES_PER_EVENT: Final = 100
"""Most drafts made from the dates of one event."""

TITLE_GROUPS: Final = frozenset({"series", "speaker", "affiliation", "title"})

SPEAKER: Final = "field_hs_event_speaker"
SERIES: Final = "field_hs_event_series"
TYPE: Final = "field_hs_event_type"
STATUS: Final = "field_hs_event_status"
CATEGORY: Final = "field_hs_event_category"
AUDIENCE: Final = "field_hs_event_audience"

_ONLINE: Final[dict[str, bool]] = {"inperson": False, "hybrid": True, "virtual": True}
_NAME_SUFFIXES: Final = frozenset(
    {"phd", "ph.d.", "md", "m.d.", "jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
)
_LAST_MINUTE: Final = time(23, 59)

Name = Annotated[str, StringConstraints(min_length=1)]


class WindowFilter(Model):
    """The date property the request filters on, and how window times are written."""

    path: Name
    value_format: Literal["unix_seconds", "iso8601"] = "unix_seconds"


class DrupalParams(Model):
    """``params`` of a ``drupal_jsonapi`` source, validated."""

    page_limit: Annotated[int, Field(ge=1, le=50)] = 50
    max_pages: Annotated[int, Field(ge=1, le=20)] = 4
    sort: list[Name] = []
    window_filter: WindowFilter | None = None
    include: list[Name] = []
    fields: dict[str, list[Name]] = {}
    status_names: dict[str, EventStatus] = {}
    title_status_pattern: re.Pattern[str] | None = None
    title_status_names: dict[str, EventStatus] = {}
    localist_link_prefix: Name | None = None
    exclude_localist_imports: bool = False
    event_type_names: dict[str, EventType] = {}
    title_patterns: list[re.Pattern[str]] = []
    speaker_affiliation_attribute: Name | None = None

    @field_validator("title_patterns")
    @classmethod
    def _known_groups(cls, patterns: list[re.Pattern[str]]) -> list[re.Pattern[str]]:
        for pattern in patterns:
            unknown = sorted(set(pattern.groupindex) - TITLE_GROUPS)
            if unknown:
                raise ValueError(
                    f"unknown named group(s) {', '.join(unknown)} in {pattern.pattern!r}"
                )
        return patterns

    @model_validator(mode="after")
    def _prefix_for_exclusion(self) -> Self:
        if self.exclude_localist_imports and not self.localist_link_prefix:
            raise ValueError("exclude_localist_imports needs localist_link_prefix")
        return self


def drupal_params(source: SourceConfig) -> DrupalParams:
    """The validated ``params`` of ``source``; ``ConfigError`` names a bad or unknown key."""
    data = {key: value for key, value in source.params.items() if key not in SHARED_PARAMS}
    try:
        return DrupalParams.model_validate(data)
    except ValidationError as error:
        problems = "; ".join(
            f"params.{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors(include_url=False, include_input=False)
        )
        raise ConfigError(f"source {source.id} (adapter drupal_jsonapi): {problems}") from error


def _filter_value(moment: datetime, value_format: str) -> str:
    if value_format == "unix_seconds":
        return str(int(moment.timestamp()))
    return moment.astimezone(UTC).isoformat()


def first_page_url(endpoint: str, params: DrupalParams, window: TimeWindow) -> str:
    """The request for the first page of ``endpoint`` (see the module docstring)."""
    pairs: list[tuple[str, str]] = [("page[limit]", str(params.page_limit))]
    if params.sort:
        pairs.append(("sort", ",".join(params.sort)))
    if params.include:
        pairs.append(("include", ",".join(params.include)))
    if params.window_filter is not None:
        path, value_format = params.window_filter.path, params.window_filter.value_format
        for name, operator, moment in (("start", ">=", window.start), ("end", "<", window.end)):
            pairs += [
                (f"filter[{name}][condition][path]", path),
                (f"filter[{name}][condition][operator]", operator),
                (f"filter[{name}][condition][value]", _filter_value(moment, value_format)),
            ]
    pairs += [(f"fields[{kind}]", ",".join(names)) for kind, names in params.fields.items()]
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}{urlencode(pairs, safe='[],')}"


@register_adapter("drupal_jsonapi")
class DrupalJsonApiAdapter:
    """Reads ``node/hs_event`` collections through Drupal JSON:API; see the module docstring."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        params = drupal_params(source)
        documents: list[FetchedDocument] = []
        for endpoint in source.all_urls:
            documents.extend(
                _fetch_pages(http, source, first_page_url(endpoint, params, window), params)
            )
        return make_payload(source, documents, window=window)

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        params = drupal_params(source)
        drafts: list[EventDraft] = []
        for document in payload.documents:
            answer = _answer(document, source)
            included = _index(answer.get("included"))
            for resource in answer["data"]:
                if isinstance(resource, dict):
                    drafts.extend(_drafts(resource, included, document, source, params))
        return drafts


def _fetch_pages(
    http: HttpClient, source: SourceConfig, url: str, params: DrupalParams
) -> list[FetchedDocument]:
    documents: list[FetchedDocument] = []
    requested: set[str] = set()
    next_url: str | None = url
    while next_url is not None:
        if len(documents) == params.max_pages:
            raise AdapterError(
                f"source {source.id}: more than max_pages ({params.max_pages}) pages of events"
            )
        if next_url in requested:
            raise ParseError(f"source {source.id}: links.next repeats a page: {next_url}")
        if not on_source_host(source, next_url):
            raise ForeignHostError(source.id, next_url)
        requested.add(next_url)
        document = http.get(next_url)
        documents.append(document)
        next_url = _next_link(document, source)
    return documents


def _answer(document: FetchedDocument, source: SourceConfig) -> dict[str, Any]:
    try:
        data = document.parse_json()
    except ValueError as error:
        raise ParseError(f"source {source.id}: the answer is not JSON: {document.url}") from error
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ParseError(f"source {source.id}: the answer has no data list: {document.url}")
    return data


def _next_link(document: FetchedDocument, source: SourceConfig) -> str | None:
    links = _object(_answer(document, source).get("links"))
    following = links.get("next")
    href = following.get("href") if isinstance(following, dict) else following
    if href is None:
        return None
    link = absolute_url(document.final_url, href) if isinstance(href, str) else None
    if link is None:
        raise ParseError(f"source {source.id}: links.next is not a web address: {document.url}")
    return link


# --- small helpers for decoded JSON --------------------------------------------------------


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    """A non-empty string without surrounding whitespace, else ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _collapsed(value: object) -> str | None:
    """A non-empty string with its whitespace collapsed, else ``None``."""
    text = _text(value)
    return " ".join(text.split()) if text is not None else None


def _index(included: object) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for resource in included if isinstance(included, list) else []:
        if isinstance(resource, dict):
            kind, identifier = resource.get("type"), resource.get("id")
            if isinstance(kind, str) and isinstance(identifier, str):
                index[(kind, identifier)] = resource
    return index


def _related(
    resource: dict[str, Any], name: str, included: dict[tuple[str, str], dict[str, Any]]
) -> list[dict[str, Any]]:
    """The included resources of relationship ``name``, in its order."""
    data = _object(_object(resource.get("relationships")).get(name)).get("data")
    links = data if isinstance(data, list) else [data]
    found = []
    for link in links:
        if isinstance(link, dict):
            target = included.get((str(link.get("type")), str(link.get("id"))))
            if target is not None:
                found.append(target)
    return found


def _resource_name(resource: dict[str, Any]) -> str | None:
    attributes = _object(resource.get("attributes"))
    for key in ("name", "title", "label"):
        name = _collapsed(attributes.get(key))
        if name is not None:
            return name
    return None


def _names(
    resource: dict[str, Any], relationship: str, included: dict[tuple[str, str], dict[str, Any]]
) -> list[str]:
    names = (_resource_name(target) for target in _related(resource, relationship, included))
    return [name for name in names if name is not None]


def _link(value: object) -> str | None:
    """The address of a Drupal link field (``{uri, title}``) or of a plain string."""
    return _text(value.get("uri")) if isinstance(value, dict) else _text(value)


# --- one resource --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Title:
    """What the title of an event says, after ``title_patterns``."""

    title: str | None
    series: str | None = None
    speaker: str | None = None
    affiliation: str | None = None


def _drafts(
    resource: dict[str, Any],
    included: dict[tuple[str, str], dict[str, Any]],
    document: FetchedDocument,
    source: SourceConfig,
    params: DrupalParams,
) -> list[EventDraft]:
    """The drafts of one event resource; an empty list when it is malformed or excluded."""
    attributes = _object(resource.get("attributes"))
    identifier = resource.get("id")
    if not isinstance(identifier, str) or not identifier:
        return []
    if params.exclude_localist_imports and _is_import(attributes, params):
        return []
    try:
        common = _common(resource, attributes, included, document, source, params)
    except ValueError:  # pydantic's ValidationError is a ValueError
        return []
    dates = _dates(attributes)
    drafts = []
    for position, entry in enumerate(dates):
        key = _date_key(entry, position)
        native_id = identifier if key is None else f"{identifier}/{key}"
        try:
            start, end, all_day = _times(entry, source.timezone)
            drafts.append(common.evolve(native_id=native_id, start=start, end=end, all_day=all_day))
        except (ValueError, TypeError):
            continue
    return drafts


def _is_import(attributes: dict[str, Any], params: DrupalParams) -> bool:
    link = _link(attributes.get("field_hs_event_link"))
    return link is not None and link.startswith(params.localist_link_prefix or "")


def _common(
    resource: dict[str, Any],
    attributes: dict[str, Any],
    included: dict[tuple[str, str], dict[str, Any]],
    document: FetchedDocument,
    source: SourceConfig,
    params: DrupalParams,
) -> EventDraft:
    """Everything of the draft but the times; built with a placeholder start."""
    raw_title = _text(attributes.get("title"))
    parsed = _read_title(raw_title, params)
    series = _names(resource, SERIES, included) or ([parsed.series] if parsed.series else [])
    types = _names(resource, TYPE, included)
    categories = _names(resource, CATEGORY, included)
    speakers = _speakers(resource, attributes, included, params)
    if not speakers and parsed.speaker is not None:
        speakers = [SpeakerDraft(name=parsed.speaker, affiliation=parsed.affiliation)]
    path = _object(attributes.get("path")).get("alias")
    return EventDraft(
        source_id=source.id,
        extraction="api",
        url=absolute_url(document.final_url, path) if isinstance(path, str) else None,
        title=parsed.title,
        start=date(2000, 1, 1),
        speakers=speakers,
        series=series[0] if series else None,
        location=_location(attributes.get("field_hs_event_location")),
        is_online=_ONLINE.get(str(attributes.get("field_hs_event_experience"))),
        stream_url=absolute_url(document.final_url, _link(attributes.get("field_hs_event_stream"))),
        abstract_html=_text(_object(attributes.get("body")).get("value")),
        categories=list(
            dict.fromkeys([*types, *categories, *_names(resource, AUDIENCE, included)])
        ),
        event_type_hint=_event_type([series, types, categories], params),
        status_hint=_status(_names(resource, STATUS, included), raw_title, params),
    )


def _bounded(text: str | None) -> str | None:
    """``text`` with collapsed whitespace when a pattern may be applied to it."""
    collapsed = _collapsed(text)
    return collapsed if collapsed is not None and len(collapsed) <= MAX_PATTERN_INPUT else None


def _read_title(title: str | None, params: DrupalParams) -> _Title:
    text = _bounded(title)
    for pattern in params.title_patterns if text is not None else []:
        match = pattern.search(text or "")
        if match is None:
            continue
        groups = {key: _collapsed(value) for key, value in match.groupdict().items()}
        return _Title(
            title=groups.get("title") or title,
            series=groups.get("series"),
            speaker=groups.get("speaker"),
            affiliation=groups.get("affiliation"),
        )
    return _Title(title=title)


def split_speaker_label(label: str) -> tuple[str, str | None]:
    """``"Name (Affiliation)"`` or ``"Name, Affiliation"`` as ``(name, affiliation)``.

    The affiliation is the last balanced bracket at the end of the label,
    else what follows the first comma, unless that is a degree or a suffix
    (``"Jane Doe, PhD"``). Anything else is a name without affiliation.
    """
    text = " ".join(label.split())
    if text.endswith(")"):
        depth = 0
        for index in range(len(text) - 1, -1, -1):
            depth += {")": 1, "(": -1}.get(text[index], 0)
            if depth == 0:
                name, affiliation = text[:index].strip(), text[index + 1 : -1].strip()
                if name:
                    return name, affiliation or None
                break
    name, comma, rest = text.partition(",")
    if comma and name.strip() and rest.strip() and rest.strip().lower() not in _NAME_SUFFIXES:
        return name.strip(), rest.strip()
    return text, None


def _speakers(
    resource: dict[str, Any],
    attributes: dict[str, Any],
    included: dict[tuple[str, str], dict[str, Any]],
    params: DrupalParams,
) -> list[SpeakerDraft]:
    labels = _names(resource, SPEAKER, included)
    if params.speaker_affiliation_attribute is not None:
        affiliation = _collapsed(attributes.get(params.speaker_affiliation_attribute))
        return [SpeakerDraft(name=label, affiliation=affiliation) for label in labels]
    pairs = (split_speaker_label(label) for label in labels)
    return [SpeakerDraft(name=name, affiliation=affiliation) for name, affiliation in pairs]


def _location(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    lines = [" ".join(line.split()) for line in value.splitlines()]
    return ", ".join(line for line in lines if line) or None


def _event_type(names: Sequence[list[str]], params: DrupalParams) -> EventType | None:
    table: dict[str, EventType] = {
        " ".join(key.split()): value for key, value in params.event_type_names.items()
    }
    for kind in names:
        for name in kind:
            if name in table:
                return table[name]
    return None


def _status(terms: list[str], title: str | None, params: DrupalParams) -> EventStatus | None:
    table: dict[str, EventStatus] = {
        " ".join(key.split()): value for key, value in params.status_names.items()
    }
    found = [table[term] for term in terms if term in table]
    pattern, text = params.title_status_pattern, _bounded(title)
    match = pattern.search(text) if pattern is not None and text is not None else None
    if match is not None:
        word = (match.group(1) if match.re.groups else match.group(0)) or ""
        hint = params.title_status_names.get(word.lower())
        found += [hint] if hint is not None else []
    for status in ("cancelled", "tentative", "confirmed"):
        if status in found:
            return status
    return None


# --- dates ---------------------------------------------------------------------------------


def _dates(attributes: dict[str, Any]) -> list[dict[str, Any]]:
    instances = attributes.get("field_hs_event_instances")
    date_field = attributes.get("field_hs_event_date")
    candidates = instances if isinstance(instances, list) and instances else date_field
    entries = candidates if isinstance(candidates, list) else [candidates]
    unique: dict[tuple[object, object], dict[str, Any]] = {}
    for entry in entries:
        if isinstance(entry, dict):
            unique.setdefault((entry.get("value"), entry.get("end_value")), entry)
    return list(unique.values())[:MAX_DATES_PER_EVENT]


def _date_key(entry: dict[str, Any], position: int) -> str | None:
    """What the native id of one date adds to the resource id; ``None`` for the first date."""
    index = entry.get("rrule_index")
    key = index if isinstance(index, int) and not isinstance(index, bool) else position
    return None if key == 0 else str(key)


def _moment(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("a time must be a string")
    return datetime.fromisoformat(value.strip())


def _times(
    entry: dict[str, Any], timezone: str
) -> tuple[date | datetime, date | datetime | None, bool]:
    start = _moment(entry.get("value"))
    end_value = entry.get("end_value")
    end = None if end_value is None or end_value == entry.get("value") else _moment(end_value)
    if end is None or end <= start or start.tzinfo is None:
        return start, end, False
    zone = ZoneInfo(timezone)
    local_start, local_end = start.astimezone(zone), end.astimezone(zone)
    if local_start.time() != time.min or local_end.time().replace(second=0) != _LAST_MINUTE:
        return start, end, False
    first, last = local_start.date(), local_end.date()
    return first, (last if last > first else None), True
