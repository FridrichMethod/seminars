"""The ``localist`` adapter: the JSON API of a Localist calendar (events.stanford.edu).

The source's ``url`` is the events endpoint of the API (``.../api/2/events``).
For every entry of ``params.queries`` the adapter requests::

    <url>?type[]=<id>...&exclude_type[]=<id>...&group_id[]=<id>...
         &start=<D1>&end=<D2>&pp=<per_page>&page=<n>

D1 is the local day (in the source's timezone) on which the run window
starts and D2 the local day after the last day of the window: ``start`` is
inclusive and ``end`` exclusive, like the window itself. ``page`` starts at
1 and is sent with every request. Each answer carries
``page: {current, total, next_page, ...}``; the adapter follows
``next_page`` until it is null and never derives the number of pages from
``per_page`` (a fixture recorded with another page size has another number
of pages). At most ``MAX_PAGES_PER_QUERY`` pages are read per query; a
query that has more fails the source instead of returning part of it.

Never sent: ``distinct`` (it would merge the instances of a recurring event
into one record) and ``match`` (it turns all criteria into alternatives).
Several ``type[]`` values are alternatives, everything else narrows the
result. ``start``, ``end`` and ``pp`` are recorded as volatile parameters in
the fixtures; ``page`` is not.

Any HTTP error of any page (an unknown id in ``type[]`` or
``exclude_type[]`` is answered with HTTP 400 for the whole request, an
unknown ``group_id`` with 404) fails the whole fetch: the client raises
``HttpStatusError`` with the URL and the status, and the drafts of the other
queries are not used either. A partial result would look like a healthy
run, and the events of the failed query would count as gone from the
source.

Parameters (``params`` of the source; any other key is a ``ConfigError``
that names it, ``extra_hosts`` excepted):

* ``per_page`` (int, 1 to 100, default 100): the ``pp`` parameter. Localist
  cuts larger values to 100 without an error.
* ``queries`` (list, default: one query without filters): one series of
  requests each, with ``label`` (text, unique, copied to the label of the
  fetched documents), ``type_ids`` (repeated ``type[]``),
  ``exclude_type_ids`` (repeated ``exclude_type[]``) and ``group_ids``
  (repeated ``group_id[]``: department or group ids; several are
  alternatives, and together with ``type[]`` both must hold).
* ``event_types`` (list of ``{id, name, event_type, min_hours}``): an event
  type id of the calendar and the ``EventType`` it stands for. A record
  carries several types; the first entry of this list that it carries gives
  ``event_type_hint``. An entry with ``min_hours`` (a positive number) only
  counts for an instance that lasts at least that many hours, is all day or
  has no end: a one-hour seminar that its organizer also tagged as a
  conference takes the next entry it carries. ``name`` is a note for readers.
* ``department_fields`` / ``group_fields`` (mapping of a name to a list of
  field ids): ``departments[].name`` / ``groups[].name`` of a record, as
  the API writes them (both sides compared after stripping surrounding
  whitespace), to ``field_hints``. The hints of all departments and groups
  of a record are joined; a name that is not listed gives none.

How a record (``events[].event``) becomes a draft:

* ``native_id``: the id of its only event instance
  (``event_instances[0].event_instance.id``). Every record is one instance;
  a record whose instance id was already seen in the same run (an event
  that moved from one query to the other between two pages) is dropped.
* ``start`` / ``end``: the instance's ``start`` / ``end``, ISO 8601 with
  the UTC offset. ``end`` may be null. An ``all_day`` instance starts at
  local midnight and ends at null or 23:59 of the same day; its draft has
  the local date as ``start`` and, for an end on a later day, the last day
  (inclusive) as ``end``.
* ``url``: ``localist_url``, the event page on the calendar (``url`` is
  whatever the organizer entered: a form, a ticket shop, a home page).
* ``title``: ``title`` as it is (it can hold HTML entities).
* ``host_org``: the name of the first department in ``departments`` that
  has one; a record without a named department keeps the ``host_org`` of
  the source. Groups never count as the host.
* ``location``: ``location_name`` (else ``location``) and ``room_number``,
  joined with ", "; the street ``address`` only when both are missing.
* ``is_online``: ``experience``: ``inperson`` false, ``hybrid`` and
  ``virtual`` true. ``stream_url``: ``stream_url`` (it may carry a meeting
  password, which normalization removes). ``stream_info`` holds joining
  instructions with passcodes and is never read.
* ``abstract_html``: ``description``, which keeps its headings and
  paragraphs apart (``description_text`` runs them together: "OverviewThis
  activity"); ``abstract``: ``description_text`` only when the HTML has no
  text.
* ``categories``: the names of the record's event types, subjects,
  audiences, departments and groups. ``tags``: its tags, which whoever
  posts an event chooses freely ("mathematics" on a talk on the history
  of astronomy), so they never count as a department.
* ``status_hint``: ``cancelled`` for the statuses ``canceled``,
  ``cancelled`` and ``postponed``. ``live`` says nothing (the calendar keeps
  it on events whose title says that they are cancelled), so no hint.
* Records carry no speaker names (``speaker_ids`` are bare ids), so the
  draft has no speaker; normalization reads one from the title if it can.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Final
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, Field, StringConstraints, ValidationError, field_validator

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
    TimeWindow,
    canonical_fields,
)
from seminars.sources.base import (
    AdapterError,
    ParseError,
    absolute_url,
    make_payload,
    register_adapter,
)

MAX_PAGES_PER_QUERY: Final = 20
"""Most pages read for one query in one run (the per-host request budget of a run is 20)."""

SHARED_PARAMS: Final = frozenset({"extra_hosts"})
"""Keys of ``params`` that the shared helpers read; they are not checked here."""

_ONLINE: Final[dict[str, bool]] = {"inperson": False, "hybrid": True, "virtual": True}
_STATUS_HINTS: Final[dict[str, EventStatus]] = {
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "postponed": "cancelled",
}
_CATEGORY_FILTERS: Final = ("event_types", "event_subject", "event_audience")
_TAG: Final = re.compile(r"<[^>]*>")

FieldList = Annotated[list[str], AfterValidator(canonical_fields)]
"""Field ids, validated and in canonical order."""


class LocalistQuery(Model):
    """One series of requests (see the module docstring)."""

    label: Annotated[str, StringConstraints(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")]
    type_ids: list[int] = []
    exclude_type_ids: list[int] = []
    group_ids: list[int] = []


class LocalistEventType(Model):
    """An event type id of the calendar and the event type it stands for."""

    id: int
    name: str | None = None
    event_type: EventType
    min_hours: Annotated[float, Field(gt=0)] | None = None


class LocalistParams(Model):
    """``params`` of a ``localist`` source, validated."""

    per_page: Annotated[int, Field(ge=1, le=100)] = 100
    queries: Annotated[list[LocalistQuery], Field(min_length=1)] = [LocalistQuery(label="all")]
    event_types: list[LocalistEventType] = []
    department_fields: dict[str, FieldList] = {}
    group_fields: dict[str, FieldList] = {}

    @field_validator("queries")
    @classmethod
    def _unique_labels(cls, queries: list[LocalistQuery]) -> list[LocalistQuery]:
        labels = [query.label for query in queries]
        if len(set(labels)) != len(labels):
            raise ValueError("query labels must be unique")
        return queries


def localist_params(source: SourceConfig) -> LocalistParams:
    """The validated ``params`` of ``source``; ``ConfigError`` names a bad or unknown key."""
    data = {key: value for key, value in source.params.items() if key not in SHARED_PARAMS}
    try:
        return LocalistParams.model_validate(data)
    except ValidationError as error:
        problems = "; ".join(
            f"params.{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors(include_url=False, include_input=False)
        )
        raise ConfigError(f"source {source.id} (adapter localist): {problems}") from error


def window_days(window: TimeWindow, timezone: str) -> tuple[date, date]:
    """``(D1, D2)``: the first local day of ``window`` and the local day after its last one.

    The window is half-open, so its last day is the day of the moment just
    before ``window.end``.
    """
    zone = ZoneInfo(timezone)
    first = window.start.astimezone(zone).date()
    last = (window.end - timedelta(microseconds=1)).astimezone(zone).date()
    return first, last + timedelta(days=1)


def query_url(
    endpoint: str, query: LocalistQuery, *, days: tuple[date, date], per_page: int, page: int
) -> str:
    """The URL of one page of ``query``, parameters in the documented order."""
    pairs = [
        *(("type[]", str(value)) for value in query.type_ids),
        *(("exclude_type[]", str(value)) for value in query.exclude_type_ids),
        *(("group_id[]", str(value)) for value in query.group_ids),
        ("start", days[0].isoformat()),
        ("end", days[1].isoformat()),
        ("pp", str(per_page)),
        ("page", str(page)),
    ]
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}{urlencode(pairs, safe='[]')}"


def _endpoint(source: SourceConfig) -> str:
    urls = source.all_urls
    if len(urls) != 1:
        raise ConfigError(
            f"source {source.id} (adapter localist): needs exactly one url, the events "
            f"endpoint of the API; it has {len(urls)}"
        )
    return urls[0]


@register_adapter("localist")
class LocalistAdapter:
    """Reads the events of a Localist calendar through its JSON API; see the module docstring."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        params = localist_params(source)
        endpoint = _endpoint(source)
        days = window_days(window, source.timezone)
        documents: list[FetchedDocument] = []
        for query in params.queries:
            documents.extend(_fetch_query(http, source, endpoint, query, params, days))
        return make_payload(source, documents, window=window)

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        params = localist_params(source)
        seen: set[str] = set()
        drafts: list[EventDraft] = []
        for document in payload.documents:
            for record in _records(document, source):
                draft = _draft_or_none(record, document, source, params)
                if draft is None or draft.native_id in seen:
                    continue
                seen.add(draft.native_id or "")
                drafts.append(draft)
        return drafts


