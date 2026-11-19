"""A deadline of a conference or workshop, as data/deadlines.json stores it.

The milestones of the registry of meetings kept by hand
(``seminars.sources.custom.registry``) are the deadlines, openings and
notifications that a reader acts on. Every run turns each milestone of an
enabled registry source into one ``Deadline`` (``seminars.pipeline.deadlines``),
keeps them in data/deadlines.json and publishes them in the feed
``deadlines.ics``, which is not one of FEED_IDS. A deadline is no ``Event``:
it is one day, it has neither speakers nor a source key, and it carries the
time and the zone that the organizer's page states as they are written.

``uid`` is ``dl.<edition id>.<kind>`` for the first milestone of a kind in
an edition, counted by date, and ``dl.<edition id>.<kind>-<n>`` for the n-th
one (n >= 2); ``deadline_uid`` builds it. In the feed the UID is
``<uid>@<uid_domain>``, the form of the UID of an event.

The file holds one deadline per line, sorted by date and uid, with the keys
in the field order of the model, and it is ASCII only (every text of a
deadline is printable ASCII on one line, as the registry requires)::

    {"schema_version": 1, "deadlines": [
    {"uid": "dl.iclr-2027.workshop_paper", ...},
    {"uid": "dl.iclr-2027.notification", ...}
    ]}

Loading such a file and dumping it again gives the same bytes.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from collections.abc import Iterable
from typing import Annotated, Any, Final, Literal, Self

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from seminars.models.base import FieldList, HttpUrlStr, Model, Timestamp, Uid

DEADLINES_SCHEMA_VERSION: Final = 1

DEADLINES_FEED: Final = "deadlines"
"""Name of the feed of the deadlines, published as ``feeds/deadlines.ics``; not one of FEED_IDS."""

DEADLINE_UID_PREFIX: Final = "dl"

DEADLINE_SHOWN: Final = frozenset(
    {
        *("edition_title", "edition_short", "edition_start", "edition_end", "location", "url"),
        *("label", "date", "time", "zone", "qualifier", "verified_on", "verified_url"),
    }
)
"""The facts of a deadline that the feed and the digest show (``Deadline.shown``).

