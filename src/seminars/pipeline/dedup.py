"""De-duplication: several listings of one talk become one event.

Many talks appear twice, on the campus calendar and on the page of the
department that hosts them. Two candidates are the same talk when

* they have the same uid or share a source key (the same listing seen
  twice, or keys that an earlier run bound to one uid), or
* their starts are at most ``dedup.max_start_difference_minutes`` apart and
  one of these holds: the titles are similar
  (``rapidfuzz.fuzz.token_set_ratio`` of at least ``dedup.title_similarity``),
  they share a speaker, the title of one names a speaker of the other by a
  name of at least two words that reads like a person's (a campus
  calendar's "MRC Public Lecture: Tadashi Tokieda" without speakers,
  against the host's listing of Tadashi Tokieda's talk; a speaker
  mis-read as "Computer Science" is no person, see
  ``seminars.pipeline.speakers.looks_like_name``), or they point at the
  same detail page, or
* they start at exactly the same moment and the detail page of one is
  named after such a speaker of the other: the last part of its address
  holds the speaker's surname as a word. A host can list a talk by its
  title alone on "/events/breakthroughs/Mehnert.html", while the campus
  calendar announces "Breakthroughs in Cancer: Janice Mehnert, MD", a
  title that shares no word with the host's, or
* one of them is all day, the other gives its hours, one of them is a
  meeting, and they point at the same detail page: a meeting that a
  curated list or its host gives as whole days and a calendar lists with
  its hours, once or day by day. The listing with hours starts on one of
  those days and lasts at least ``seminars.models.MEETING_MIN_TIMED_HOURS``
  hours: the opening keynote of a symposium links the page of the
  symposium too.

Meetings (conferences and long workshops, ``Event.is_meeting``) are
compared by title more strictly than talks: a meeting is one with another
listing by their titles only when the folded titles are equal. All-day
meetings of one day all start at midnight, and "ICML 2027" is similar to
"ICLR 2027" by any fuzzy measure; "AIMI Symposium: Opening keynote" is
similar to "AIMI Symposium" as well, and the keynote is not the
symposium. The same title at about the same time is one listing, though,
even when only one of them is a meeting: a calendar that gives the hours
of a symposium makes it a conference by the title rule, while a host that
gives only its start leaves it a talk. And a listing with hours that ends
before a meeting does is a part of it, such as its opening keynote at the
start of the symposium: the page of the meeting that it links, or a
speaker of the meeting that it names, does not make it the meeting. Such
a link joins a meeting and a talk only when the talk has no end or ends
no earlier than the meeting.

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
when its uid also holds another listing of its source, so the rules above
decide again in every run and a merge that later evidence refutes is
undone. That is the one known gap of the guard: two talks of one source
at one time, one of which names no speaker yet, can be one event for a
run; once both name their speakers, the next run takes them apart again.

Everything that is connected by such pairs forms one group, and each group
becomes one event:

* the uid is the one that was seen first (``first_seen``, then the uid
  itself), so a calendar client keeps the entry it already has;
* the title is one that is not "TBA". Titles are compared by their words
  without those of the series, of the speakers, degrees and titles of
  persons ("PhD", "MD", "Prof.") and fillers such as "seminar",
  "department", "Stanford" or "with". A title that names a speaker of the
  talk gives way to one that does not in two cases. Either it wraps that
  one: it holds all of that one's words and adds fewer words than it
  holds, and either it adds none or it is words in front, the name, and
  then that one's words with no added word among or after them
  ('Biology Seminar Series - Jane Doe, MD "Protein design"' adds one word
  to the two of "Protein design" and gives way to it). A title that puts
  the generic words in front of the name wraps nothing: "Machine Learning
  Seminar: Jane Doe, Diffusion" stays against "Machine Learning Seminar",
  and "Physics Colloquium: Jane Doe, Quantum materials" adds two words to
  the one of "Physics Colloquium" anyway. Or that one has strictly more
  words (a host that announces a talk by its speaker, "CSB Special
  Seminar & Open Discussion, David Garfield, PhD", gives way to the title
  of the talk).
  Of the rest, a title whose words are strictly more than those of
  another wins ("Diffusion models for protein design" over "Diffusion
  models", any title over the bare name of its series), and so does, of
  two titles that both name a speaker, the one with more words ("Tadashi
  Tokieda: Surprises from rubbing the wrong way" over "MRC Public
  Lecture: Tadashi Tokieda"); otherwise the order below decides.
  The rules count words and their order around the name, and three known
  cases remain: (1) a generic title that no series name removes and that
  shares no word with a talk title that names the speaker ("Applied
  Physics Colloquium" against "Protein design with diffusion models -
  Jane Doe (MIT)") leaves the choice to the order, so the host's title
  stays, whichever it is; (2) a generic title with more words than a talk
  title that names the speaker ("Genetics and Genomics Seminar Series"
  against "Jane Doe: CRISPR") wins as an announcement would; (3) a talk
  title that names the speaker after the talk and adds a word of its own
  ("Protein design - Jane Doe (MIT)") wraps nothing and stays against
  "Protein design" as the title that says more. And a talk title of few
  words stays behind a wrapper that adds as many words as it holds ("Jane
  Doe (MIT): Diffusion" over "Diffusion");
* the longest abstract wins, speakers are united, and every source key and
  source address is kept;
* the remaining values come from the listing of the host rather than from
  an aggregator (``SourceConfig.aggregator``): the address of the detail
  page, the times, the place, the host. Of two hosts (or two aggregators)
  the one whose source key sorts first leads; that is the order the title
  rule falls back on too. The uid plays no part in it, since the next run
  hands every listing of the talk the merged uid, and the merged event
  must stay what it was;
* a group with a meeting is a meeting. Its type is that of the first
  meeting in the order above, and when the lead is no meeting, the times
  are that meeting's as well: a calendar gives the hours of a symposium,
  and the host that leads gives only its start. When a listing is all day
  and covers more days than those times, the all-day listing with the
  most days gives the times instead, so a meeting of a week that a
  curated list gives keeps its days against a calendar's listing of its
  first day, whichever leads. Its title is chosen by the rules above
  among the listings that cover at least as many days as the one that
  gives the times: a listing of one day or one session of a meeting
  ("XYZ Workshop 2027 - Day 1", "Poster session") never names it;
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
from datetime import date, datetime, timedelta
from typing import Final
from urllib.parse import unquote, urlsplit

from rapidfuzz import fuzz

from seminars.config import DedupSettings
from seminars.models import (
    MEETING_MIN_TIMED_HOURS,
    Candidate,
    Event,
    EventHints,
    EventStatus,
    Speaker,
    canonical_fields,
    event_to_line,
    split_source_key,
)
from seminars.pipeline.ids import (
    canonical_url,
    follow_changed_keys,
    is_native_key,
    reclaim_timed_keys,
    same_people,
    tell_apart_day_keys,
    uid_for_key,
)
from seminars.pipeline.normalize import merge_speakers
from seminars.pipeline.speakers import looks_like_name
from seminars.pipeline.text import canonical_name, fold, word_set

MIN_TITLE_WORDS: Final = 2
"""Titles with fewer words are not compared: "Seminar" is part of too many titles."""
_ONE_DAY: Final = timedelta(days=1)
_JUST_BEFORE: Final = timedelta(microseconds=1)


_TITLE_FILLER: Final = word_set(
    "a an and at by for from in of on the to with seminar seminars talk talks lecture "
    "colloquium series special department dept stanford university "
    "dr prof professor md phd mph msc dphil"
)
"""Words that say nothing about a talk when titles of one talk are compared.

