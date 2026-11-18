"""Custom adapter ``registry``: conferences and workshops that no feed carries, kept by hand.

Some meetings are announced months ahead on pages that no enabled source
reads: the annual meeting of a society in San Francisco, a symposium that a
center posts once, the workshops of an institute whose site needs a browser.
The registry lists them in its own source entry
(config/sources.d/conferences.yaml), one entry per edition, with the facts
of the organizer's own page and nothing else: no description, no contact,
no projected date. ``fetch`` requests nothing (its payload has no document,
and ``fetched_at`` is the start of the window it was asked for); ``parse``
turns the entries into all-day drafts.

Keys of ``params`` (an unknown key is an error that names it):

* ``allow_empty`` (default false): with ``true``, a run that has no meeting
  to give is healthy (``seminars.outputs.health.allows_empty``), so a
  registry whose meetings are all over opens no ``source-broken`` issue.
* ``meetings``: the editions (``Meeting``), each a mapping of

  - ``id``: ``<series slug>-<year>``, lower-case words joined by hyphens
    that end in the year the edition starts in, at most 63 characters. It
    is the native id of the event, whose uid is ``<source id>.<id>`` for
    good: an id is never renamed or used for another edition;
  - ``title``, ``series``, ``host_org`` and ``location`` (venue and city),
    one line each;
  - ``short_title`` (optional): the name of the edition in the feed of the
    deadlines, at most ``MAX_SHORT_TITLE`` characters ("GEM at ICLR
    2027"); without it the name is the one ``display_names`` gives;
  - ``start`` and ``end``: local dates, ``end`` inclusive and at most
    ``MAX_MEETING_DAYS`` days after ``start``;
  - ``region``: ``bay-area`` (an event) or ``world`` (never an event; such
    an entry only carries its milestones);
  - ``url``: the https address of the organizer's page of the edition;
  - ``fields``: one or more field ids;
  - ``event_type``: ``conference`` or ``workshop``; ``status``:
    ``confirmed`` or ``tentative`` (a date that is only saved, say);
  - ``verified``: ``date`` and ``url`` (https) of the last check of the
    facts on the organizer's page;
  - ``milestones`` (optional): dates of the edition (``Milestone``), each
    with ``kind`` (``MILESTONE_KINDS``), ``date`` (on or before ``end``),
    ``time`` (``"HH:MM"`` in quotes, or none), ``zone`` (``ZONE_NAMES`` or
    an IANA name, or none; without a time it is the zone of the day, as in
    "January 22 AoE"), ``qualifier`` (``exact``, the default, or ``by``: at
    the latest on that day, as in "opens by February 1") and ``label``, a
    few words of our own ("Poster deadline");
  - ``notes`` (optional): lines for the curator; they are never published.

Every text is printable ASCII on one line, without white space at either
end. Two traps of YAML 1.1, which the loader speaks, shape the keys: an
unquoted ``on``, ``off``, ``yes`` or ``no`` is a boolean (so the key of the
day of a check is ``date``, and a qualifier is ``exact`` or ``by``), and an
unquoted ``23:59`` is the number 1439 (so a time is written in quotes). A
boolean key of ``verified``, a boolean qualifier and a number as a time
get an error that says so.

A run gets one draft for every ``bay-area`` edition that has not ended
before the window starts (``payload.window``), in the order of the entries:
all day from ``start`` to ``end``, native id ``id``, ``url``, ``title``,
``series``, ``host_org``, ``location``, ``fields`` as field hints (the
field tagger keeps them and may add fields that the title or the abstract
names), ``event_type`` and ``status`` as stated. An edition that is over
leaves the listing, as on a page of upcoming events, so a registry of past
meetings gives no draft (see ``allow_empty``). An edition further ahead than the
window reaches is left to normalization, which counts it as outside the
window until the window reaches it. The abstract is one line per
milestone from the first day of the window on, by date:
"Poster deadline: 2027-03-30 23:59 PT", "Registration opens: by
2027-02-01". The window starts some days before the run (the days the
feeds keep a talk after it took place), so a milestone stays listed for
those days after it passed; nothing in an adapter knows the run itself.

Every milestone of an edition, ``bay-area`` and ``world`` alike, is also a
deadline of the feed ``deadlines.ics``: ``seminars.pipeline.deadlines``
reads the editions with ``registry_editions`` on every run, so no request
and no run of the adapter is needed for them. ``display_names`` names each
edition there: its ``short_title``, else ``<series> <year of start>`` (the
series alone when it holds a year already, ``Meeting.display_name``), and
the title of the edition when another edition of the registry without a
``short_title`` would get that name as well, so that two editions of one
series and year (the workshops of one institute) are told apart.

``curation_problems`` says what a curator has to look at on a given day: an
edition that starts within ``RECHECK_WITHIN_DAYS`` and was verified more
than ``RECHECK_AFTER_DAYS`` before, a check dated in the future, and a
registry without a ``bay-area`` edition ahead. An adapter cannot add a
warning to the run report, so a test of the configuration applies it to
the committed registry on the day the tests run.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Any, Final, Literal, Self, get_args
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, BeforeValidator, StringConstraints, model_validator

from seminars.http import HttpClient
from seminars.models import (
    EventDraft,
    Model,
    RawPayload,
    SourceConfig,
    TimeWindow,
    check_http_url,
    check_timezone,
    url_has_credential,
)
from seminars.models.base import FieldList
from seminars.sources.base import register_adapter
from seminars.sources.generic.common import load_params

ADAPTER: Final = "registry"
"""The name of the adapter, which a registry source names under ``adapter``."""

MAX_MEETING_DAYS: Final = 31
"""The longest an edition may run: its last day is at most this many days after its first."""

MAX_ID_LENGTH: Final = 63
"""The longest id that still gives a readable uid (``seminars.pipeline.ids.uid_for_key``)."""

MAX_SHORT_TITLE: Final = 40
"""The longest ``short_title``: the name of an edition at the head of a summary line."""

RECHECK_WITHIN_DAYS: Final = 120
"""An edition that starts within this many days needs a recent check (``curation_problems``)."""

RECHECK_AFTER_DAYS: Final = 90
"""A check older than this many days is stale for such an edition."""

_YEAR: Final = re.compile(r"(?<![0-9])[0-9]{4}(?![0-9])")

ZONE_NAMES: Final = ("AoE", "PT", "MT", "ET", "UTC")
"""Zones a milestone may name besides an IANA time zone, as organizers write them."""

MilestoneKind = Literal[
    "abstract",
    "paper",
    "poster",
    "short_talk",
    "registration_open",
    "early_registration",
    "registration",
    "scholarship",
    "travel_award",
    "funding",
    "application",
    "notification",
    "workshop_paper",
    "other",
]
Region = Literal["bay-area", "world"]
MeetingType = Literal["conference", "workshop"]
MeetingStatus = Literal["confirmed", "tentative"]
QualifierName = Literal["exact", "by"]

MILESTONE_KINDS: Final[tuple[str, ...]] = get_args(MilestoneKind)
"""What a milestone is: a deadline of that kind, an opening or a notification."""


def _ascii_line(value: str) -> str:
    if not value.isascii() or not value.isprintable():
        raise ValueError("must be printable ASCII on one line")
    if value != value.strip():
        raise ValueError("must not start or end with white space")
    return value


def _https_url(value: str) -> str:
    check_http_url(value)
    if not value.isascii() or not value.startswith("https://"):
        raise ValueError(f"must be an https address in ASCII: {value!r}")
    if url_has_credential(value):
        raise ValueError("must not carry a credential")
    return value


def _zone(value: str) -> str:
    return value if value in ZONE_NAMES else check_timezone(value)


YAML_BOOLEANS: Final = "YAML reads an unquoted on, off, yes or no as true or false"


def _no_boolean_keys(data: object) -> object:
    if isinstance(data, Mapping) and any(isinstance(key, bool) for key in data):
        raise ValueError(f"a key is true or false ({YAML_BOOLEANS}); the day is 'date'")
    return data


def _not_boolean(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError(f"write exact or by ({YAML_BOOLEANS})")
    return value


def _quoted_time(value: object) -> object:
    if isinstance(value, int) and not isinstance(value, bool):
        raise ValueError("write the time in quotes ('23:59'): YAML reads 23:59 as a number")
    return value


def _some_fields(value: list[str]) -> list[str]:
    if not value:
        raise ValueError("needs at least one field")
    return value


Title = Annotated[str, StringConstraints(min_length=1, max_length=300), AfterValidator(_ascii_line)]
Name = Annotated[str, StringConstraints(min_length=1, max_length=200), AfterValidator(_ascii_line)]
ShortTitle = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_SHORT_TITLE),
    AfterValidator(_ascii_line),
]
Label = Annotated[str, StringConstraints(min_length=1, max_length=120), AfterValidator(_ascii_line)]
Note = Annotated[str, StringConstraints(min_length=1, max_length=500), AfterValidator(_ascii_line)]
HttpsUrl = Annotated[str, StringConstraints(max_length=500), AfterValidator(_https_url)]
MeetingId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]{4}$", max_length=MAX_ID_LENGTH),
]
ClockTime = Annotated[
    str,
    BeforeValidator(_quoted_time),
    StringConstraints(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"),
]
ZoneName = Annotated[str, AfterValidator(_zone)]
Qualifier = Annotated[QualifierName, BeforeValidator(_not_boolean)]
Fields = Annotated[FieldList, AfterValidator(_some_fields)]


class Verified(Model):
    """When and where the facts of an edition were last checked on the organizer's page."""

    date: dt.date
    url: HttpsUrl

    @model_validator(mode="before")
    @classmethod
    def _check_keys(cls, data: Any) -> Any:
        return _no_boolean_keys(data)