def _fetch_query(
    http: HttpClient,
    source: SourceConfig,
    endpoint: str,
    query: LocalistQuery,
    params: LocalistParams,
    days: tuple[date, date],
) -> list[FetchedDocument]:
    documents: list[FetchedDocument] = []
    page = 1
    for _ in range(MAX_PAGES_PER_QUERY):
        url = query_url(endpoint, query, days=days, per_page=params.per_page, page=page)
        document = http.get(url).evolve(label=query.label)
        documents.append(document)
        next_page = _next_page(document, source)
        if next_page is None:
            return documents
        if next_page <= page:
            raise ParseError(
                f"source {source.id}: page {page} of query {query.label} names page "
                f"{next_page} as the next one"
            )
        page = next_page
    raise AdapterError(
        f"source {source.id}: query {query.label} has more than {MAX_PAGES_PER_QUERY} pages"
    )


def _answer(document: FetchedDocument, source: SourceConfig) -> dict[str, Any]:
    """The decoded answer of one page; ``ParseError`` when it is no Localist event list."""
    try:
        data = document.parse_json()
    except ValueError as error:
        raise ParseError(f"source {source.id}: the answer is not JSON: {document.url}") from error
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ParseError(f"source {source.id}: the answer has no list of events: {document.url}")
    return data


