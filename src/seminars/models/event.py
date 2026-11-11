"""The canonical event record stored in data/events.json."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import AfterValidator, Field, FiniteFloat, HttpUrl, model_validator

from seminars.models.base import (
    EventStatus,
    EventType,
    ExtractionMethod,
    FieldList,
    FieldTagMethod,
    Institution,
    Model,
    NonEmptyStr,
    SortedStrList,
    Timestamp,
    Uid,
    url_has_credential,
)


def _sorted_unique_urls(values: list[HttpUrl]) -> list[HttpUrl]:
    return sorted(set(values), key=str)


SortedUrlList = Annotated[list[HttpUrl], AfterValidator(_sorted_unique_urls)]


class Speaker(Model):
    """One speaker of an event."""

    name: NonEmptyStr
    affiliation: str | None = None
    url: HttpUrl | None = None


class Event(Model):
    """One talk: the record every stage after normalization works with.

    Conventions that every producer must follow (they keep git diffs small and
    make merging order-independent):

    * ``uid`` matches UID_PATTERN so it is safe in ICS UIDs, URLs and DOM ids.
    * ``start`` and ``end`` are stored as wall time of ``settings.timezone``
      with a numeric offset; ``first_seen`` and ``last_changed`` are UTC. All
      timestamps are truncated to whole seconds.
    * ``fields`` is validated against FIELD_IDS and kept in canonical order;
      ``source_ids`` and ``source_urls`` are kept sorted and unique. The order
      of ``speakers`` and ``picks_reasons`` is meaningful and preserved.
    * ``stream_url`` never carries a credential (see ``url_has_credential``).
    * Between normalization and classification ``fields`` is empty and
      ``field_tag_method`` is the placeholder ``"source"``.
    * ``missing_runs`` is internal state and never leaves data/events.json.
    """

    uid: Uid  # stable ID, never changes once assigned
    title: NonEmptyStr  # "TBA" allowed
    speakers: list[Speaker] = []
    series: str | None  # e.g. "Physical Chemistry Seminar"
    host_org: NonEmptyStr  # e.g. "Stanford Chemistry", "SLAC"
    institution: Institution
    event_type: EventType
    start: Timestamp  # timezone-aware
    end: Timestamp | None
    all_day: bool = False
    location: str | None
    is_online: bool = False
    stream_url: HttpUrl | None  # only if it carries no credential
    abstract: str | None  # plain text, cleaned
    url: HttpUrl  # preferred detail page
    source_urls: SortedUrlList  # all sources after merge
    source_ids: Annotated[SortedStrList, Field(min_length=1)]  # e.g. ["localist:44475127680618"]
    fields: FieldList  # multi-label, see taxonomy.yaml
    field_tag_method: FieldTagMethod
    picks_score: FiniteFloat = 0.0
    picks_reasons: list[str] = []  # matched keywords/rules, shown to the user
    status: EventStatus = "confirmed"
    extraction: ExtractionMethod
    first_seen: Timestamp
    last_changed: Timestamp
    missing_runs: Annotated[int, Field(ge=0)] = 0  # consecutive healthy runs without this event
    sequence: Annotated[int, Field(ge=0)] = 0  # +1 on content change, written to ICS SEQUENCE

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.end is not None and self.end < self.start:
            raise ValueError("end must not be earlier than start")
        if self.stream_url is not None and url_has_credential(self.stream_url):
            raise ValueError("stream_url carries a credential; drop it and set is_online instead")
        return self

    def is_pick(self, threshold: float) -> bool:
        """True when the event belongs in the picks feed (score >= threshold)."""
        return self.picks_score >= threshold
