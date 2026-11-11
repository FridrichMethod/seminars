"""What source adapters exchange with the pipeline: fetched documents and event drafts."""

from __future__ import annotations

import contextlib
import json
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, Self
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, PlainValidator, model_validator

from seminars.models.base import (
    EventStatus,
    EventType,
    ExtractionMethod,
    FieldList,
    HttpUrlStr,
    Model,
    NonEmptyStr,
    SourceId,
    Timestamp,
    TimezoneName,
)
from seminars.textio import normalize_body
from seminars.textrules import without_surrogates

BodyText = Annotated[str, AfterValidator(normalize_body)]
"""Text of a response body in canonical form (see ``seminars.textio.normalize_body``)."""


def _draft_moment(value: object) -> datetime | date | str:
    """Validate a draft time and give ISO 8601 strings their typed form.

    A string that is exactly a date becomes a ``date`` and a string that is
    an ISO 8601 date and time becomes a ``datetime``; any other string is
    kept for normalization to parse. The same draft therefore has the same
    field types whether it was built in code, loaded from a dict or read
    back from JSON, for example from the LLM cache.
    """
    if isinstance(value, date):  # datetime is a subclass of date
        return value
    if not isinstance(value, str):
        raise ValueError("expected a datetime, a date or a string")
    text = value.strip()
    if not text:
        raise ValueError("an empty string is not a time")
    with contextlib.suppress(ValueError):
        return date.fromisoformat(text)
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(text)
    return value


DraftMoment = Annotated[
    datetime | date | str,
    PlainValidator(_draft_moment, json_schema_input_type=datetime | date | str),
]
"""Start or end of a draft; see ``EventDraft`` for what each type means."""


class TimeWindow(Model):
    """Half-open interval ``[start, end)`` of event start times a run cares about."""

    start: Timestamp
    end: Timestamp

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.end <= self.start:
            raise ValueError("window end must be later than window start")
        return self

    @classmethod
    def around(cls, now: datetime, *, days_back: int, days_ahead: int, timezone: str) -> Self:
        """Window of whole local days: ``days_back`` before today to ``days_ahead`` after.

        ``start`` is local midnight of (today - days_back) and ``end`` is local
        midnight after (today + days_ahead), both in ``timezone``.
        """
        zone = ZoneInfo(timezone)
        today = now.astimezone(zone).date()
        start = datetime.combine(today - timedelta(days=days_back), time.min, tzinfo=zone)
        end = datetime.combine(today + timedelta(days=days_ahead + 1), time.min, tzinfo=zone)
        return cls(start=start, end=end)

    def contains(self, moment: datetime) -> bool:
        """True when ``moment`` (timezone-aware) lies inside the window."""
        return self.start <= moment < self.end


class FetchedDocument(Model):
    """One response body as returned by ``HttpClient.get`` (live or offline).

    Only what a recorded fixture can reproduce is exposed, so adapters behave
    the same online and offline: there are no response headers, and ``text``
    is always in the form a fixture file has. Whatever a client received, a
    leading byte order mark is gone, line endings are LF and the text ends
    with a newline (an empty body is a single newline). Parsers therefore
    never see CRLF, not even in ICS feeds.
    """

    url: str  # the URL that was requested
    final_url: str  # after redirects; equals ``url`` when there was none
    status: int
    content_type: str | None = None  # media type without parameters, lower case
    text: BodyText  # decoded body in canonical form: no BOM, LF only, final newline
    fetched_at: Timestamp
    from_cache: bool = False  # body served from the conditional-request cache (HTTP 304)
    label: str | None = None  # free for adapters, e.g. "index" or "detail"

    def parse_json(self) -> Any:
        """Parse the body as JSON (raises ``ValueError`` when it is not JSON).

        An unpaired ``\\ud83d`` escape decodes to a lone surrogate, which
        cannot be stored or sent anywhere. Such characters are dropped from
        every key and string of the result
        (``seminars.textrules.without_surrogates``); do the same when you
        decode JSON yourself, for example a JSON-LD block of a page.

        JSON that is nested deeper than the decoder can follow (tens of
        thousands of levels) is a ``ValueError`` as well, never a
        ``RecursionError``.
        """
        try:
            data = json.loads(self.text)
        except RecursionError as error:
            raise ValueError("JSON is nested too deeply") from error
        return without_surrogates(data)


class RawPayload(Model):
    """Everything one adapter fetched for one source in one run."""

    source_id: SourceId
    fetched_at: Timestamp  # reference time for parsing, e.g. to infer a missing year
    window: TimeWindow  # the window the fetch was asked for, e.g. to expand recurrences
    documents: list[FetchedDocument] = []

    def labelled(self, label: str) -> list[FetchedDocument]:
        """Documents carrying ``label``, in fetch order."""
        return [document for document in self.documents if document.label == label]