Besides fillers, these are the words of hosts that every title of a
campus calendar can carry and the degrees and titles of persons: neither
says what a talk is about.
"""


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
    words: str  # the folded title between spaces, to find names in
    speakers: frozenset[str]
    persons: frozenset[str]  # the speakers whose names read like a person's, of two words or more
    surnames: frozenset[str]  # the last word of each of ``persons``
    page: str | None  # canonical address of the detail page, if it is one
    page_words: frozenset[str]  # the folded words of the last part of the address of ``page``
    sources: frozenset[str]  # ids of the sources whose keys the event holds
    native: frozenset[str]  # ids of the sources whose native id the event holds
    meeting: bool = False  # a conference or a long workshop (``Event.is_meeting``)
    all_day: bool = False
    end: datetime | None = None


def _detail_page(event: Event, not_pages: Collection[str]) -> str | None:
    address = canonical_url(str(event.url))
    if address in not_pages or not urlsplit(address).path.strip("/"):
        return None
    return address


def _page_words(page: str | None) -> frozenset[str]:
    """The folded words of the last part of the path of ``page`` ("mehnert", "html")."""
    if page is None:
        return frozenset()
    return frozenset(fold(unquote(urlsplit(page).path.rpartition("/")[2])).split())


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
    persons = frozenset(
        name
        for speaker in event.speakers
        if looks_like_name(speaker.name) and " " in (name := canonical_name(speaker.name))
    )
    page = _detail_page(event, not_pages)
    return _Profile(
        start=event.start,
        title=title if comparable else None,
        words=f" {title} ",
        speakers=frozenset(name for name in names if name),
        persons=persons,
        surnames=frozenset(name.split()[-1] for name in persons),
        page=page,
        page_words=_page_words(page),
        sources=_sources_of(event),
        native=frozenset(key.partition(":")[0] for key in event.source_ids if is_native_key(key)),
        meeting=event.is_meeting,
        all_day=event.all_day,
        end=event.end,
    )


def _linked(first: _Profile, second: _Profile) -> bool:
    """The strong reasons: the same detail page, a shared speaker, a speaker in a title or page."""
    return (
        (first.page is not None and first.page == second.page)
        or bool(first.speakers & second.speakers)
        or _names_one_of(first, second.persons)
        or _names_one_of(second, first.persons)
        or _page_of_speaker(first, second)
        or _page_of_speaker(second, first)
    )


def _page_of_speaker(profile: _Profile, other: _Profile) -> bool:
    """Whether ``profile`` has a detail page named after a speaker of ``other``, at one start."""
    return profile.start == other.start and bool(profile.page_words & other.surnames)


def _names_one_of(profile: _Profile, names: Collection[str]) -> bool:
    """Whether the title of ``profile`` names one of ``names`` (folded names of persons)."""
    return any(f" {name} " in profile.words for name in names)


def _similar_titles(first: _Profile, second: _Profile, threshold: int) -> bool:
    """Whether two titles are similar enough to make one talk (see the module docstring).

    A meeting is compared strictly: it is the same event as another
    listing, a meeting or a talk, by its title only when the folded titles
    are equal. All-day meetings of one day all start at midnight, and
    "ICML 2027" against "ICLR 2027", or "RECOMB 2027" against
    "RECOMB-Seq 2027", are similar by any fuzzy measure.
    """
    if first.title is None or second.title is None:
        return False
    if first.meeting or second.meeting:
        return first.title == second.title
    return fuzz.token_set_ratio(first.title, second.title) >= threshold


def _same_page(first: _Profile, second: _Profile) -> bool:
    return first.page is not None and first.page == second.page


def _last_day(start: datetime, end: datetime | None, *, all_day: bool) -> date:
    """The local day an event ends on (its end is exclusive); its first day without an end."""
    if end is None:
        return start.date()
    return end.date() - _ONE_DAY if all_day else (end - _JUST_BEFORE).date()


def _days_and_hours(first: _Profile, second: _Profile) -> bool:
    """Whether one of two listings is all day and the other gives the hours of one of its days.

    One of the two is a meeting, either one; the listing with hours starts
    on one of the days of the other and lasts at least
    ``MEETING_MIN_TIMED_HOURS`` hours.
    """
    if first.all_day == second.all_day or not (first.meeting or second.meeting):
        return False
    whole, timed = (first, second) if first.all_day else (second, first)
    if timed.end is None or timed.end - timed.start < timedelta(hours=MEETING_MIN_TIMED_HOURS):
        return False
    last = _last_day(whole.start, whole.end, all_day=True)
    return whole.start.date() <= timed.start.date() <= last


def _ends_before(part: _Profile, meeting: _Profile) -> bool:
    """Whether ``part``, a listing with hours, ends before the end of ``meeting``."""
    if part.all_day or part.end is None:
        return False
    end = meeting.end
    if end is None and meeting.all_day:
        end = meeting.start + _ONE_DAY
    return end is not None and part.end < end


def _part_of_meeting(first: _Profile, second: _Profile) -> bool:
    """Whether one of two listings is a meeting and the other a part of it (see the module).

    The other is no meeting, gives its hours and ends before the meeting
    does: the opening keynote of a symposium, which links the page of the
    symposium and names one of its speakers.
    """
    if first.meeting == second.meeting:
        return False
    meeting, part = (first, second) if first.meeting else (second, first)
    return _ends_before(part, meeting)


def _strong_link(first: _Profile, second: _Profile) -> bool:
    """A strong reason (``_linked``) that does not join a meeting and a part of it."""
    return _linked(first, second) and not _part_of_meeting(first, second)


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

    def day_pairs() -> Iterator[tuple[int, int]]:
        """Pairs of an all-day listing and one that gives the hours of one of its days."""
        for index, first in enumerate(order):
            whole = profiles[first]
            if not whole.all_day:
                continue
            last = _last_day(whole.start, whole.end, all_day=True)
            for second in order[index + 1 :]:
                if profiles[second].start.date() > last:
                    break
                if _days_and_hours(whole, profiles[second]):
                    yield first, second

    def titles(first: _Profile, second: _Profile) -> bool:
        return _similar_titles(first, second, settings.title_similarity)

    rounds = ((_strong_link, close_pairs), (_same_page, day_pairs), (titles, close_pairs))
    for matches, pairs in rounds:
        for first, second in pairs():
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


def _after_names(title: str, names: Collection[str]) -> list[str]:
    """The folded words of ``title`` after the last of ``names`` (folded names) that it holds."""
    words = fold(title).split()
    end = 0
    for name in names:
        part = name.split()
        for start in range(len(words) - len(part) + 1):
            if part and words[start : start + len(part)] == part:
                end = max(end, start + len(part))
    return words[end:]


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
    tails = [_after_names(event.title, names) for event in titled]

    def wraps(index: int, other: int) -> bool:
        """Whether the title at ``index`` wraps ``other``: words in front, a name, ``other``."""
        inner, added = words[other], words[index] - words[other]
        if not (inner and inner <= words[index] and len(inner) > len(added)):
            return False
        if not added:
            return True
        tail = tails[index]
        if not inner <= set(tail):
            return False
        first = min(tail.index(word) for word in inner)
        return not added.intersection(tail[first:])

    def gives_way(index: int) -> bool:
        """Whether the title at ``index``, which names a speaker, yields to one without."""
        return any(
            not naming[other] and (wraps(index, other) or len(words[other]) > len(words[index]))
            for other in range(len(titled))
        )

    def beaten(index: int) -> bool:
        """Whether a kept title says more than the one at ``index`` (see the module)."""
        return any(
            words[other] > words[index]
            or (naming[index] and naming[other] and len(words[other]) > len(words[index]))
            for other in kept
        )

    kept = [index for index in range(len(titled)) if not naming[index] or not gives_way(index)]
    for index in kept:
        if not beaten(index):
            return titled[index].title
    return titled[kept[0]].title  # unreachable: one with the most words is never beaten


def _days(event: Event) -> int:
    """The number of local days that ``event`` covers, by their dates."""
    last = _last_day(event.start, event.end, all_day=event.all_day)
    return (last - event.start.date()).days + 1


def _timing(events: Sequence[Event]) -> Event:
    """The member of a group, in merge order, whose times the merged event takes.

    That is the lead, or the first meeting when the lead is none; but an
    all-day member that covers more days gives them instead, the one with
    the most days (see the module docstring).
    """
    meetings = [event for event in events if event.is_meeting]
    timing = meetings[0] if meetings and not events[0].is_meeting else events[0]
    longer = [event for event in events if event.all_day and _days(event) > _days(timing)]
    return max(longer, key=_days) if longer else timing


def _merge(group: Sequence[Candidate], aggregators: Collection[str]) -> Candidate:
    """One candidate for a group; a group of one comes back in canonical form."""

    def rank(candidate: Candidate) -> tuple[bool, tuple[str, ...], str, str]:
        # By source keys, not by uid: the next run gives every member the merged uid.
        sources = candidate.hints.source_ids
        aggregated = bool(sources) and all(source in aggregators for source in sources)
        event = candidate.event
        keys = tuple(sorted(event.source_ids))
        return aggregated, keys, event_to_line(event), candidate.hints.model_dump_json()

    members = sorted(group, key=rank)
    events = [member.event for member in members]
    lead = events[0]
    timing = _timing(events)
    meeting = next((event for event in events if event.is_meeting), lead)
    abstracts = [event.abstract for event in events if event.abstract]
    same_time = [e for e in events if e.start == timing.start and e.all_day == timing.all_day]
    hints = members[0].hints
    for member in members[1:]:
        hints = hints.merge(member.hints)
    speakers = merge_speakers([speaker for event in events for speaker in event.speakers])
    named = (
        [event for event in events if _days(event) >= _days(timing)]
        if meeting.is_meeting
        else events
    )
    merged = lead.evolve(
        uid=min(events, key=lambda event: (event.first_seen, event.uid)).uid,
        title=_title(named, speakers),
        speakers=speakers,
        series=_first([event.series for event in events]),
        event_type=meeting.event_type,
        start=timing.start,
        end=_first([timing.end, *(event.end for event in same_time)]),
        all_day=timing.all_day,
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


def _leaving(uid: str, members: Sequence[Candidate]) -> list[list[int]]:
    """The units of ``members`` (listings that all have ``uid``) that must leave the uid.

    The unit that holds the key ``uid`` was derived from comes first, then
    the others by their smallest key, the same order in every run. A unit
    leaves when a unit of one of its sources stayed.
    """

    def rank(unit: list[int]) -> tuple[bool, str]:
        keys = _keys(members, unit)
        return all(uid_for_key(key) != uid for key in keys), keys[0]

    staying: set[str] = set()
    leaving: list[list[int]] = []
    for unit in sorted(_units(members), key=rank):
        sources = {split_source_key(key)[0] for key in _keys(members, unit)}
        if sources & staying:
            leaving.append(unit)
        else:
            staying |= sources
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
    """Give a listing a uid of its own when the uid it has also holds a listing of its source.

    The uid map binds every key that was ever part of an event to its
    uid, so two listings of one source can arrive under one uid: one of
    them joined the other by its title before it named a speaker, or one
    took over the uid of the other while that was away (``adopt_stored_uids``).
    One source lists a talk once, so two listings of it in one run are one
    talk only when de-duplication finds them to be by its rules; the uid
    must not hold them together, or they would be merged for good (the
    same uid always joins).

    Of the listings that have one uid, the one that holds the key the uid
    was derived from (``seminars.pipeline.ids.uid_for_key``) keeps it, or
    else the one with the smallest key; listings that share a key go
    together, and listings of other sources keep the uid as well. Every
    other listing of a source that kept the uid gets the uid of its
    smallest key; the uid map then binds its keys to that uid
    (``DedupResult.aliases``) unless de-duplication merges it back. Its new
    uid may hold a third listing, so this repeats until nothing moves; a
    listing that moved holds the key its uid was derived from, so it never
    moves again.

    Before that, talks of one source that share a day key but start at
    different times get a key each (``seminars.pipeline.ids.tell_apart_day_keys``).

    The candidates come back in their order; the result does not depend
    on it.
    """
    current = tell_apart_day_keys(candidates)
    while (separated := _separated_once(current)) != current:
        current = separated
    return current


def _adoptable(orphan: _Profile, candidate: _Profile, threshold: int) -> bool:
    """Whether ``candidate`` is the stored ``orphan`` under a new key (step 1 of adoption).

    Within one source, two listings that both name speakers are the same
    talk exactly when they name the same people, however many and in
    whatever spelling (``seminars.pipeline.ids.same_people``); a page is
    no link there, because a listing that comes back under a new key has
    a new page or a page that is not its own. An orphan that this source
    knows by its native id needs the same people on both sides: a native
    id stays what it was when a title or a speaker is announced, so a
    similar title (a series title, or one that grew a subtitle) is another
    talk of the series. Otherwise the rules of de-duplication apply.
    """
    shared = orphan.sources & candidate.sources
    if shared and orphan.speakers and candidate.speakers:
        return same_people(orphan.speakers, candidate.speakers)
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


def adopt_stored_uids(
    candidates: Sequence[Candidate],
    stored: Sequence[Event],
    *,
    settings: DedupSettings,
    listing_urls: Collection[str] = (),
) -> list[Candidate]:
    """Let a new candidate take over the uid of a stored event that is the same talk.

    A source sometimes changes the key of an event: a page gets a new
    address, a talk gains a speaker or its title, a talk known by its day
    moves to another day or its speaker's name is spelled anew. The event
    then arrives under a uid that was never seen, while its stored self is
    no longer delivered, and a subscriber would see the talk twice until
    the old entry expires (and for good after importing an older and then
    a newer file).

    First, a talk that shared its day key with a later talk of its day
    gets its uid back (``seminars.pipeline.ids.reclaim_timed_keys``).
    Then a candidate whose uid is not stored takes the uid of a stored
    event that no candidate of this run claims (an orphan), in five steps:

    1. an orphan that is the same talk by the rules of de-duplication
       (start within the limit and a similar title, a shared speaker or the
       same detail page), with three differences for a listing of a source
       the orphan has a key of (``_adoptable``). The same people in any
       spelling make it the same talk and other people another talk; the
       same page is no reason, because a listing of that source whose key
       changed has a new page or one that is not its own; and an orphan
       that source knows by its native id is taken only by the same
       people, never by a similar title alone, because its key does not
       change. A page that two talks of one source point at about the
       same time, in this run or among the stored events, is no detail
       page at all;
    2. an orphan moved to another day: a source knows both only by day
       keys, both start after the time of the run (which normalization
       stamps on every candidate as ``last_changed``; a talk that is over
       and left its page is no talk that moved) and at most
       ``seminars.pipeline.ids.MAX_MOVE`` apart, they name the same people,
       and their titles are similar unless one is "TBA" ("RESCHEDULED:
       Quantum codes" is "Quantum codes"). Only a pair of which neither
       side matches anything else counts, so two speakers who swap their
       days keep their uids;
    3. a slot of its source: an orphan of the same source that starts at
       exactly the same moment, names no speaker, is known to that source
       only by day keys, and is the only orphan without a speaker of that
       source and start. Its key changes with its title ("TBA", or a
       generic "Theory Lunch" that gives way to the title of the talk) and
       with a speaker that is announced;
    4. a page of its own with a new address: the orphan is still "TBA",
       neither names a speaker, the same source knows both only by their
       pages, the two pages lie in one directory
       (``seminars.pipeline.ids.renamed_page``) and the starts are the same
       moment; again only a pair that is alone. A page with a title of its
       own that gives way to another page is another talk;
    5. an orphan moved as in step 2 when neither names a speaker: both
       titles are "TBA", or neither is and they are similar. The title is
       all there is to go by, so this comes after the slots of step 3.

    Steps 2 to 5 are ``seminars.pipeline.ids.follow_changed_keys``. An
    orphan with a native id is never taken there: its key does not change,
    so a new key is another talk. Should an orphan come
    back after its uid was taken, both listings arrive under one uid and
    ``separate_listings`` gives them a uid each again. In steps 1 and 3 the
    orphan seen first is taken; each orphan is taken once. The candidates
    come back in their order; the choice does not depend on it.
    """
    candidates = reclaim_timed_keys(candidates, stored, title_similarity=settings.title_similarity)
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
    left = [position for position in unknown if position not in adopted]
    later = follow_changed_keys(
        [candidates[position].event for position in left],
        [event for event in orphans if event.uid not in set(adopted.values())],
        now=max((c.event.last_changed for c in candidates), default=None),
        title_similarity=settings.title_similarity,
    )
    adopted |= {left[index]: uid for index, uid in later.items()}
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