class Milestone(Model):
    """One date of an edition: a deadline, an opening or a notification."""

    kind: MilestoneKind
    date: dt.date
    time: ClockTime | None = None
    zone: ZoneName | None = None
    qualifier: Qualifier = "exact"
    label: Label

    @property
    def line(self) -> str:
        """The line of the abstract: "Poster deadline: 2027-03-30 23:59 PT", "2027-01-22 AoE"."""
        when = " ".join(part for part in (self.date.isoformat(), self.time, self.zone) if part)
        return f"{self.label}: {'by ' if self.qualifier == 'by' else ''}{when}"


class Meeting(Model):
    """One edition of a meeting in the registry (see the module docstring)."""

    id: MeetingId
    title: Title
    short_title: ShortTitle | None = None
    series: Name
    host_org: Name
    start: dt.date
    end: dt.date
    region: Region
    location: Title
    url: HttpsUrl
    fields: Fields
    event_type: MeetingType
    status: MeetingStatus
    verified: Verified
    milestones: list[Milestone] = []
    notes: list[Note] = []

    @model_validator(mode="after")
    def _check_dates(self) -> Self:
        if int(self.id[-4:]) != self.start.year:
            raise ValueError(
                f"the id ends in {self.id[-4:]}, the edition starts in {self.start.year}"
            )
        if self.end < self.start:
            raise ValueError("end is before start")
        if self.end - self.start > dt.timedelta(days=MAX_MEETING_DAYS):
            raise ValueError(f"end is more than {MAX_MEETING_DAYS} days after start")
        late = [milestone.label for milestone in self.milestones if milestone.date > self.end]
        if late:
            raise ValueError(f"milestone(s) after the end of the edition: {', '.join(late)}")
        return self

    @property
    def display_name(self) -> str:
        """The name of the edition alone, before ``display_names`` compares it with others.

        ``short_title``, else ``<series> <year of start>``, or the series alone
        when it holds a year of four digits already ("ISMB 2027").
        """
        if self.short_title is not None:
            return self.short_title
        if _YEAR.search(self.series):
            return self.series
        return f"{self.series} {self.start.year}"

    def abstract(self, since: dt.date) -> str | None:
        """The lines of the milestones on or after ``since``, by date; ``None`` without any."""
        ahead = sorted(
            (milestone for milestone in self.milestones if milestone.date >= since),
            key=lambda milestone: (milestone.date, milestone.time or "", milestone.label),
        )
        return "\n".join(milestone.line for milestone in ahead) or None


