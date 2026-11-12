"""De-duplication: several listings of one talk become one event.

Many talks appear twice, on the campus calendar and on the page of the
department that hosts them. Two candidates are the same talk when

* they have the same uid or share a source key (the same listing seen
  twice, or keys that an earlier run bound to one uid), or
* their starts are at most ``dedup.max_start_difference_minutes`` apart and
  one of these holds: the titles are similar
  (``rapidfuzz.fuzz.token_set_ratio`` of at least ``dedup.title_similarity``),
  they share a speaker, or they point at the same detail page.

Three guards keep unrelated talks apart. "TBA" is never similar to "TBA",
and a title of a single word ("Seminar") is not compared at all, because
it would match every title that contains it. A page that a source is
fetched from is not a detail page: two talks without a page of their own
both point there.

Everything that is connected by such pairs forms one group, and each group
becomes one event:

* the uid is the one that was seen first (``first_seen``, then the uid
  itself), so a calendar client keeps the entry it already has;
* a title that is not "TBA" wins, the longest abstract wins, speakers are
  united, and every source key and source address is kept;
* the remaining values come from the listing of the host rather than from
  an aggregator (``SourceConfig.aggregator``): the address of the detail
  page, the times, the place, the host;
* a cancellation that any listing reports is kept.

The result does not depend on the order of the input, and running it over
its own result changes nothing: merging repeats until no two events are
the same talk, and every choice between members is made by value, never by
position. tests/test_pipeline_dedup.py checks both with generated input.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final
from urllib.parse import urlsplit

from rapidfuzz import fuzz

from seminars.config import DedupSettings
from seminars.models import (
    Candidate,
    Event,
    EventHints,
    EventStatus,
    canonical_fields,
    event_to_line,
    split_source_key,
)
from seminars.pipeline.ids import canonical_url
from seminars.pipeline.normalize import merge_speakers
from seminars.pipeline.text import canonical_name, fold

MIN_TITLE_WORDS: Final = 2
"""Titles with fewer words are not compared: "Seminar" is part of too many titles."""


@dataclass(frozen=True)
class DedupResult:
    """Outcome of de-duplication.

    ``candidates`` are sorted by uid and no two of them are the same talk.
    ``absorbed`` maps the uid of every input candidate that no longer
    exists under its own uid to the uid of the event it became part of.
    """

    candidates: tuple[Candidate, ...] = ()
    absorbed: dict[str, str] = field(default_factory=dict)

    @property
    def aliases(self) -> dict[str, str]:
        """Every source key of the result with the uid it belongs to (for the uid map)."""
        return {
            key: candidate.event.uid
            for candidate in self.candidates
            for key in candidate.event.source_ids
        }


@dataclass(frozen=True)
class _Profile:
    """What two candidates are compared by."""

    start: datetime
    title: str | None  # folded; None when the title is not compared
    speakers: frozenset[str]
    page: str | None  # canonical address of the detail page, if it is one


def _detail_page(event: Event, listing_urls: Collection[str]) -> str | None:
    address = canonical_url(str(event.url))
    if address in listing_urls or not urlsplit(address).path.strip("/"):
        return None
    return address


def _profile(candidate: Candidate, listing_urls: Collection[str]) -> _Profile:
    event = candidate.event
    title = fold(event.title)
    comparable = not event.is_tba and len(title.split()) >= MIN_TITLE_WORDS
    names = (canonical_name(speaker.name) for speaker in event.speakers)
    return _Profile(
        start=event.start,
        title=title if comparable else None,
        speakers=frozenset(name for name in names if name),
        page=_detail_page(event, listing_urls),
    )


def _similar(first: _Profile, second: _Profile, threshold: int) -> bool:
    if first.page is not None and first.page == second.page:
        return True
    if first.speakers & second.speakers:
        return True
    if first.title is None or second.title is None:
        return False
    return fuzz.token_set_ratio(first.title, second.title) >= threshold


class _Groups:
    """Union-find over the positions of a list."""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, item: int) -> int:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, first: int, second: int) -> None:
        self._parent[self.find(first)] = self.find(second)

    def members(self) -> list[list[int]]:
        groups: dict[int, list[int]] = defaultdict(list)
        for item in range(len(self._parent)):
            groups[self.find(item)].append(item)
        return list(groups.values())


def _group(
    candidates: Sequence[Candidate], settings: DedupSettings, listing_urls: Collection[str]
) -> list[list[Candidate]]:
    """Split ``candidates`` into groups that are one talk each."""
    groups = _Groups(len(candidates))
    seen: dict[str, int] = {}
    for position, candidate in enumerate(candidates):
        identities = [f"uid {candidate.event.uid}", *candidate.event.source_ids]
        for identity in identities:
            groups.union(position, seen.setdefault(identity, position))
    profiles = [_profile(candidate, listing_urls) for candidate in candidates]
    by_start = sorted(range(len(candidates)), key=lambda position: profiles[position].start)
    limit = timedelta(minutes=settings.max_start_difference_minutes)
    for index, first in enumerate(by_start):
        for second in by_start[index + 1 :]:
            if profiles[second].start - profiles[first].start > limit:
                break
            if _similar(profiles[first], profiles[second], settings.title_similarity):
                groups.union(first, second)
    return [[candidates[position] for position in group] for group in groups.members()]


def _status(events: Sequence[Event]) -> EventStatus:
    statuses = {event.status for event in events}
    if "cancelled" in statuses:
        return "cancelled"
    return "confirmed" if "confirmed" in statuses else "tentative"


def _first[T](values: Sequence[T | None]) -> T | None:
    return next((value for value in values if value is not None), None)


def _merge(group: Sequence[Candidate], aggregators: Collection[str]) -> Candidate:
    """One candidate for a group; a group of one comes back in canonical form."""

    def rank(candidate: Candidate) -> tuple[bool, str, str, str]:
        sources = candidate.hints.source_ids
        aggregated = bool(sources) and all(source in aggregators for source in sources)
        event = candidate.event
        return aggregated, event.uid, event_to_line(event), candidate.hints.model_dump_json()

    members = sorted(group, key=rank)
    events = [member.event for member in members]
    lead = events[0]
    abstracts = [event.abstract for event in events if event.abstract]
    same_time = [e for e in events if e.start == lead.start and e.all_day == lead.all_day]
    hints = members[0].hints
    for member in members[1:]:
        hints = hints.merge(member.hints)
    merged = lead.evolve(
        uid=min(events, key=lambda event: (event.first_seen, event.uid)).uid,
        title=next((event.title for event in events if not event.is_tba), lead.title),
        speakers=merge_speakers([speaker for event in events for speaker in event.speakers]),
        series=_first([event.series for event in events]),
        end=_first([event.end for event in same_time]),
        location=_first([event.location for event in events]),
        is_online=any(event.is_online for event in events),
        stream_url=_first([event.stream_url for event in events]),
        abstract=min(abstracts, key=lambda text: (-len(text), text), default=None),
        source_urls=[url for event in events for url in event.source_urls],
        source_ids=[key for event in events for key in event.source_ids],
        fields=canonical_fields(name for event in events for name in event.fields),
        picks_score=max(event.picks_score for event in events),
        status=_status(events),
        first_seen=min(event.first_seen for event in events),
        last_changed=max(event.last_changed for event in events),
        missing_runs=min(event.missing_runs for event in events),
        sequence=max(event.sequence for event in events),
    )
    return Candidate(event=merged, hints=EventHints.model_validate(hints))


def _sources_of(event: Event) -> frozenset[str]:
    """The ids of the sources whose keys an event holds."""
    return frozenset(split_source_key(key)[0] for key in event.source_ids)


def _adopt_similar(
    candidates: Sequence[Candidate],
    positions: Sequence[int],
    orphans: Sequence[Event],
    settings: DedupSettings,
    listing_urls: Collection[str],
) -> dict[int, str]:
    """Candidate positions with the uid of an orphan that is the same talk by dedup rules."""
    profiles = [(event, _profile(Candidate(event=event), listing_urls)) for event in orphans]
    limit = timedelta(minutes=settings.max_start_difference_minutes)
    adopted: dict[int, str] = {}
    taken: set[str] = set()
    for position in positions:
        profile = _profile(candidates[position], listing_urls)
        for event, other in profiles:
            close = abs(other.start - profile.start) <= limit
            if (
                event.uid not in taken
                and close
                and _similar(other, profile, settings.title_similarity)
            ):
                adopted[position] = event.uid
                taken.add(event.uid)
                break
    return adopted


def _placeholders(orphans: Iterable[Event]) -> dict[tuple[str, datetime], Event]:
    """Orphans that are TBA and without a speaker, by source and start.

    A source and start that more than one of them shares is left out: it
    would be a guess which of them a talk is.
    """
    found: dict[tuple[str, datetime], list[Event]] = defaultdict(list)
    for event in orphans:
        if event.is_tba and not event.speakers:
            for source_id in _sources_of(event):
                found[(source_id, event.start)].append(event)
    return {slot: events[0] for slot, events in found.items() if len(events) == 1}


def _adopt_placeholders(
    candidates: Sequence[Candidate], positions: Sequence[int], orphans: Sequence[Event]
) -> dict[int, str]:
    """Candidate positions with the uid of a placeholder of their source and start."""
    slots = _placeholders(orphans)
    adopted: dict[int, str] = {}
    taken: set[str] = set()
    for position in positions:
        event = candidates[position].event
        for source_id in sorted(_sources_of(event)):
            placeholder = slots.get((source_id, event.start))
            if placeholder is not None and placeholder.uid not in taken:
                adopted[position] = placeholder.uid
                taken.add(placeholder.uid)
                break
    return adopted


def adopt_stored_uids(
    candidates: Sequence[Candidate],
    stored: Sequence[Event],
    *,
    settings: DedupSettings,
    listing_urls: Collection[str] = (),
) -> list[Candidate]:
    """Let a new candidate take over the uid of a stored event that is the same talk.

    A source sometimes changes the key of an event: a page gets a new
    address, a talk gains a speaker or its title. The event then arrives
    under a uid that was never seen, while its stored self is no longer
    delivered, and a subscriber would see the talk twice until the old
    entry expires.

    A candidate whose uid is not stored therefore takes the uid of a stored
    event that no candidate of this run claims (an orphan), in two steps:

    1. an orphan that is the same talk by the rules of de-duplication
       (start within the limit and a similar title, a shared speaker or the
       same detail page);
    2. otherwise a placeholder of its source: an orphan of the same source
       that starts at exactly the same moment, is still "TBA" and names no
       speaker, and is the only such orphan of that source and start. Such
       a talk has nothing the rules of step 1 could compare, and its key
       (the time of day) changes as soon as the title or a speaker is
       announced.

    In each step the orphan seen first is taken, and each orphan is taken
    once. The candidates come back in their order; the choice does not
    depend on it.
    """
    claimed = {candidate.event.uid for candidate in candidates}
    known = {event.uid for event in stored}
    orphans = sorted(
        (event for event in stored if event.uid not in claimed),
        key=lambda event: (event.first_seen, event.uid),
    )
    order = sorted(
        range(len(candidates)), key=lambda position: event_to_line(candidates[position].event)
    )
    unknown = [position for position in order if candidates[position].event.uid not in known]
    adopted = _adopt_similar(candidates, unknown, orphans, settings, listing_urls)
    taken = set(adopted.values())
    adopted |= _adopt_placeholders(
        candidates,
        [position for position in unknown if position not in adopted],
        [event for event in orphans if event.uid not in taken],
    )
    return [
        candidate.evolve(event=candidate.event.evolve(uid=adopted[position]))
        if position in adopted
        else candidate
        for position, candidate in enumerate(candidates)
    ]


def dedupe(
    candidates: Sequence[Candidate],
    *,
    settings: DedupSettings,
    aggregators: Collection[str] = (),
    listing_urls: Collection[str] = (),
) -> DedupResult:
    """Merge the candidates that are the same talk (see the module docstring).

    ``aggregators`` are the ids of the sources with ``aggregator: true``.
    ``listing_urls`` are addresses in the form of
    ``seminars.pipeline.ids.canonical_url`` that are never the detail page
    of one talk: the pages the sources are fetched from.
    """
    current = list(candidates)
    final = {candidate.event.uid: candidate.event.uid for candidate in current}
    while True:
        groups = _group(current, settings, listing_urls)
        merged = [_merge(group, aggregators) for group in groups]
        step = {
            member.event.uid: result.event.uid
            for group, result in zip(groups, merged, strict=True)
            for member in group
        }
        final = {uid: step[now] for uid, now in final.items()}
        if len(merged) == len(current):
            break
        current = merged
    ordered = sorted(merged, key=lambda candidate: candidate.event.uid)
    absorbed = {uid: now for uid, now in sorted(final.items()) if uid != now}
    return DedupResult(candidates=tuple(ordered), absorbed=absorbed)
