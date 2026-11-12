"""De-duplication: several listings of one talk become one event.

Many talks appear twice, on the campus calendar and on the page of the
department that hosts them. Two candidates are the same talk when

* they have the same uid or share a source key (the same listing seen
  twice, or keys that an earlier run bound to one uid), or
* their starts are at most ``dedup.max_start_difference_minutes`` apart and
  one of these holds: the titles are similar
  (``rapidfuzz.fuzz.token_set_ratio`` of at least ``dedup.title_similarity``),
  they share a speaker, or they point at the same detail page.

Four guards keep unrelated talks apart. "TBA" is never similar to "TBA",
and a title of a single word ("Seminar") is not compared at all, because
it would match every title that contains it. A page that a source is
fetched from is not a detail page: two talks without a page of their own
both point there. Nor is a page that two talks of one source point at
about the same time (a series page), as for the keys
(``seminars.pipeline.ids.shared_urls``).
And two listings that share a source and both name
speakers are never one talk unless each names the same one speaker: a
source lists a talk once, and two colloquia at the same hour have the
same title and often the same page. This last guard holds for groups as
well: a group never takes in a listing that is distinct from one of its
members, even when a third listing (a campus calendar that names both
speakers) is similar to both.

The uid map does not get the last word on that guard: before a run
looks at the uids, ``separate_listings`` gives a listing a uid of its own
when its uid also holds a listing of its source that names other
speakers, so a merge that later evidence refutes is undone. That is the
one known gap of the guard: two talks of one source at one time, one of
which names no speaker yet, can be one event for a run; once both name
their speakers, the next run takes them apart again.

Everything that is connected by such pairs forms one group, and each group
becomes one event:

* the uid is the one that was seen first (``first_seen``, then the uid
  itself), so a calendar client keeps the entry it already has;
* the title is one that is not "TBA". Titles are compared by their words
  without those of the series, of the speakers and fillers such as
  "seminar" or "with". A title that names a speaker of the talk gives way
  to one that does not and has at least as many such words: a host that
  announces a talk by its speaker ("CSB Special Seminar & Open
  Discussion, David Garfield, PhD") gives way to one that names the talk,
  while "Jane Doe: Protein design with diffusion models" stays against
  "Bioengineering Seminar". Of the rest, a title whose words are strictly
  more than those of another wins ("Diffusion models for protein design"
  over "Diffusion models", any title over the bare name of its series), and
  otherwise the order below decides;
* the longest abstract wins, speakers are united, and every source key and
  source address is kept;
* the remaining values come from the listing of the host rather than from
  an aggregator (``SourceConfig.aggregator``): the address of the detail
  page, the times, the place, the host;
* a cancellation that any listing reports is kept.

The result does not depend on the order of the input, and running it over
its own result changes nothing: merging repeats until no two events are
the same talk, pairs are joined in an order fixed by their values, and
every choice between members is made by value, never by position.
tests/test_pipeline_dedup.py checks both with generated input.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Hashable, Iterable, Iterator, Sequence
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
    Speaker,
    canonical_fields,
    event_to_line,
    split_source_key,
)
from seminars.pipeline.ids import canonical_url, is_native_key, is_time_of_day_key, uid_for_key
from seminars.pipeline.normalize import merge_speakers
from seminars.pipeline.text import canonical_name, fold, word_set

MIN_TITLE_WORDS: Final = 2
"""Titles with fewer words are not compared: "Seminar" is part of too many titles."""

_TITLE_FILLER: Final = word_set(
    "a an and at by for from in of on the to with seminar seminars talk talks lecture "
    "colloquium series special"
)
"""Words that say nothing about a talk when titles of one talk are compared."""


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


def _sources_of(event: Event) -> frozenset[str]:
    """The ids of the sources whose keys an event holds (the part before the first colon)."""
    return frozenset(key.partition(":")[0] for key in event.source_ids)


@dataclass(frozen=True)
class _Profile:
    """What two candidates are compared by."""

    start: datetime
    title: str | None  # folded; None when the title is not compared
    speakers: frozenset[str]
    page: str | None  # canonical address of the detail page, if it is one
    sources: frozenset[str]  # ids of the sources whose keys the event holds
    native: frozenset[str]  # ids of the sources whose native id the event holds


def _detail_page(event: Event, not_pages: Collection[str]) -> str | None:
    address = canonical_url(str(event.url))
    if address in not_pages or not urlsplit(address).path.strip("/"):
        return None
    return address


def _shared_pages(talks: Iterable[tuple[Hashable, Event]], limit: timedelta) -> frozenset[str]:
    """Pages, in canonical form, that two talks of one source point at about the same time.

    ``talks`` pairs every event with the talk it belongs to; events of one
    talk never count as two. Such a page lists a series, it is not the page
    of a talk, just as ``seminars.pipeline.ids.shared_urls`` has it for keys.
    Only starts at most ``limit`` apart count, the starts a page could link:
    the instances of a recurring event on a campus calendar share its page
    a week apart, and that page still tells which instance a copy is.
    """
    starts: dict[tuple[str, str], list[tuple[datetime, Hashable]]] = defaultdict(list)
    for talk, event in talks:
        page = canonical_url(str(event.url))
        for source_id in _sources_of(event):
            starts[(source_id, page)].append((event.start, talk))
    return frozenset(page for (_, page), found in starts.items() if _crowded(found, limit))


def _crowded(starts: list[tuple[datetime, Hashable]], limit: timedelta) -> bool:
    """Whether two different talks among ``starts`` begin at most ``limit`` apart."""
    ordered = sorted(starts, key=lambda item: item[0])
    for index, (start, talk) in enumerate(ordered):
        for later, other in ordered[index + 1 :]:
            if later - start > limit:
                break
            if other != talk:
                return True
    return False


def _profile(candidate: Candidate, not_pages: Collection[str]) -> _Profile:
    """The profile of a candidate; ``not_pages`` are addresses that are no detail page."""
    event = candidate.event
    title = fold(event.title)
    comparable = not event.is_tba and len(title.split()) >= MIN_TITLE_WORDS
    names = (canonical_name(speaker.name) for speaker in event.speakers)
    return _Profile(
        start=event.start,
        title=title if comparable else None,
        speakers=frozenset(name for name in names if name),
        page=_detail_page(event, not_pages),
        sources=_sources_of(event),
        native=frozenset(key.partition(":")[0] for key in event.source_ids if is_native_key(key)),
    )


def _linked(first: _Profile, second: _Profile) -> bool:
    """The strong reasons: the same detail page or a shared speaker."""
    return (first.page is not None and first.page == second.page) or bool(
        first.speakers & second.speakers
    )


def _similar_titles(first: _Profile, second: _Profile, threshold: int) -> bool:
    if first.title is None or second.title is None:
        return False
    return fuzz.token_set_ratio(first.title, second.title) >= threshold


def _distinct(first: _Profile, second: _Profile) -> bool:
    """Whether two listings are two talks whatever else they share.

    One source lists one talk once. Two listings that share a source and
    both name speakers are two talks (two colloquia at the same hour, each
    with its own speaker), even with the same title and the same page,
    unless each names the same one speaker.

    The rule has to hold for merged events too, whose speakers are those of
    all their listings: a campus calendar that names both colloquium
    speakers in one listing joins one of the two talks, and the merged
    talk must still be distinct from the other one. So two listings with
    the same two speakers count as distinct as well; with that, merging
    can only ever make two events more distinct, never less, and a merge
    that was refused once is refused in every later round and every later
    run of de-duplication.
    """
    if not (first.sources & second.sources and first.speakers and second.speakers):
        return False
    return not (first.speakers == second.speakers and len(first.speakers) == 1)


class _Groups:
    """Union-find over the positions of a list, with the members of every group."""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))
        self._members = {item: [item] for item in range(size)}

    def find(self, item: int) -> int:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def group_of(self, item: int) -> list[int]:
        return self._members[self.find(item)]

    def union(self, first: int, second: int) -> None:
        source, target = self.find(first), self.find(second)
        if source != target:
            self._parent[source] = target
            self._members[target] += self._members.pop(source)

    def members(self) -> list[list[int]]:
        return [sorted(group) for group in self._members.values()]


def _canonical_order(candidates: Sequence[Candidate], profiles: Sequence[_Profile]) -> list[int]:
    """Positions by start, then by value: the order pairs are joined in, whatever the input."""

    def key(position: int) -> tuple[datetime, str, str]:
        candidate = candidates[position]
        hints = candidate.hints.model_dump_json()
        return profiles[position].start, event_to_line(candidate.event), hints

    return sorted(range(len(candidates)), key=key)


def _group(
    candidates: Sequence[Candidate], settings: DedupSettings, listing_urls: Collection[str]
) -> list[list[Candidate]]:
    """Split ``candidates`` into groups that are one talk each.

    The same uid or source key always joins. A page that listings of two
    of the talks so formed point at, from one source and about the same
    time, is no detail page (``_shared_pages``). A similar pair joins unless a
    member of one group and a member of the other are distinct talks
    (``_distinct``). Pairs linked by a page or a speaker are joined before
    pairs that only have similar titles, so that a listing goes with the
    talk of its speaker; within each round, pairs are taken in an order
    fixed by their values, so which pair is refused does not depend on the
    order of the input.
    """
    groups = _Groups(len(candidates))
    seen: dict[str, int] = {}
    for position, candidate in enumerate(candidates):
        identities = [f"uid {candidate.event.uid}", *candidate.event.source_ids]
        for identity in identities:
            groups.union(position, seen.setdefault(identity, position))
    limit = timedelta(minutes=settings.max_start_difference_minutes)
    talks = ((groups.find(position), c.event) for position, c in enumerate(candidates))
    not_pages = frozenset(listing_urls) | _shared_pages(talks, limit)
    profiles = [_profile(candidate, not_pages) for candidate in candidates]
    order = _canonical_order(candidates, profiles)

    def apart(first: int, second: int) -> bool:
        return any(
            _distinct(profiles[one], profiles[other])
            for one in groups.group_of(first)
            for other in groups.group_of(second)
        )

    def close_pairs() -> Iterator[tuple[int, int]]:
        for index, first in enumerate(order):
            for second in order[index + 1 :]:
                if profiles[second].start - profiles[first].start > limit:
                    break
                yield first, second

    def titles(first: _Profile, second: _Profile) -> bool:
        return _similar_titles(first, second, settings.title_similarity)

    for matches in (_linked, titles):
        for first, second in close_pairs():
            if matches(profiles[first], profiles[second]) and not (
                groups.find(first) == groups.find(second) or apart(first, second)
            ):
                groups.union(first, second)
    return [[candidates[position] for position in group] for group in groups.members()]


def _status(events: Sequence[Event]) -> EventStatus:
    statuses = {event.status for event in events}
    if "cancelled" in statuses:
        return "cancelled"
    return "confirmed" if "confirmed" in statuses else "tentative"


def _first[T](values: Sequence[T | None]) -> T | None:
    return next((value for value in values if value is not None), None)


def _names_a_speaker(title: str, names: Collection[str]) -> bool:
    folded = f" {fold(title)} "
    return any(f" {name} " in folded for name in names if name)


def _title(events: Sequence[Event], speakers: Sequence[Speaker]) -> str:
    """The title of a group whose members are ``events`` in merge order (see the module)."""
    titled = [event for event in events if not event.is_tba]
    if not titled:
        return events[0].title
    names = {canonical_name(speaker.name) for speaker in speakers}
    known = set(_TITLE_FILLER)
    for text in [*names, *(event.series or "" for event in events)]:
        known.update(fold(text).split())
    words = [frozenset(fold(event.title).split()) - known for event in titled]
    naming = [_names_a_speaker(event.title, names) for event in titled]
    kept = [
        index
        for index in range(len(titled))
        if not naming[index]
        or not any(
            not naming[other] and len(words[other]) >= len(words[index])
            for other in range(len(titled))
        )
    ]
    for index in kept:
        if not any(words[other] > words[index] for other in kept):
            return titled[index].title
    return titled[kept[0]].title  # unreachable: a finite family of sets has a maximal one


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
    speakers = merge_speakers([speaker for event in events for speaker in event.speakers])
    merged = lead.evolve(
        uid=min(events, key=lambda event: (event.first_seen, event.uid)).uid,
        title=_title(events, speakers),
        speakers=speakers,
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


def _units(members: Sequence[Candidate]) -> list[list[int]]:
    """Positions of ``members`` joined by shared source keys: one listing seen twice is one."""
    groups = _Groups(len(members))
    seen: dict[str, int] = {}
    for position, member in enumerate(members):
        for key in member.event.source_ids:
            groups.union(position, seen.setdefault(key, position))
    return groups.members()


def _keys(members: Sequence[Candidate], unit: Iterable[int]) -> list[str]:
    return sorted(key for position in unit for key in members[position].event.source_ids)


def _other_speakers(first: _Profile, second: _Profile) -> bool:
    """Whether two listings of one source name speakers, and not the same ones."""
    return bool(
        first.sources & second.sources
        and first.speakers
        and second.speakers
        and first.speakers != second.speakers
    )


def _leaving(uid: str, members: Sequence[Candidate]) -> list[list[int]]:
    """The units of ``members`` (listings that all have ``uid``) that must leave the uid.

    The unit that holds the key ``uid`` was derived from comes first, then
    the others by their smallest key, the same order in every run. A unit
    stays unless it names other speakers than a unit of its source that
    stayed (``_other_speakers``).
    """
    profiles = [_profile(member, ()) for member in members]

    def rank(unit: list[int]) -> tuple[bool, str]:
        keys = _keys(members, unit)
        return all(uid_for_key(key) != uid for key in keys), keys[0]

    staying: list[int] = []
    leaving: list[list[int]] = []
    for unit in sorted(_units(members), key=rank):
        if any(
            _other_speakers(profiles[one], profiles[other]) for one in unit for other in staying
        ):
            leaving.append(unit)
        else:
            staying += unit
    return leaving


def _separated_once(candidates: Sequence[Candidate]) -> list[Candidate]:
    """One round of ``separate_listings``."""
    positions: dict[str, list[int]] = defaultdict(list)
    for position, candidate in enumerate(candidates):
        positions[candidate.event.uid].append(position)
    moved: dict[int, str] = {}
    for uid, found in positions.items():
        if len(found) > 1:
            members = [candidates[position] for position in found]
            for unit in _leaving(uid, members):
                own = uid_for_key(_keys(members, unit)[0])
                moved |= {found[index]: own for index in unit}
    return [
        candidate.evolve(event=candidate.event.evolve(uid=moved[position]))
        if position in moved
        else candidate
        for position, candidate in enumerate(candidates)
    ]


def separate_listings(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Give a listing a uid of its own when the uid it has also holds another talk of its source.

    The uid map binds every key that was ever part of an event to its
    uid. Two listings of one source can end up bound to one uid: one of
    them named no speaker yet and joined the other by its title, or one
    took over the uid of the other while that was away. Once both name
    speakers, and not the same ones, they are two talks, and the uid must
    not hold them together, or de-duplication would merge them for good
    (the same uid always joins).

    Of the listings that have one uid, the one that holds the key the uid
    was derived from (``seminars.pipeline.ids.uid_for_key``) keeps it, or
    else the one with the smallest key; listings that share a key go
    together. The others follow in the order of their smallest key, and
    one that names other speakers than a listing of its source that kept
    the uid gets the uid of its smallest key; the uid map then binds its
    keys to that uid (``DedupResult.aliases``). Its new uid may hold a
    third listing, so this repeats until nothing moves; a listing that
    moved holds the key its uid was derived from, so it never moves again.

    The candidates come back in their order; the result does not depend
    on it.
    """
    current = list(candidates)
    while (separated := _separated_once(current)) != current:
        current = separated
    return current