class RegistryParams(Model):
    """``params`` of a ``registry`` source; see the module docstring."""

    allow_empty: bool = False
    meetings: list[Meeting] = []

    @model_validator(mode="after")
    def _check_ids(self) -> Self:
        counts = Counter(meeting.id for meeting in self.meetings)
        repeated = sorted(name for name, count in counts.items() if count > 1)
        if repeated:
            raise ValueError(f"meeting id(s) used more than once: {', '.join(repeated)}")
        return self


def registry_editions(source: SourceConfig) -> list[Meeting]:
    """Every edition of the registry ``source``, ``bay-area`` and ``world``, in order.

    Raises ``AdapterConfigError`` (a ``ValueError``) when the ``params`` of the
    source do not fit, as ``fetch`` and ``parse`` do.
    """
    return list(load_params(RegistryParams, source).meetings)


def display_names(meetings: Sequence[Meeting]) -> dict[str, str]:
    """The name of each of ``meetings`` in the feed of the deadlines, by id.

    It is ``Meeting.display_name``, except for an edition without a
    ``short_title`` whose name another such edition shares: that edition is
    named by its title. Two editions can still share a name, a ``short_title``
    twice or one title twice; ``tests/test_custom_registry_deadlines.py``
    fails when two editions with milestones of the committed registry do.
    """
    shared = Counter(m.display_name for m in meetings if m.short_title is None)
    return {
        meeting.id: (
            meeting.title
            if meeting.short_title is None and shared[meeting.display_name] > 1
            else meeting.display_name
        )
        for meeting in meetings
    }