def _next_page(document: FetchedDocument, source: SourceConfig) -> int | None:
    page = _answer(document, source).get("page")
    if not isinstance(page, dict):
        raise ParseError(f"source {source.id}: the answer has no page object: {document.url}")
    value = page.get("next_page")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParseError(f"source {source.id}: next_page is not a number: {document.url}")
    return value


def _records(document: FetchedDocument, source: SourceConfig) -> Iterator[dict[str, Any]]:
    for item in _answer(document, source)["events"]:
        record = item.get("event") if isinstance(item, dict) else None
        if isinstance(record, dict):
            yield record


def _draft_or_none(
    record: dict[str, Any], document: FetchedDocument, source: SourceConfig, params: LocalistParams
) -> EventDraft | None:
    """The draft of one record, or ``None`` for a malformed record (it is skipped)."""
    try:
        return _draft(record, document, source, params)
    except (ValueError, TypeError):  # pydantic's ValidationError is a ValueError
        return None


def _draft(
    record: dict[str, Any], document: FetchedDocument, source: SourceConfig, params: LocalistParams
) -> EventDraft:
    instance = _instance(record)
    native_id = instance.get("id")
    if isinstance(native_id, bool) or not isinstance(native_id, int | str) or native_id == "":
        raise ValueError("the instance has no id")
    start, end, all_day = _times(instance, source.timezone)
    departments = _names(record.get("departments"))
    groups = _names(record.get("groups"))
    abstract, abstract_html = _abstracts(record)
    return EventDraft(
        source_id=source.id,
        extraction="api",
        native_id=str(native_id),
        url=absolute_url(document.final_url, _text(record.get("localist_url"))),
        title=_text(record.get("title")),
        host_org=departments[0] if departments else None,
        start=start,
        end=end,
        all_day=all_day,
        location=_location(record),
        is_online=_ONLINE.get(str(record.get("experience"))),
        stream_url=absolute_url(document.final_url, _text(record.get("stream_url"))),
        abstract=abstract,
        abstract_html=abstract_html,
        field_hints=_field_hints(departments, params.department_fields)
        + _field_hints(groups, params.group_fields),
        categories=_categories(record, departments, groups),
        tags=_tags(record),
        event_type_hint=_event_type(record, params, _hours(start, end, all_day)),
        status_hint=_STATUS_HINTS.get(str(record.get("status")).strip().lower()),
    )


