"""The deadlines of the registry of meetings: read from the configuration, stored in data/.

Every run reads the milestones of every enabled source with the adapter
``registry`` (``seminars.sources.custom.registry.registry_editions``), of
``bay-area`` and ``world`` editions alike, and makes one ``Deadline`` of each
(``configured_deadlines``), named by ``display_names`` of the editions of its
source. Its ``verified_url``, the page a reader checks the date on, is the
``url`` of the milestone, or else the page of the check of the edition
(``verified.url``). Nothing is requested and no adapter runs for them, so
the edit of a curator shows in the next run, whatever sources that run
reads (``--source`` included).

The uid of a milestone is ``dl.<edition id>.<kind>``, and ``-<n>`` is added
for the n-th milestone of the same kind in the edition (n >= 2), counted by
date, then by time and label (``seminars.models.deadline_uid``). A curator
therefore adds a later round of a kind after the earlier ones and keeps an
earlier round in the entry while a later one is listed: a milestone that is
inserted before another one of its kind, or deleted before it, moves the uid
of every later one, which a calendar then shows as one deadline removed and
another one added.

``reconcile_deadlines`` compares the configured deadlines with the stored
ones by uid:

* a deadline that is not stored is new: ``first_seen`` and ``last_changed``
  are the time of the run and ``sequence`` is 0;
* a stored deadline whose facts are all the same stays as it is stored;
* a stored deadline with a fact that the feed or the digest shows
  (``DEADLINE_SHOWN``: the label, the day, the time, the zone, the
  qualifier, the name, title, days, place and page of the edition, the day
  of the check, the page that states the milestone) that changed takes the
  facts of the configuration, ``sequence`` goes up by one and
  ``last_changed`` becomes the time of the run, so a subscribed calendar
  updates it;
* a stored deadline of which only a fact that nothing shows changed
  (``series``, ``region``, ``fields``) takes the facts of the configuration
  and keeps its ``first_seen``, ``last_changed`` and ``sequence``, so the
  feed keeps its bytes;
* a stored deadline that the configuration no longer holds (its milestone,
  its edition or its source was deleted, or the source is disabled) leaves
  the file; there is no archive of deadlines. Only when the ``params`` of a
  registry source cannot be read (the run of its adapter fails as well)
  does the configuration say nothing, and every stored deadline that it
  does not hold stays as it is;
* a deadline whose date lies before ``since``, the first day of the window
  of the run (``settings.window.days_back`` days before the local date of
  the run), leaves the file, configured or not, as a talk leaves the feeds
  once the window has moved past it.

Two registry sources that give the same uid (the same edition id in both)
keep the deadline of the first source in the order of the configuration,
and the run says so in a warning.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from seminars.models import Deadline, SourceConfig, deadline_uid, ordered_deadlines
from seminars.sources.custom.registry import (
    ADAPTER,
    Meeting,
    Milestone,
    display_names,
    registry_editions,
)


@dataclass(frozen=True)
class ConfiguredDeadlines:
    """The deadlines of the configuration, stamped with the time of the run."""

    deadlines: tuple[Deadline, ...] = ()
    complete: bool = True  # every registry source could be read
    warnings: tuple[str, ...] = ()


def _by_date(milestone: Milestone) -> tuple[dt.date, str, str]:
    return milestone.date, milestone.time or "", milestone.label


def edition_deadlines(
    meeting: Meeting, *, now: datetime, name: str | None = None
) -> list[Deadline]:
    """One deadline per milestone of ``meeting``, new as of ``now``, by date.

    ``name`` is the name of the edition among the others of its registry
    (``display_names``); without it the edition is named alone
    (``Meeting.display_name``).
    """
    stamp = now.astimezone(UTC)
    deadlines: list[Deadline] = []
    positions: dict[str, int] = {}
    for milestone in sorted(meeting.milestones, key=_by_date):
        position = positions[milestone.kind] = positions.get(milestone.kind, 0) + 1
        deadlines.append(
            Deadline(
                uid=deadline_uid(meeting.id, milestone.kind, position),
                edition_id=meeting.id,
                edition_title=meeting.title,
                edition_short=meeting.display_name if name is None else name,
                series=meeting.series,
                region=meeting.region,
                edition_start=meeting.start,
                edition_end=meeting.end,
                location=meeting.location,
                url=meeting.url,
                fields=meeting.fields,
                kind=milestone.kind,
                label=milestone.label,
                date=milestone.date,
                time=milestone.time,
                zone=milestone.zone,
                qualifier=milestone.qualifier,
                verified_on=meeting.verified.date,
                verified_url=milestone.url or meeting.verified.url,
                first_seen=stamp,
                last_changed=stamp,
            )
        )
    return deadlines


def configured_deadlines(sources: Iterable[SourceConfig], *, now: datetime) -> ConfiguredDeadlines:
    """The deadlines of the enabled registry sources among ``sources`` (see the module)."""
    found: dict[str, Deadline] = {}
    complete = True
    warnings: list[str] = []
    for source in sources:
        if not source.enabled or source.adapter != ADAPTER:
            continue
        try:
            editions = registry_editions(source)
        except ValueError as error:  # AdapterConfigError: the source fails in this run too
            complete = False
            warnings.append(
                f"the milestones of source {source.id} could not be read "
                f"({type(error).__name__}); its stored deadlines stay"
            )
            continue
        repeated: list[str] = []
        names = display_names(editions)
        for meeting in editions:
            for deadline in edition_deadlines(meeting, now=now, name=names[meeting.id]):
                if deadline.uid in found:
                    repeated.append(deadline.uid)
                else:
                    found[deadline.uid] = deadline
        if repeated:
            warnings.append(
                f"source {source.id} gives {len(repeated)} deadline uid(s) that an earlier "
                f"registry source gives already (the first is {repeated[0]}); they are left out"
            )
    return ConfiguredDeadlines(
        deadlines=tuple(ordered_deadlines(found.values())),
        complete=complete,
        warnings=tuple(warnings),
    )


def reconcile_deadlines(
    stored: Sequence[Deadline],
    configured: ConfiguredDeadlines,
    *,
    now: datetime,
    since: dt.date,
) -> tuple[Deadline, ...]:
    """The deadlines to store after a run at ``now`` (see the module docstring)."""
    previous = {deadline.uid: deadline for deadline in stored}
    kept: dict[str, Deadline] = {}
    for fresh in configured.deadlines:
        old = previous.get(fresh.uid)
        if old is None:
            kept[fresh.uid] = fresh
        elif old.shown() == fresh.shown():
            same = fresh.evolve(
                first_seen=old.first_seen, last_changed=old.last_changed, sequence=old.sequence
            )
            kept[fresh.uid] = old if same == old else same
        else:
            kept[fresh.uid] = fresh.evolve(
                first_seen=old.first_seen,
                last_changed=now.astimezone(UTC),
                sequence=old.sequence + 1,
            )
    if not configured.complete:
        for old in stored:
            kept.setdefault(old.uid, old)
    return tuple(ordered_deadlines(d for d in kept.values() if d.date >= since))