def _adoptable(orphan: _Profile, candidate: _Profile, threshold: int) -> bool:
    """Whether ``candidate`` is the stored ``orphan`` under a new key (step 1 of adoption).

    Within one source, two listings that both name speakers are the same
    talk exactly when they name the same people, however many; a page is
    no link there, because a listing that comes back under a new key has
    a new page or a page that is not its own. An orphan that this source
    knows by its native id needs the same people on both sides: a native
    id stays what it was when a title or a speaker is announced, so a
    similar title (a series title, or one that grew a subtitle) is another
    talk of the series. Otherwise the rules of de-duplication apply.
    """
    shared = orphan.sources & candidate.sources
    if shared and orphan.speakers and candidate.speakers:
        return orphan.speakers == candidate.speakers
    if shared & orphan.native:
        return False
    same_page = orphan.page is not None and orphan.page == candidate.page
    return (
        (same_page and not shared)
        or bool(orphan.speakers & candidate.speakers)
        or _similar_titles(orphan, candidate, threshold)
    )


def _adopt_similar(
    candidates: Sequence[Candidate],
    positions: Sequence[int],
    orphans: Sequence[Event],
    settings: DedupSettings,
    not_pages: Collection[str],
) -> dict[int, str]:
    """Candidate positions with the uid of an orphan that is the same talk (``_adoptable``)."""
    profiles = [(event, _profile(Candidate(event=event), not_pages)) for event in orphans]
    limit = timedelta(minutes=settings.max_start_difference_minutes)
    adopted: dict[int, str] = {}
    taken: set[str] = set()
    for position in positions:
        profile = _profile(candidates[position], not_pages)
        for event, other in profiles:
            close = abs(other.start - profile.start) <= limit
            if (
                event.uid not in taken
                and close
                and _adoptable(other, profile, settings.title_similarity)
            ):
                adopted[position] = event.uid
                taken.add(event.uid)
                break
    return adopted


