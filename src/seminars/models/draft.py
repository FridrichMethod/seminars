"""What source adapters exchange with the pipeline: fetched documents and event drafts."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta
from itertools import islice
from types import MappingProxyType
from typing import Annotated, Any, Final, Self
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

# --- how much of a draft is kept ---------------------------------------------------------
#
# Normalization reads a line field of stored limit L as at most 4 L characters
# of text, of which it takes at most 8 times as much raw input (markup
# included), so 32 L characters in all (seminars.pipeline.normalize and
# seminars.pipeline.text, RAW_FACTOR). A draft keeps one character more,
# so that normalization still sees where it cuts. tests/test_pipeline_draft_limits.py
# ties every number here to the limits of normalization.

MAX_ROLLOVER: Final = timedelta(hours=6)
"""An end that the clock puts before its start on the same day is on the next day
when that is at most this much after the start ("11pm - 1am"); otherwise there is none.
Normalization and the adapters that read times of day apply the same rule."""

MAX_DRAFT_URL: Final = 2000
"""A draft address longer than this counts as missing; normalization would not use it."""

MAX_DRAFT_NATIVE_ID: Final = 2000
"""A native id longer than this counts as missing: no source uses one that long."""

DRAFT_TEXT_LIMITS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "title": 9601,  # 32 x 300
        "speaker_text": 64001,  # 32 x 2000
        "series": 6401,  # 32 x 200
        "host_org": 6401,  # 32 x 200
        "location": 9601,  # 32 x 300
        "abstract": 128001,  # 32 x 4000, the default of events.abstract_max_chars
        "abstract_html": 128001,
        "source_text": 262144,  # the page text an LLM answer was grounded in
    }
)
"""Characters a text field of ``EventDraft`` keeps at most; the rest is cut off."""

MAX_DRAFT_SPEAKER_NAME: Final = 3841  # 32 x 120
MAX_DRAFT_AFFILIATION: Final = 9601  # 32 x 300
MAX_DRAFT_CATEGORY: Final = 3201  # 32 x 100

MAX_DRAFT_SPEAKERS: Final = 40
"""Structured speakers a draft keeps; normalization keeps 20 different people."""

MAX_DRAFT_CATEGORIES: Final = 100
"""Categories (and tags) a draft keeps; normalization keeps the first 50 that are not empty."""

MAX_DRAFT_TIME: Final = 121
"""Characters a start or end written as text keeps; normalization reads at most 120."""

MAX_DRAFT_CHARACTERS: Final = (
    sum(DRAFT_TEXT_LIMITS.values())
    + 2 * MAX_DRAFT_TIME
    + 2 * MAX_DRAFT_URL
    + MAX_DRAFT_NATIVE_ID
    + MAX_DRAFT_SPEAKERS * (MAX_DRAFT_SPEAKER_NAME + MAX_DRAFT_AFFILIATION + MAX_DRAFT_URL)
    + 2 * MAX_DRAFT_CATEGORIES * MAX_DRAFT_CATEGORY
)
"""Characters of text, times, addresses and lists one draft holds at most (about 1.9 million).

