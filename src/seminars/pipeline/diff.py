"""What a run changes in data/events.json: additions, changes, absences, the archive.

The stored events and the events of the run are compared by uid:

* an event of the run that is not stored yet is added;
* a stored event that the run delivered again takes the new values. When
  anything a reader can see has changed, ``sequence`` goes up by one and
  ``last_changed`` becomes the time of the run; both feed the calendar
  files, so a subscriber's client notices the update. Internal values (the
  source keys, how the fields were found, the score behind a pick) do not
  count as a change;
* a stored event that the run did not deliver is handled by what is known
  about it:

  - it starts before the window: it moves to the archive;
  - the run itself excluded it (a filter rule, out of scope): it is
    removed at once;
  - it has started already, or it is cancelled: it stays as it is. Pages
    drop talks once they are over, and a cancelled talk is kept until
    its time has passed;
  - every source it came from was healthy in this run (it answered and
    delivered events): ``missing_runs`` goes up by one, and at
    ``events.delete_after_missing_runs`` the event is removed;
  - otherwise nothing is concluded: a source that failed, delivered
    nothing or was not part of the run says nothing about its events.

Everything counts runs, never days, so a run that was skipped changes
nothing.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from seminars.models import Event, TimeWindow

UNTRACKED_FIELDS: Final = frozenset(
    {
        "uid",
        "first_seen",
        "last_changed",
        "missing_runs",
        "sequence",
        "picks_score",
        "field_tag_method",
        "extraction",
        "source_ids",
    }
)
"""Fields whose change is not a change a reader can see."""

TRACKED_FIELDS: Final = frozenset(Event.model_fields) - UNTRACKED_FIELDS


@dataclass(frozen=True)
class Reconciled:
    """The stored events after a run, and what happened on the way."""

    events: tuple[Event, ...] = ()  # sorted by uid
    archived: tuple[Event, ...] = ()  # sorted by uid
    added: int = 0
    changed: int = 0
    missing: int = 0  # future events the run did not deliver and that are still stored
    removed: int = 0


def visible_content(event: Event) -> dict[str, Any]:
    """The values of ``event`` whose change is an update for subscribers."""
    return event.model_dump(mode="json", include=set(TRACKED_FIELDS))


def _source_of(key: str) -> str:
    return key.partition(":")[0]


def _delivered(stored: Event | None, fresh: Event, now: datetime) -> tuple[Event, bool]:
    """The event of the run with the bookkeeping of its stored self; True when it changed."""
    if stored is None:
        return fresh.evolve(first_seen=now, last_changed=now, sequence=0, missing_runs=0), False
    changed = visible_content(stored) != visible_content(fresh)
    updated = fresh.evolve(
        first_seen=stored.first_seen,
        last_changed=now if changed else stored.last_changed,
        sequence=stored.sequence + int(changed),
        missing_runs=0,
    )
    return updated, changed


def reconcile(
    previous: Sequence[Event],
    fresh: Sequence[Event],
    *,
    now: datetime,
    window: TimeWindow,
    healthy_sources: Collection[str],
    delete_after_missing_runs: int,
    excluded_uids: Collection[str] = (),
    absorbed: Mapping[str, str] | None = None,
) -> Reconciled:
    """Bring the stored events up to date with the events of a run.

    ``healthy_sources`` are the ids of the sources that delivered events in
    this run. ``excluded_uids`` are events the run decided to leave out,
    and ``absorbed`` maps uids that de-duplication merged into another
    event to that event; neither is kept under its old uid.
    """
    moment = now.astimezone(UTC)
    stored_by_uid = {event.uid: event for event in previous}
    delivered = [_delivered(stored_by_uid.get(event.uid), event, moment) for event in fresh]
    fresh_uids = {event.uid for event in fresh}
    kept = [event for event, _ in delivered]
    archived: list[Event] = []
    missing = removed = 0
    for stored in previous:
        if stored.uid in fresh_uids or stored.uid in (absorbed or {}):
            continue
        if stored.start < window.start:
            archived.append(stored)
        elif stored.uid in excluded_uids:
            removed += 1
        elif stored.start < moment or stored.status == "cancelled":
            kept.append(stored)
        elif {_source_of(key) for key in stored.source_ids} <= set(healthy_sources):
            if stored.missing_runs + 1 >= delete_after_missing_runs:
                removed += 1
            else:
                kept.append(stored.evolve(missing_runs=stored.missing_runs + 1))
                missing += 1
        else:
            kept.append(stored)
            missing += 1
    return Reconciled(
        events=tuple(sorted(kept, key=lambda event: event.uid)),
        archived=tuple(sorted(archived, key=lambda event: event.uid)),
        added=sum(event.uid not in stored_by_uid for event in fresh),
        changed=sum(changed for _, changed in delivered),
        missing=missing,
        removed=removed,
    )