def first_day(window: TimeWindow, source: SourceConfig) -> dt.date:
    """The local day on which ``window`` starts, in the time zone of ``source``."""
    return window.start.astimezone(ZoneInfo(source.timezone)).date()


def meeting_draft(meeting: Meeting, source: SourceConfig, *, since: dt.date) -> EventDraft:
    """The all-day draft of ``meeting``; its abstract lists the milestones from ``since`` on."""
    return EventDraft(
        source_id=source.id,
        extraction="api",
        native_id=meeting.id,
        url=meeting.url,
        title=meeting.title,
        start=meeting.start,
        end=meeting.end,
        all_day=True,
        series=meeting.series,
        host_org=meeting.host_org,
        location=meeting.location,
        abstract=meeting.abstract(since),
        field_hints=meeting.fields,
        event_type_hint=meeting.event_type,
        status_hint=meeting.status,
    )


def listed(meetings: Iterable[Meeting], since: dt.date) -> list[Meeting]:
    """The ``bay-area`` editions of ``meetings`` that end on or after ``since``, in order."""
    return [
        meeting for meeting in meetings if meeting.region == "bay-area" and meeting.end >= since
    ]


def curation_problems(meetings: Sequence[Meeting], today: dt.date) -> list[str]:
    """What a curator has to look at on ``today`` (see the module docstring)."""
    problems: list[str] = []
    for meeting in meetings:
        checked = meeting.verified.date
        if checked > today:
            problems.append(f"{meeting.id}: verified on {checked}, after {today}")
            continue
        soon = meeting.start <= today + dt.timedelta(days=RECHECK_WITHIN_DAYS)
        stale = today - checked > dt.timedelta(days=RECHECK_AFTER_DAYS)
        if soon and meeting.end >= today and stale:
            problems.append(
                f"{meeting.id}: starts on {meeting.start} and was last verified on {checked}; "
                "check the organizer's page again"
            )
    if not listed(meetings, today):
        problems.append("no bay-area meeting ends on or after today; curate the registry")
    return problems


@register_adapter(ADAPTER)
class RegistryAdapter:
    """Meetings kept by hand in the source entry; see the module docstring."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        load_params(RegistryParams, source)
        return RawPayload(source_id=source.id, fetched_at=window.start, window=window)

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        params = load_params(RegistryParams, source)
        since = first_day(payload.window, source)
        return [
            meeting_draft(meeting, source, since=since)
            for meeting in listed(params.meetings, since)
        ]
