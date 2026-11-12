"""What a run changes in data/events.json: additions, changes, absences, the archive.

The stored events and the events of the run are compared by uid:

* an event of the run that is not stored yet is added;
* a stored event that the run delivered again takes the new values. When
  anything a reader can see has changed, ``sequence`` goes up by one and
  ``last_changed`` becomes the time of the run; both feed the calendar
  files, so a subscriber's client notices the update. Internal values (the
  source keys, how the fields were found, the score behind a pick) do not
  count as a change;
* unless the stored event also came from a source that says nothing in
  this run (below) and whose listing the run did not deliver for it. The
  event of the run is then only part of the talk: one source of a merged
  event failed, and what the others list (a "TBA" title, no place, other
  fields) is not news. The stored event stands in for the listing that is
  missing: it keeps everything a reader sees and its keys, takes up the
  new keys of the run and is no longer missing. Only a cancellation that
  a listing of the run reports goes through, as a change. When the source
  is back, its listing and the others make the event again, and nothing
  has changed;
* a stored event that the run did not deliver is handled by what is known
  about it:

  - it starts before the window: it moves to the archive;
  - the run itself excluded it (a filter rule, out of scope): it is
    removed at once;
  - every source it came from is disabled or no longer configured: it is
    removed at once, like the events of a deleted source;
  - it has started already, or it is cancelled: it stays as it is. Pages
    drop talks once they are over, and a cancelled talk is kept until
    its time has passed;
  - every source it came from was healthy in this run: ``missing_runs``
    goes up by one, and at ``events.delete_after_missing_runs`` the event
    is removed;
  - otherwise nothing is concluded.

A source says nothing about its events in a run when it is configured and
enabled but not healthy: it failed, delivered no event, was not part of
the run (``--source``), or was not read in full (``healthy_sources`` holds
only the ones that were). A source that is disabled or removed from the
configuration is no source any more: its keys are dropped from the stored
events, and an event is judged by the sources it has left.

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


@dataclass(frozen=True)
class _Sources:
    """What the sources of a run say: who was healthy, who is configured at all."""

    healthy: frozenset[str]
    enabled: frozenset[str] | None  # None: every source is

    def retired(self, key: str) -> bool:
        """Whether ``key`` belongs to a source that is disabled or removed."""
        return self.enabled is not None and _source_of(key) not in self.enabled

    def silent(self, source_id: str) -> bool:
        """Whether a configured source says nothing about its events in this run."""
        return source_id not in self.healthy and (self.enabled is None or source_id in self.enabled)

    def live_keys(self, event: Event) -> list[str]:
        return [key for key in event.source_ids if not self.retired(key)]


def _bookkept(stored: Event, fresh: Event, now: datetime) -> tuple[Event, bool]:
    """``fresh`` with the bookkeeping of its stored self; True when it changed."""
    changed = visible_content(stored) != visible_content(fresh)
    updated = fresh.evolve(
        first_seen=stored.first_seen,
        last_changed=now if changed else stored.last_changed,
        sequence=stored.sequence + int(changed),
        missing_runs=0,
    )
    return updated, changed


def _stand_in(stored: Event, fresh: Event, silent: set[str], now: datetime) -> tuple[Event, bool]:
    """The stored event in place of a partial listing (see the module docstring)."""
    kept = [key for key in stored.source_ids if _source_of(key) in silent]
    keys = sorted({*fresh.source_ids, *kept})
    event = stored.evolve(source_ids=keys, missing_runs=0)
    if fresh.status == "cancelled" and stored.status != "cancelled":
        return event.evolve(
            status="cancelled", last_changed=now, sequence=stored.sequence + 1
        ), True
    return event, False


def _delivered(
    stored: Event | None, fresh: Event, now: datetime, sources: _Sources
) -> tuple[Event, bool]:
    """The event of the run as it is stored after the run; True when it changed."""
    if stored is None:
        return fresh.evolve(first_seen=now, last_changed=now, sequence=0, missing_runs=0), False
    present = {_source_of(key) for key in fresh.source_ids}
    silent = {
        _source_of(key)
        for key in stored.source_ids
        if _source_of(key) not in present and sources.silent(_source_of(key))
    }
    if silent:
        return _stand_in(stored, fresh, silent, now)
    return _bookkept(stored, fresh, now)


def reconcile(
    previous: Sequence[Event],
    fresh: Sequence[Event],
    *,
    now: datetime,
    window: TimeWindow,
    healthy_sources: Collection[str],
    delete_after_missing_runs: int,
    enabled_sources: Collection[str] | None = None,
    excluded_uids: Collection[str] = (),
    absorbed: Mapping[str, str] | None = None,
) -> Reconciled:
    """Bring the stored events up to date with the events of a run.

    ``healthy_sources`` are the ids of the sources that were read in full
    in this run and delivered events. ``enabled_sources`` are the ids of
    every enabled source of the configuration, also those that were not
    part of the run; ``None`` takes every source for enabled.
    ``excluded_uids`` are events the run decided to leave out, and
    ``absorbed`` maps uids that de-duplication merged into another event to
    that event; neither is kept under its old uid.
    """
    moment = now.astimezone(UTC)
    sources = _Sources(
        frozenset(healthy_sources), None if enabled_sources is None else frozenset(enabled_sources)
    )
    stored_by_uid = {event.uid: event for event in previous}
    delivered = [
        _delivered(stored_by_uid.get(event.uid), event, moment, sources) for event in fresh
    ]
    fresh_uids = {event.uid for event in fresh}
    kept = [event for event, _ in delivered]
    archived: list[Event] = []
    missing = removed = 0
    for stored in previous:
        if stored.uid in fresh_uids or stored.uid in (absorbed or {}):
            continue
        keys = sources.live_keys(stored)
        current = stored if keys in (stored.source_ids, []) else stored.evolve(source_ids=keys)
        if stored.start < window.start:
            archived.append(stored)
        elif stored.uid in excluded_uids or not keys:
            removed += 1
        elif stored.start < moment or stored.status == "cancelled":
            kept.append(current)
        elif {_source_of(key) for key in keys} <= sources.healthy:
            if stored.missing_runs + 1 >= delete_after_missing_runs:
                removed += 1
            else:
                kept.append(current.evolve(missing_runs=stored.missing_runs + 1))
                missing += 1
        else:
            kept.append(current)
            missing += 1
    return Reconciled(
        events=tuple(sorted(kept, key=lambda event: event.uid)),
        archived=tuple(sorted(archived, key=lambda event: event.uid)),
        added=sum(event.uid not in stored_by_uid for event in fresh),
        changed=sum(changed for _, changed in delivered),
        missing=missing,
        removed=removed,
    )
