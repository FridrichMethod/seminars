"""Public site payloads: public/events.json and public/status.json.

Written by seminars.outputs.site_data and read by the static site (plain
JavaScript). Both files are pretty-printed JSON produced by
``seminars.models.dump_json``. Every key is always present (``null`` when
there is no value), so the site never has to test for missing keys. Anything
internal (``missing_runs``, ``sequence``, source ids, tagging method) is left
out on purpose.
"""

from __future__ import annotations

from typing import Final, Literal, Self

from pydantic import HttpUrl

from seminars.models.base import (
    FEED_IDS,
    FIELD_IDS,
    EventStatus,
    EventType,
    FieldList,
    Institution,
    Model,
    NonEmptyStr,
    SourceId,
    Timestamp,
    TimezoneName,
    Uid,
)
from seminars.models.event import Event, Speaker
from seminars.models.report import Count

PUBLIC_SCHEMA_VERSION: Final = 1
FEEDS_DIRNAME: Final = "feeds"

PublicSourceState = Literal["ok", "empty", "failed", "disabled", "unknown"]
"""Last known state of a source; ``unknown`` means it has never run."""


def feed_path(feed_id: str) -> str:
    """Site-relative path of a feed, e.g. ``feeds/math.ics``."""
    return f"{FEEDS_DIRNAME}/{feed_id}.ics"


class PublicFeed(Model):
    """One ICS feed the site offers for subscription.

    Display name and description come from the locale files
    (``feeds.<id>.name`` and ``feeds.<id>.description``).
    """

    id: NonEmptyStr
    path: NonEmptyStr  # relative to the site root, e.g. "feeds/math.ics"


class PublicEvent(Model):
    """The slim event record of public/events.json.

    All text comes from scraped web pages and is untrusted: the site must
    render it with ``textContent`` and must only link http(s) URLs.
    """

    uid: Uid
    title: NonEmptyStr
    speakers: list[Speaker] = []
    series: str | None = None
    host_org: NonEmptyStr
    institution: Institution
    event_type: EventType
    start: Timestamp  # ISO 8601 with offset, wall time of the payload's timezone
    end: Timestamp | None = None
    all_day: bool = False
    location: str | None = None
    is_online: bool = False
    stream_url: HttpUrl | None = None
    abstract: str | None = None
    url: HttpUrl
    fields: FieldList
    is_pick: bool = False
    picks_reasons: list[str] = []
    status: EventStatus = "confirmed"

    @classmethod
    def from_event(cls, event: Event, *, picks_threshold: float) -> Self:
        """Project a stored event onto the public schema."""
        return cls(
            uid=event.uid,
            title=event.title,
            speakers=event.speakers,
            series=event.series,
            host_org=event.host_org,
            institution=event.institution,
            event_type=event.event_type,
            start=event.start,
            end=event.end,
            all_day=event.all_day,
            location=event.location,
            is_online=event.is_online,
            stream_url=event.stream_url,
            abstract=event.abstract,
            url=event.url,
            fields=event.fields,
            is_pick=event.is_pick(picks_threshold),
            picks_reasons=event.picks_reasons,
            status=event.status,
        )


class PublicEventsPayload(Model):
    """Content of public/events.json.

    ``events`` holds every stored event of the current window (past ones
    included, cancelled ones included) sorted by ``start`` and then ``uid``;
    the site decides what to show. ``generated_at`` is the "last updated"
    time shown on the page.
    """

    schema_version: Literal[1] = 1
    generated_at: Timestamp
    timezone: TimezoneName
    fields: list[str] = list(FIELD_IDS)  # field ids in display order
    feeds: list[PublicFeed] = [PublicFeed(id=feed, path=feed_path(feed)) for feed in FEED_IDS]
    events: list[PublicEvent] = []


class PublicSourceStatus(Model):
    """One row of the status page."""

    id: SourceId
    name: NonEmptyStr
    group: NonEmptyStr
    url: str | None = None  # human-facing page of the source
    state: PublicSourceState
    events: Count = 0  # events the source delivered in its last run
    last_run_at: Timestamp | None = None
    last_success_at: Timestamp | None = None
    consecutive_failures: Count = 0
    error: str | None = None  # short message of the last failure, if it failed


class PublicStatusPayload(Model):
    """Content of public/status.json, sources sorted by id."""

    schema_version: Literal[1] = 1
    generated_at: Timestamp
    last_run_at: Timestamp | None = None  # when the last run finished
    last_run_ok: bool | None = None
    event_count: Count = 0  # events in public/events.json
    llm_provider: str | None = None  # provider used by the last run; null means rules only
    sources: list[PublicSourceStatus] = []
