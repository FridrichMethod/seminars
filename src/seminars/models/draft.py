"""What source adapters exchange with the pipeline: fetched documents and event drafts."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, Self
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, model_validator

from seminars.models.base import (
    EventStatus,
    EventType,
    ExtractionMethod,
    FieldList,
    Model,
    NonEmptyStr,
    SourceId,
    Timestamp,
    TimezoneName,
)
from seminars.textio import normalize_body

BodyText = Annotated[str, AfterValidator(normalize_body)]
"""Text of a response body in canonical form (see ``seminars.textio.normalize_body``)."""


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
        """Parse the body as JSON (raises ``ValueError`` when it is not JSON)."""
        return json.loads(self.text)


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
    """Speaker as found in the source; cleaned and validated by normalization."""

    name: NonEmptyStr
    affiliation: str | None = None
    url: str | None = None


class EventDraft(Model):
    """What ``Adapter.parse`` returns for one event, before normalization.

    Adapters copy what the source says and leave cleaning to normalization.
    Text fields may contain HTML entities, stray tags and odd whitespace.

    * ``native_id``: the id the source itself uses (Localist instance id,
      JSON:API resource id, RSS guid, ICS UID). It is the best basis for a
      stable uid; leave it ``None`` when the source has none.
    * ``start`` / ``end``: an aware datetime is used as is; a naive datetime
      is wall time in ``timezone`` (default: the source timezone); a ``date``
      means an all-day event; a string must be ISO 8601 or something
      ``dateutil`` can parse. A draft whose start cannot be understood is
      dropped and reported as ``invalid``.
    * ``speakers`` holds structured speakers; ``speaker_text`` holds a raw
      line such as "Speaker: Jane Doe (MIT)" that normalization splits.
    * ``abstract`` is plain text, ``abstract_html`` is HTML to be converted.
    * ``field_hints`` are canonical field ids the adapter can infer (for
      example through a department map); ``categories`` are free-text labels
      of the source (departments, subjects, event types, tags) for the rule
      based tagger and the filter.
    * ``event_type_hint`` / ``status_hint`` are set only when the source
      states them explicitly; otherwise the source defaults apply.
    * ``stream_url`` is raw and may carry a credential; normalization drops
      such URLs.
    * ``source_text`` is the text the draft was extracted from. It is
      mandatory for ``extraction == "llm"`` because every LLM-extracted field
      must be grounded in it.
    """

    source_id: SourceId
    extraction: ExtractionMethod
    native_id: str | None = None
    url: str | None = None  # detail page
    title: str | None = None
    start: datetime | date | str
    end: datetime | date | str | None = None
    all_day: bool = False
    timezone: TimezoneName | None = None
    speakers: list[SpeakerDraft] = []
    speaker_text: str | None = None
    series: str | None = None
    host_org: str | None = None  # overrides the source's host_org
    location: str | None = None
    is_online: bool | None = None  # None: unknown, decide during normalization
    stream_url: str | None = None
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