The other fields are of a fixed small size: the source id, the time zone,
the field ids, flags and names of a kind. A draft of ASCII text therefore
takes about as many bytes, and one of other text up to four times as many.
"""


def _cut(value: object, limit: int) -> object:
    """A string longer than ``limit`` cut to it; anything else as it is."""
    return value[:limit] if isinstance(value, str) and len(value) > limit else value


def _bounded_address(value: object, limit: int = MAX_DRAFT_URL) -> object:
    """``None`` for a string longer than ``limit``; anything else as it is."""
    return None if isinstance(value, str) and len(value) > limit else value


def _first(value: object, count: int) -> object:
    """The first ``count`` items of a list or other iterable; anything else as it is."""
    if isinstance(value, list | tuple):
        return list(value[:count]) if len(value) > count else value
    if isinstance(value, Iterable) and not isinstance(value, str | bytes | Mapping):
        return list(islice(value, count))
    return value


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

    @model_validator(mode="before")
    @classmethod
    def _keep_bounded(cls, data: Any) -> Any:
        """Cut name and affiliation to what a draft keeps; a too long address is missing."""
        if not isinstance(data, Mapping):
            return data
        bounded: dict[Any, Any] = dict(data)
        if "name" in bounded:
            bounded["name"] = _cut(bounded["name"], MAX_DRAFT_SPEAKER_NAME)
        if "affiliation" in bounded:
            bounded["affiliation"] = _cut(bounded["affiliation"], MAX_DRAFT_AFFILIATION)
        if "url" in bounded:
            bounded["url"] = _bounded_address(bounded["url"])
        return bounded


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
      of the source (departments, subjects, event types) for the rule
      based tagger and the filter. ``tags`` are keywords that whoever posts
      an event may attach (the tags of a Stanford Events record): a tag
      "mathematics" on a talk about the history of astronomy names no
      department, so tags never count as a department or an out-of-scope
      category; the filter and the LLM tagger read them like categories.
    * ``event_type_hint`` / ``status_hint`` are set only when the source
      states them explicitly; otherwise the source defaults apply.
    * ``stream_url`` may carry a credential; normalization drops such URLs.
    * ``source_text`` is the text the draft was extracted from. It is
      mandatory for ``extraction == "llm"`` because every LLM-extracted field
      must be grounded in it.

    * Sizes are bounded when a draft is built, so that no page can make
      the drafts of its events large, however many events share one long
      text: a text field keeps at most ``DRAFT_TEXT_LIMITS`` characters (a
      speaker's name ``MAX_DRAFT_SPEAKER_NAME``, an affiliation
      ``MAX_DRAFT_AFFILIATION``, a category ``MAX_DRAFT_CATEGORY``), which
      is more than normalization reads; ``speakers`` keeps the first
      ``MAX_DRAFT_SPEAKERS`` and ``categories`` and ``tags`` the first
      ``MAX_DRAFT_CATEGORIES`` (a tag is cut like a category). A longer
      address (``MAX_DRAFT_URL``) or native id (``MAX_DRAFT_NATIVE_ID``)
      is not cut but counts as missing, and a start or end written as text
      keeps ``MAX_DRAFT_TIME`` characters, one more than normalization
      reads. Text below its limit is kept as the very object it was given.
      One draft therefore holds at most ``MAX_DRAFT_CHARACTERS``
      characters (about 1.9 million, about 1.9 MB of ASCII text). Drafts
      that share a text too long for a field each keep a cut copy of their
      own, so a thousand of them can take a thousand times as much.

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
    tags: list[str] = []
    event_type_hint: EventType | None = None
    status_hint: EventStatus | None = None
    source_text: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _keep_bounded(cls, data: Any) -> Any:
        """Bound text, lists and addresses to what a draft keeps (see the class)."""
        if not isinstance(data, Mapping):
            return data
        bounded: dict[Any, Any] = dict(data)
        for name, limit in DRAFT_TEXT_LIMITS.items():
            if name in bounded:
                bounded[name] = _cut(bounded[name], limit)
        for name in ("start", "end"):
            if name in bounded:
                bounded[name] = _cut(bounded[name], MAX_DRAFT_TIME)
        for name in ("url", "stream_url"):
            if name in bounded:
                bounded[name] = _bounded_address(bounded[name])
        if "native_id" in bounded:
            bounded["native_id"] = _bounded_address(bounded["native_id"], MAX_DRAFT_NATIVE_ID)
        if "speakers" in bounded:
            bounded["speakers"] = _first(bounded["speakers"], MAX_DRAFT_SPEAKERS)
        for name in ("categories", "tags"):
            if name in bounded:
                labels = _first(bounded[name], MAX_DRAFT_CATEGORIES)
                if isinstance(labels, list | tuple):
                    labels = [_cut(label, MAX_DRAFT_CATEGORY) for label in labels]
                bounded[name] = labels
        return bounded

    @model_validator(mode="after")
    def _check_grounding_text(self) -> Self:
        if self.extraction == "llm" and not (self.source_text or "").strip():
            raise ValueError("source_text is required when extraction is 'llm'")
        return self

    @property
    def is_all_day(self) -> bool:
        """True when the draft describes an all-day event (flag set, or ``start`` is a date)."""
        return self.all_day or not isinstance(self.start, datetime | str)