A change of one of them is a change of the deadline, which a subscribed calendar
has to see. ``uid``, ``edition_id`` and ``kind`` make the uid, and ``series``,
``region`` and ``fields`` are shown nowhere."""

DeadlineRegion = Literal["bay-area", "world"]
DeadlineQualifier = Literal["exact", "by"]


def _ascii_line(value: str) -> str:
    if not value.isascii() or not value.isprintable() or value != value.strip():
        raise ValueError("must be printable ASCII on one line, without white space at either end")
    return value


def _ascii_url(value: str) -> str:
    if not value.isascii():
        raise ValueError("must be an address in ASCII")
    return value


Line = Annotated[str, StringConstraints(min_length=1, max_length=300), AfterValidator(_ascii_line)]
EditionId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=63)]
Kind = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_]*$", max_length=40)]
ClockTime = Annotated[str, StringConstraints(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
ZoneName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_+/-]*$", max_length=64)]
AsciiHttpUrl = Annotated[HttpUrlStr, AfterValidator(_ascii_url)]


def deadline_uid(edition_id: str, kind: str, position: int = 1) -> str:
    """The uid of the ``position``-th milestone of ``kind`` of an edition, counted by date."""
    if position < 1:
        raise ValueError(f"the position of a milestone starts at 1, not {position}")
    suffix = "" if position == 1 else f"-{position}"
    return f"{DEADLINE_UID_PREFIX}.{edition_id}.{kind}{suffix}"


class Deadline(Model):
    """One milestone of an edition: a deadline, an opening or a notification.

    Every value but ``first_seen``, ``last_changed`` and ``sequence`` is a
    fact of the registry entry: ``edition_*``, ``series``, ``region``,
    ``location``, ``url`` (the organizer's page of the edition) and ``fields``
    describe the edition, ``kind``, ``label``, ``date``, ``time``, ``zone``
    and ``qualifier`` the milestone, and ``verified_on`` is the day of the
    last check of its facts and ``verified_url`` the organizer's page that
    states the date (the page of the milestone, or else the page of the last
    check). ``edition_short`` is the name of the edition in a feed
    (``display_names`` of the registry). ``time`` and ``zone`` are as the page
    writes them; a zone without a time is the zone of the day. ``first_seen``
    and ``last_changed`` are moments of runs, and ``sequence`` goes up with
    every change of a fact that the feed shows (``DEADLINE_SHOWN``,
    ``seminars.pipeline.deadlines``).
    """

    uid: Uid
    edition_id: EditionId
    edition_title: Line
    edition_short: Line
    series: Line
    region: DeadlineRegion
    edition_start: dt.date
    edition_end: dt.date
    location: Line
    url: AsciiHttpUrl
    fields: FieldList
    kind: Kind
    label: Line
    date: dt.date
    time: ClockTime | None = None
    zone: ZoneName | None = None
    qualifier: DeadlineQualifier = "exact"
    verified_on: dt.date
    verified_url: AsciiHttpUrl
    first_seen: Timestamp
    last_changed: Timestamp
    sequence: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.edition_end < self.edition_start:
            raise ValueError("edition_end is before edition_start")
        if self.date > self.edition_end:
            raise ValueError("the deadline is after the end of the edition")
        first = deadline_uid(self.edition_id, self.kind)
        rest = self.uid.removeprefix(f"{first}-")
        later = rest != self.uid and rest.isdigit() and not rest.startswith("0") and int(rest) >= 2
        if self.uid != first and not later:
            raise ValueError("uid must be dl.<edition id>.<kind> or dl.<edition id>.<kind>-<n>")
        return self

    def shown(self) -> dict[str, Any]:
        """The facts of ``DEADLINE_SHOWN``, keyed by field: what a change is measured on."""
        fields = type(self).model_fields
        return {name: getattr(self, name) for name in fields if name in DEADLINE_SHOWN}


def ordered_deadlines(deadlines: Iterable[Deadline]) -> list[Deadline]:
    """``deadlines`` by date and uid, the order of the file and of the feed.

    A uid that appears twice is an error.
    """
    listed = list(deadlines)
    repeated = sorted(uid for uid, count in Counter(d.uid for d in listed).items() if count > 1)
    if repeated:
        raise ValueError(f"duplicate deadline uid: {repeated[0]!r}")
    return sorted(listed, key=lambda deadline: (deadline.date, deadline.uid))


def deadline_to_line(deadline: Deadline) -> str:
    """One deadline as a single line of ASCII JSON (no trailing newline)."""
    return json.dumps(deadline.model_dump(mode="json"), ensure_ascii=True, allow_nan=False)


def dump_deadlines(deadlines: Iterable[Deadline]) -> str:
    """Serialize deadlines as the text of data/deadlines.json (see the module docstring)."""
    lines = [deadline_to_line(deadline) for deadline in ordered_deadlines(deadlines)]
    body = ",\n".join(lines)
    header = f'{{"schema_version": {DEADLINES_SCHEMA_VERSION}, "deadlines": ['
    return f"{header}\n{body}\n]}}\n" if lines else f"{header}\n]}}\n"


def load_deadlines(text: str) -> list[Deadline]:
    """Parse the text of data/deadlines.json; the result is sorted by date and uid."""
    document = json.loads(text)
    if not isinstance(document, dict) or not isinstance(document.get("deadlines"), list):
        raise ValueError("deadlines file must be an object with a 'deadlines' list")
    version = document.get("schema_version")
    if version != DEADLINES_SCHEMA_VERSION:
        raise ValueError(f"unsupported deadlines schema_version: {version!r}")
    return ordered_deadlines(Deadline.model_validate(item) for item in document["deadlines"])