class SpeakerDraft(Model):
    """Speaker as found in the source; cleaned and validated by normalization.

    ``url`` is an absolute http(s) URL or ``None``, like every URL of a draft
    (see ``EventDraft``).
    """

    name: NonEmptyStr
    affiliation: str | None = None
    url: HttpUrlStr | None = None


class EventDraft(Model):
    """What ``Adapter.parse`` returns for one event, before normalization.

    Adapters copy what the source says and leave cleaning to normalization.
    Text fields may contain HTML entities, stray tags and odd whitespace.
    Times and URLs are different: normalization sees neither the page nor
    the format they came from, so their meaning is fixed here.

    * ``native_id``: the id the source itself uses (Localist instance id,
      JSON:API resource id, RSS guid, ICS UID). It is the best basis for a
      stable uid; leave it ``None`` when the source has none.
    * ``start`` / ``end``, by type:

      - an aware ``datetime`` is used as it is;
      - a naive ``datetime`` is wall time in ``timezone`` (default: the
        source timezone);
      - a ``date`` has no time of day. As ``start`` it makes the draft an
        all-day event;
      - a string is ISO 8601 or something ``dateutil`` can parse. ISO 8601
        strings are converted when the draft is built: a date-only string
        such as ``"2026-11-17"`` is the same as ``date(2026, 11, 17)``, so
        it means an all-day event and never midnight, and
        ``"2026-11-17T16:00"`` is the same as the naive ``datetime``. Other
        strings stay strings.

      A draft whose start cannot be understood is dropped and reported as
      ``invalid``.
    * All-day events: a draft is all-day when ``all_day`` is true or
      ``start`` is a ``date`` (``is_all_day``). Its ``end`` is the LAST DAY
      of the event, inclusive, the way a page states it: a workshop on 17,
      18 and 19 November has ``end=date(2026, 11, 19)``, and ``None`` or the
      start day means a single day. A source that gives the day after the
      last one (DTEND of an all-day ICS event) is converted by its adapter,
      which subtracts one day. When ``end`` is a ``datetime`` instead, the
      last day is the day that contains the moment just before ``end`` (so
      midnight after the last day and 23:59 on the last day both work).
      Normalization turns this into the exclusive end of ``Event``.
    * URLs (``url``, ``stream_url``, ``speakers[].url``) are absolute http(s)
      URLs or ``None``; anything else is rejected when the draft is built.
      The adapter resolves every link against the page it came from with
      ``seminars.sources.base.absolute_url(document.final_url, link)``, which
      also returns ``None`` for links that are not web pages (``mailto:``,
      ``javascript:``).
    * ``speakers`` holds structured speakers; ``speaker_text`` holds a raw
      line such as "Speaker: Jane Doe (MIT)" that normalization splits.
    * ``abstract`` is plain text, ``abstract_html`` is HTML to be converted.
    * ``field_hints`` are canonical field ids the adapter can infer (for
      example through a department map); ``categories`` are free-text labels
      of the source (departments, subjects, event types, tags) for the rule
      based tagger and the filter.
    * ``event_type_hint`` / ``status_hint`` are set only when the source
      states them explicitly; otherwise the source defaults apply.
    * ``stream_url`` may carry a credential; normalization drops such URLs.
    * ``source_text`` is the text the draft was extracted from. It is
      mandatory for ``extraction == "llm"`` because every LLM-extracted field
      must be grounded in it.

    Building a draft validates it, and a value that breaks these rules
    raises ``pydantic.ValidationError``. Adapters treat that like any other
    malformed item: skip the item, keep the rest.
    """

    source_id: SourceId
    extraction: ExtractionMethod
    native_id: str | None = None
    url: HttpUrlStr | None = None  # detail page
    title: str | None = None
    start: DraftMoment
    end: DraftMoment | None = None
    all_day: bool = False
    timezone: TimezoneName | None = None
    speakers: list[SpeakerDraft] = []
    speaker_text: str | None = None
    series: str | None = None
    host_org: str | None = None  # overrides the source's host_org
    location: str | None = None
    is_online: bool | None = None  # None: unknown, decide during normalization
    stream_url: HttpUrlStr | None = None
    abstract: str | None = None
    abstract_html: str | None = None
    field_hints: FieldList = []
    categories: list[str] = []
    event_type_hint: EventType | None = None
    status_hint: EventStatus | None = None
    source_text: str | None = None

    @model_validator(mode="after")
    def _check_grounding_text(self) -> Self:
        if self.extraction == "llm" and not (self.source_text or "").strip():
            raise ValueError("source_text is required when extraction is 'llm'")
        return self

    @property
    def is_all_day(self) -> bool:
        """True when the draft describes an all-day event (flag set, or ``start`` is a date)."""
        return self.all_day or not isinstance(self.start, datetime | str)
