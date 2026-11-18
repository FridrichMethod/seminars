"""Public site payloads: public/events.json and public/status.json.

Written by seminars.outputs.site_data and read by the static site (plain
JavaScript). Both files are pretty-printed JSON produced by
``seminars.models.dump_json``. Every key is always present (``null`` when
there is no value), so the site never has to test for missing keys. Anything
internal (``missing_runs``, ``sequence``, source ids, tagging method) is left
out on purpose.

``schema_version`` (``PUBLIC_SCHEMA_VERSION``) changes only when a key is
removed or changes its meaning. A key that is added has a default, which is
also what the site assumes while a payload lacks the key, so a page that is
newer than its data (or the other way round) keeps working.

Picks reasons. The stored events hold every reason of their picks score as
one line of plain ASCII text (``Event.picks_reasons``), which is what the
ICS feeds show. ``PickReason`` is the same reason taken apart, so that the
site and the digest can say it in the reader's language. The text forms
are defined here, once, for both directions: the picks scorer
(``seminars.classify.picks``) builds its lines with ``PickReason.text``, and
``PickReason.parse`` reads them back.
"""

from __future__ import annotations

import contextlib
import re
from typing import Final, Literal, Self

from pydantic import FiniteFloat, HttpUrl, ValidationError, model_validator

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


PickReasonKind = Literal["keyword", "series", "speaker", "penalty", "other"]
"""What a picks reason is about.

``keyword``: a keyword of the interests found in ``place``; ``series``: a
series of the allow-list; ``speaker``: a speaker of the watch-list;
``penalty``: an exclusion term; ``other``: a line in none of these forms
(written by something other than the rule scorer), kept as it is in ``term``.
"""

PickReasonPlace = Literal["title", "series", "abstract"]
"""Where a keyword was found."""

_POINTS: Final = r"\((?P<points>[+-](?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)\)"
_REASON_LINES: Final = tuple(
    (kind, re.compile(pattern, re.DOTALL))
    for kind, pattern in (
        ("keyword", rf'"(?P<term>.+)" in (?P<place>title|series|abstract) {_POINTS}'),
        ("series", rf'series "(?P<term>.+)" {_POINTS}'),
        ("speaker", rf'speaker "(?P<term>.+)" {_POINTS}'),
        ("penalty", rf'penalty "(?P<term>.+)" {_POINTS}'),
    )
)
"""The line of each kind, as ``PickReason.text`` writes it."""


def signed_points(points: float) -> str:
    """Points as a reason writes them: ``+3``, ``-2``, ``+0.6`` (never ``-0``)."""
    value = points + 0.0  # turns a negative zero into zero
    return f"+{value:g}" if value >= 0 else f"-{-value:g}"


class PickReason(Model):
    """One reason of a picks score: what matched, and how many points it gave.

    ``term`` is the keyword, series or speaker name as the configuration
    spells it, or the whole line for ``kind="other"``. ``place`` is set for
    keywords only, and ``points`` (negative for a penalty) for every kind
    but ``other``.
    """

    kind: PickReasonKind
    term: NonEmptyStr
    place: PickReasonPlace | None = None
    points: FiniteFloat | None = None

    @model_validator(mode="after")
    def _check_parts(self) -> Self:
        if (self.place is not None) != (self.kind == "keyword"):
            raise ValueError("a picks reason has a place exactly when it is a keyword")
        if (self.points is None) != (self.kind == "other"):
            raise ValueError("a picks reason has points unless its kind is other")
        return self

    @property
    def text(self) -> str:
        """The line of plain text the stored event and the ICS feeds hold."""
        if self.kind == "other":
            return self.term
        points = signed_points(self.points or 0.0)
        if self.kind == "keyword":
            return f'"{self.term}" in {self.place} ({points})'
        return f'{self.kind} "{self.term}" ({points})'

    @classmethod
    def parse(cls, text: str) -> Self:
        """The reason ``text`` is the line of (``other`` for a line of no known form).

        The points are read as the line writes them, with six significant
        digits. A line of a known form whose parts are no reason (points
        that overflow to infinity, ``+1e999``) is a line of no known form
        too. ``text`` must not be empty.
        """
        for kind, pattern in _REASON_LINES:
            found = pattern.fullmatch(text)
            if found is not None:
                values = {**found.groupdict(), "kind": kind, "points": float(found["points"])}
                with contextlib.suppress(ValidationError):
                    return cls.model_validate(values)
                break
        return cls(kind="other", term=text)


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
    ``meeting`` is ``Event.is_meeting``: a conference, an all-day workshop
    of two days or more, or a workshop with times of day that lasts at
    least three hours (what the site's filter of the kind calls conferences
    and workshops). Every other event is a talk, a workshop listed all day
    on a single day among them.
    """

    uid: Uid
    title: NonEmptyStr
    speakers: list[Speaker] = []
    series: str | None = None
    host_org: NonEmptyStr
    institution: Institution
    event_type: EventType
    meeting: bool = False
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
    picks_reasons: list[PickReason] = []  # see PickReason
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
            meeting=event.is_meeting,
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
            picks_reasons=[PickReason.parse(text) for text in event.picks_reasons if text],
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
    """One row of the status page.

    ``read_in_full`` is false when the last run delivered events but not
    all of them: ``skipped_pages`` pages could not be fetched, or the
    listing had ``listed`` usable items where it usually has
    ``usual_size``. ``resized_from`` is set when the last run took its
    smaller listing (``listed`` items) as the usual size, in place of
    ``resized_from``. A disabled source, and one that never ran, has
    nothing of the kind.
    """

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
    read_in_full: bool = True  # false: the last run is known to have missed events
    skipped_pages: Count = 0  # pages the last run could not fetch and went on without
    listed: Count | None = None  # usable items of the last run, when its size was judged
    usual_size: Count | None = None  # the usual number, when the last run listed far fewer
    resized_from: Count | None = None  # the usual number the last run gave up for ``listed``


class PublicStatusPayload(Model):
    """Content of public/status.json, sources sorted by id.

    ``llm_provider`` and ``llm_stored_answers`` are those of ``LastRun``:
    neither of them means that the last run used rules only.
    """

    schema_version: Literal[1] = 1
    generated_at: Timestamp
    last_run_at: Timestamp | None = None  # when the last run finished
    last_run_ok: bool | None = None
    event_count: Count = 0  # events in public/events.json
    llm_provider: str | None = None  # provider used by the last run
    llm_stored_answers: Count | None = None  # stored answers used while no provider answered
    sources: list[PublicSourceStatus] = []