def _slot_sources(event: Event) -> frozenset[str]:
    """The sources that know ``event`` only by the time of day of its start.

    For such a source every key of the event is a time-of-day day key
    (``seminars.pipeline.ids.is_time_of_day_key``). A native id or the
    address of a page of its own never changes when a title or a speaker
    is announced, so an event with such a key comes back under it.
    """
    stable = {split_source_key(key)[0] for key in event.source_ids if not is_time_of_day_key(key)}
    return _sources_of(event) - stable


def _placeholders(orphans: Iterable[Event]) -> dict[tuple[str, datetime], Event]:
    """Orphans that are TBA, without a speaker and known by a time key, by source and start.

    A source and start that more than one TBA orphan without a speaker
    shares is left out, whatever their keys: it would be a guess which of
    them a talk is.
    """
    found: dict[tuple[str, datetime], list[Event]] = defaultdict(list)
    for event in orphans:
        if event.is_tba and not event.speakers:
            for source_id in _sources_of(event):
                found[(source_id, event.start)].append(event)
    return {
        slot: events[0]
        for slot, events in found.items()
        if len(events) == 1 and slot[0] in _slot_sources(events[0])
    }


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
       same detail page), with three differences for a listing of a source
       the orphan has a key of. The same people make it the same talk and
       other people another talk, whatever their number; and the same
       page is no reason, because a listing of that source whose key
       changed has a new page or one that is not its own: a TBA talk with
       a native id on a series page would otherwise hand its uid to the
       next talk on that page, and both would be one event once it is
       back. An orphan that source knows by its native id is taken only
       by the same people, never by a similar title alone: its key does
       not change, so a listing under a new key with the series title is
       the next talk of the series. A page that two talks of one source
       point at about the same time, in this run or among the stored
       events, is no detail page at all. Step 1 covers a page whose
       address changed when its title was announced: same source, same
       start, same speakers;
    2. otherwise a placeholder of its source: an orphan of the same source
       that starts at exactly the same moment, is still "TBA", names no
       speaker, is known to that source only by the time of day of its
       start (``<source>:day:<date>:t<HHMM>``), and is the only TBA orphan
       without a speaker of that source and start. Such a talk has nothing
       the rules of step 1 could compare, and its key changes as soon as
       the title or a speaker is announced. An orphan with a native id or
       a page of its own is never a placeholder: its key does not change,
       so a new key at its start is another talk, and taking its uid would
       bind both keys to one uid and merge two talks when it comes back.

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
    limit = timedelta(minutes=settings.max_start_difference_minutes)
    not_pages = (
        frozenset(listing_urls)
        | _shared_pages(((c.event.uid, c.event) for c in candidates), limit)
        | _shared_pages(((event.uid, event) for event in stored), limit)
    )
    adopted = _adopt_similar(candidates, unknown, orphans, settings, not_pages)
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