def _instance(record: dict[str, Any]) -> dict[str, Any]:
    instances = record.get("event_instances")
    first = instances[0] if isinstance(instances, list) and instances else None
    instance = first.get("event_instance") if isinstance(first, dict) else None
    if not isinstance(instance, dict):
        raise ValueError("the record has no event instance")
    return instance


def _moment(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("a time must be a string")
    return datetime.fromisoformat(value.strip())


def _times(
    instance: dict[str, Any], timezone: str
) -> tuple[date | datetime, date | datetime | None, bool]:
    start = _moment(instance.get("start"))
    end = None if instance.get("end") is None else _moment(instance.get("end"))
    if instance.get("all_day") is not True:
        return start, end, False
    first = _local_date(start, timezone)
    last = None if end is None else _local_date(end - timedelta(microseconds=1), timezone)
    return first, (last if last is not None and last > first else None), True


def _local_date(moment: datetime, timezone: str) -> date:
    return moment.date() if moment.tzinfo is None else moment.astimezone(ZoneInfo(timezone)).date()


def _text(value: object) -> str | None:
    """A non-empty string as it is (only surrounding whitespace removed), else ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _abstracts(record: dict[str, Any]) -> tuple[str | None, str | None]:
    """``(abstract, abstract_html)``: the HTML description when it has text, else the plain one."""
    markup = _text(record.get("description"))
    if markup is not None and _TAG.sub("", markup).strip():
        return None, markup
    return _text(record.get("description_text")), None


def _names(items: object) -> list[str]:
    """The stripped ``name`` of every ``{id, name}`` object of a list, in order."""
    if not isinstance(items, list):
        return []
    names = (_text(item.get("name")) for item in items if isinstance(item, dict))
    return [name for name in names if name is not None]


def _location(record: dict[str, Any]) -> str | None:
    name = _text(record.get("location_name")) or _text(record.get("location"))
    parts = [part for part in (name, _text(record.get("room_number"))) if part]
    return ", ".join(parts) if parts else _text(record.get("address"))


def _field_hints(names: Iterable[str], table: dict[str, list[str]]) -> list[str]:
    lookup = {key.strip(): fields for key, fields in table.items()}
    return [field for name in names for field in lookup.get(name, [])]


def _filters(record: dict[str, Any]) -> dict[str, Any]:
    filters = record.get("filters")
    return filters if isinstance(filters, dict) else {}


def _categories(record: dict[str, Any], departments: list[str], groups: list[str]) -> list[str]:
    filters = _filters(record)
    labels = [name for key in _CATEGORY_FILTERS for name in _names(filters.get(key))]
    return list(dict.fromkeys([*labels, *departments, *groups]))


def _tags(record: dict[str, Any]) -> list[str]:
    tags = record.get("tags")
    if not isinstance(tags, list):
        return []
    return list(dict.fromkeys(tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()))


def _hours(start: date | datetime, end: date | datetime | None, all_day: bool) -> float | None:
    """How long a timed instance lasts; ``None`` when all day or without a comparable end."""
    if all_day or not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    if (start.tzinfo is None) != (end.tzinfo is None):
        return None
    return (end - start).total_seconds() / 3600


def _event_type(
    record: dict[str, Any], params: LocalistParams, hours: float | None
) -> EventType | None:
    types = _filters(record).get("event_types")
    carried = (
        {item.get("id") for item in types if isinstance(item, dict)}
        if isinstance(types, list)
        else set()
    )
    for entry in params.event_types:
        if entry.id not in carried:
            continue
        if entry.min_hours is not None and hours is not None and hours < entry.min_hours:
            continue
        return entry.event_type
    return None
