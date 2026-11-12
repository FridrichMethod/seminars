"""Stable event ids: the key a source gives an event and the uid derived from it.

Calendar clients tell events apart by uid, so a uid never changes once it
was assigned, whatever happens to the title or the time. Two steps make
that hold:

1. Every draft gets exactly one *source key* (``seminars.models.source_key``),
   built from the best identity its source offers, in this order:

   * the native id of the source (Localist instance id, JSON:API resource
     id, RSS guid, ICS UID): ``<source id>:<native id>``;
   * the address of its own detail page: ``<source id>:url:<address>``. A
     page counts as the detail page of an event only when no other draft
     of the same source and run points at it and it is not one of the
     addresses the source itself is fetched from. A page that lists a
     whole series would otherwise make all its talks one event;
   * the local day of the start plus the first speaker, or else the title,
     or else the time of day: ``<source id>:day:<date>:<words>``. The
     words are ASCII (``slug``): accents are folded, a Latin letter without
     a decomposition is spelled out, and a name in another script becomes a
     short digest of itself, so two such talks of one day stay apart.

2. The key is looked up in data/uid_map.json (``UidMap``). A known key
   gives the uid it was bound to, in every run. A new key gets a uid
   derived from the key alone (``uid_for_key``), so the result does not
   depend on the order in which events are processed.

When several sources list one talk, de-duplication keeps the uid that was
seen first and binds the keys of the others to it (``seminars.pipeline.dedup``).
A binding yields to later evidence: two listings of one source that arrive
under one uid in one run are two listings, so one keeps the uid and the
other gets the uid of its own key, and de-duplication alone decides
whether they are one talk (``seminars.pipeline.dedup.separate_listings``).
Two talks of one source and day that would get one day key get a key each
(``tell_apart_day_keys``).

Some keys change while the talk stays: a day key when the talk moves to
another day, gains its title (without a speaker) or a speaker, or the
speaker's name is spelled anew; the address of a page when the site
renames it. ``seminars.pipeline.dedup.adopt_stored_uids`` then hands the
stored uid on to the new key (with ``same_people`` and
``follow_changed_keys``), so the uid stays.

A native id is whatever a site sent. One that is very long, or that looks
like a credential, is replaced by its SHA-256 digest before it becomes
part of a key, so it can neither bloat the uid map nor trip the secret
scan of every later run.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Sequence
from datetime import datetime, timedelta
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from rapidfuzz import fuzz

from seminars.models import (
    Candidate,
    Event,
    EventDraft,
    SourceConfig,
    UidMap,
    source_key,
    split_source_key,
)
from seminars.pipeline.text import canonical_name, fold

MAX_NATIVE_ID_LENGTH: Final = 200
DIGEST_LENGTH: Final = 24
URL_KEY_PREFIX: Final = "url:"
DAY_KEY_PREFIX: Final = "day:"
HASHED_ID_PREFIX: Final = "sha256:"
MIN_FUZZY_NAME_LENGTH: Final = 8
NAME_SIMILARITY: Final = 90
"""Two names of at least this length and ``fuzz.ratio`` are one name spelled two ways."""
MAX_MOVE: Final = timedelta(days=60)
"""How far a talk known by its day may move and keep its uid (``follow_changed_keys``)."""
TIME_OF_DAY_FORMAT: Final = "t%H%M"
"""How the time of day of the start is written in a day key without a speaker or title."""

_TIME_OF_DAY_KEY: Final = re.compile(rf"{DAY_KEY_PREFIX}\d{{4}}-\d{{2}}-\d{{2}}:t\d{{4}}")

_READABLE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
_SLUG_CHARACTERS: Final = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_LENGTH: Final = 60
_OTHER_SCRIPT_PREFIX: Final = "u"
_OTHER_SCRIPT_DIGEST: Final = 10

_LATIN: Final = str.maketrans(
    {
        "\u00e6": "ae",
        "\u00f0": "d",
        "\u00f8": "o",
        "\u00fe": "th",
        "\u0111": "d",
        "\u0127": "h",
        "\u0131": "i",
        "\u0138": "k",
        "\u0142": "l",
        "\u014b": "ng",
        "\u0153": "oe",
        "\u0167": "t",
    }
)
"""Lower-case Latin letters without a decomposition, spelled in ASCII ("l" for l with stroke)."""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:DIGEST_LENGTH]


def canonical_url(url: str) -> str:
    """``url`` in the one spelling used to compare detail pages.

    Scheme and host in lower case, no fragment, no trailing slash on the
    path. The query stays, because many sites tell events apart by it.
    """
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def latin_fold(text: str) -> str:
    """``fold(text)`` with the Latin letters that have no decomposition spelled in ASCII.

    "Lukasz" written with an L with stroke and "Lukasz" fold to the same
    "lukasz", as "Jose" with and without an accent do with ``fold`` alone.
    """
    return fold(text).translate(_LATIN)


def slug(text: str) -> str:
    """``text`` as lower-case ASCII words joined by hyphens (accents folded, rest dropped).

    Text whose letters are all of another script (a name in Chinese) would
    leave no word at all, so it becomes ``u`` and a short digest of its
    folded form instead: two such names stay apart, and one name always
    gives the same words. Text without any letter or digit gives "".
    """
    folded = latin_fold(text)
    words = _SLUG_CHARACTERS.sub("-", folded).strip("-")[:_MAX_SLUG_LENGTH].strip("-")
    if words or not folded:
        return words
    return _OTHER_SCRIPT_PREFIX + _digest(folded)[:_OTHER_SCRIPT_DIGEST]


def safe_native_id(native_id: str, is_secret: Callable[[str], bool]) -> str:
    """``native_id`` as it goes into a key: stripped, and hashed when long or secret-shaped."""
    text = native_id.strip()
    if len(text) > MAX_NATIVE_ID_LENGTH or is_secret(text):
        return HASHED_ID_PREFIX + _digest(text)
    return text


def shared_urls(drafts: Iterable[EventDraft], source: SourceConfig) -> frozenset[str]:
    """Addresses that are not the detail page of one event, in canonical form.

    These are the addresses the source is fetched from (and its homepage)
    and every address that more than one draft of the run points at.
    """
    counts = Counter(canonical_url(draft.url) for draft in drafts if draft.url)
    own = [*source.all_urls, *([source.homepage] if source.homepage else [])]
    repeated = {url for url, count in counts.items() if count > 1}
    return frozenset(repeated | {canonical_url(url) for url in own})


def draft_key(
    draft: EventDraft,
    *,
    start: datetime,
    speaker: str | None,
    title: str | None,
    shared: frozenset[str],
    is_secret: Callable[[str], bool],
) -> str:
    """The source key of a draft (see the module docstring for the order of preference).

    ``start`` is the normalized start, ``speaker`` the name of the first
    speaker and ``title`` the title unless it is still "TBA"; ``shared`` is
    the result of ``shared_urls`` for the drafts of the run.
    """
    native = safe_native_id(draft.native_id or "", is_secret)
    if native:
        return source_key(draft.source_id, native)
    if draft.url and canonical_url(draft.url) not in shared and not is_secret(draft.url):
        return source_key(draft.source_id, URL_KEY_PREFIX + canonical_url(draft.url))
    words = slug(speaker or "") or slug(title or "") or start.strftime(TIME_OF_DAY_FORMAT)
    return source_key(draft.source_id, f"{DAY_KEY_PREFIX}{start.date().isoformat()}:{words}")


def is_time_of_day_key(key: str) -> bool:
    """Whether ``key`` is a day key that names only the time of day of the start.

    That is ``<source id>:day:<date>:t<HHMM>``, the key of a draft with
    neither a native id nor a page of its own, no speaker and no title.
    """
    _, native = split_source_key(key)
    return _TIME_OF_DAY_KEY.fullmatch(native) is not None


def is_day_key(key: str) -> bool:
    """Whether ``key`` is a day key: the source knows the talk by its day, not an id or a page."""
    return split_source_key(key)[1].startswith(DAY_KEY_PREFIX)


def _time_of_day_key(key: str, start: datetime) -> str:
    return f"{key}:{start.strftime(TIME_OF_DAY_FORMAT)}"


def tell_apart_day_keys(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Give talks of one source that share a day key but not the start a key each.

    Two talks of one day with the same first speaker ("Lecture I" at 10:00
    and "Lecture II" at 14:00) or, without speakers, the same title get one
    day key, and the same key always makes one event. The talk that starts
    last keeps the key and its uid; every earlier one gets the key with the
    time of day of its start appended (``...:ann-smith:t1000``) and the uid
    of that key. The last one keeps the key because pages drop talks once
    they are over: when the earlier talks leave the page, the key and the
    uid of the one that is left stay what they were.

    The candidates come back in their order; the result does not depend on
    it, and a second call changes nothing.
    """
    starts: dict[str, set[datetime]] = defaultdict(set)
    for candidate in candidates:
        for key in candidate.event.source_ids:
            if is_day_key(key) and not is_time_of_day_key(key):
                starts[key].add(candidate.event.start)
    latest = {key: max(found) for key, found in starts.items() if len(found) > 1}
    result: list[Candidate] = []
    for candidate in candidates:
        event = candidate.event
        moved = [key for key in event.source_ids if key in latest and event.start < latest[key]]
        if not moved:
            result.append(candidate)
            continue
        keys = [
            _time_of_day_key(key, event.start) if key in moved else key for key in event.source_ids
        ]
        uid = uid_for_key(_time_of_day_key(moved[0], event.start))
        result.append(candidate.evolve(event=event.evolve(uid=uid, source_ids=keys)))
    return result


def is_native_key(key: str) -> bool:
    """Whether ``key`` is built from the native id of its source, not from a page or a day."""
    native = key.partition(":")[2]
    return not native.startswith((URL_KEY_PREFIX, DAY_KEY_PREFIX))


def is_url_key(key: str) -> bool:
    """Whether ``key`` is built from the address of the talk's own page."""
    return split_source_key(key)[1].startswith(URL_KEY_PREFIX)


def sources_known_by(event: Event, kind: Callable[[str], bool]) -> frozenset[str]:
    """The sources every key of which in ``event`` is of one kind (``is_day_key``, ...)."""
    every: dict[str, bool] = {}
    for key in event.source_ids:
        source_id = split_source_key(key)[0]
        every[source_id] = every.get(source_id, True) and kind(key)
    return frozenset(source_id for source_id, found in every.items() if found)


def renamed_page(old: str, new: str) -> bool:
    """Whether two url keys of one source name two pages of one directory, without a query.

    A site that derives the address of a page from its title gives a TBA
    talk a new address once the title is announced, next to the old one:
    ``/events/coming-soon-3`` becomes ``/events/knots-and-braids``.
    """
    first, second = (
        urlsplit(split_source_key(key)[1][len(URL_KEY_PREFIX) :]) for key in (old, new)
    )
    return (
        split_source_key(old)[0] == split_source_key(new)[0]
        and (first.scheme, first.netloc) == (second.scheme, second.netloc)
        and not (first.query or second.query)
        and first.path != second.path
        and first.path.rpartition("/")[0] == second.path.rpartition("/")[0]
    )


def _same_person(first: str, second: str) -> bool:
    one, other = latin_fold(first), latin_fold(second)
    if one == other:
        return True
    words = [word for word in one.split() if len(word) > 1]
    if words and words == [word for word in other.split() if len(word) > 1]:
        return True  # a middle initial more or less
    short = min(len(one), len(other)) < MIN_FUZZY_NAME_LENGTH
    return not short and fuzz.ratio(one, other) >= NAME_SIMILARITY


def same_people(first: Collection[str], second: Collection[str]) -> bool:
    """Whether two sets of folded speaker names name the same people, each in its own spelling.

    Two spellings are one name when they fold to the same letters (an L with
    stroke as "l", see ``latin_fold``), differ only by initials ("Emily B.
    Fox", "Emily Fox") or are long and nearly equal ("Cohen-Adad",
    "Cohen-Addad"). Meant for listings of one source, which do not list two
    people of almost one name for one talk. A side without speakers never
    names the same people.
    """
    if not first or len(first) != len(second):
        return False
    return all(any(_same_person(one, other) for other in second) for one in first) and all(
        any(_same_person(one, other) for one in first) for other in second
    )


def uid_for_key(key: str) -> str:
    """The uid a new source key gets: ``<source id>.<native id>``, or a digest of the key.

    The readable form is used when the native id consists of letters,
    digits, ``.``, ``_`` and ``-`` and is short; anything else becomes
    ``<source id>._<digest>``. Source ids contain no dot and a readable
    native id never starts with ``_``, so two keys never share a uid.
    """
    source_id, native = split_source_key(key)
    if _READABLE.fullmatch(native) is not None:
        return f"{source_id}.{native}"
    return f"{source_id}._{_digest(key)}"


def resolve_uid(key: str, uid_map: UidMap) -> str:
    """The uid of ``key``: the one it is bound to, or the one a new key gets."""
    return uid_map.resolve(key) or uid_for_key(key)


def _one_to_one(pairs: Iterable[tuple[int, str]]) -> dict[int, str]:
    """The pairs of an event and an orphan uid of which neither is in another pair."""
    found = list(pairs)
    events = Counter(index for index, _ in found)
    uids = Counter(uid for _, uid in found)
    return {index: uid for index, uid in found if events[index] == uids[uid] == 1}


def _names(event: Event) -> frozenset[str]:
    return frozenset(name for speaker in event.speakers if (name := canonical_name(speaker.name)))


def _moved(orphan: Event, event: Event, now: datetime, title_similarity: int) -> bool:
    """Whether ``event`` is ``orphan`` moved to another day (see ``follow_changed_keys``)."""
    if not sources_known_by(orphan, is_day_key) & sources_known_by(event, is_day_key):
        return False
    if min(orphan.start, event.start) <= now or abs(event.start - orphan.start) > MAX_MOVE:
        return False
    titled = [fold(item.title) for item in (orphan, event) if not item.is_tba]
    agree = len(titled) < 2 or fuzz.token_set_ratio(titled[0], titled[1]) >= title_similarity
    return agree and same_people(_names(orphan), _names(event))


def _sources(event: Event) -> list[str]:
    return sorted({split_source_key(key)[0] for key in event.source_ids})


def _slots(orphans: Iterable[Event]) -> dict[tuple[str, datetime], Event]:
    """Orphans without a speaker that a source knows only by day keys, by that source and start.

    A source and start that more than one orphan without a speaker shares
    is left out, whatever their keys: it would be a guess which of them a
    talk is.
    """
    found: dict[tuple[str, datetime], list[Event]] = defaultdict(list)
    for event in orphans:
        if not event.speakers:
            for source_id in _sources(event):
                found[(source_id, event.start)].append(event)
    return {
        slot: events[0]
        for slot, events in found.items()
        if len(events) == 1 and slot[0] in sources_known_by(events[0], is_day_key)
    }


def _slotted(events: Sequence[Event], orphans: Sequence[Event]) -> dict[int, str]:
    slots = _slots(orphans)
    adopted: dict[int, str] = {}
    for index, event in enumerate(events):
        for source_id in _sources(event):
            slot = slots.get((source_id, event.start))
            if slot is not None and slot.uid not in adopted.values():
                adopted[index] = slot.uid
                break
    return adopted


def _renamed(orphan: Event, event: Event) -> bool:
    """Whether ``event`` is the TBA ``orphan`` on a page with a new address."""
    if not orphan.is_tba or orphan.speakers or event.speakers or orphan.start != event.start:
        return False
    known = sources_known_by(orphan, is_url_key) & sources_known_by(event, is_url_key)
    return any(
        renamed_page(old, new)
        for old in orphan.source_ids
        for new in event.source_ids
        if split_source_key(old)[0] in known
    )


def follow_changed_keys(
    events: Sequence[Event],
    orphans: Sequence[Event],
    *,
    now: datetime | None,
    title_similarity: int,
) -> dict[int, str]:
    """Which of ``events`` (new keys) are ``orphans`` (stored, not delivered) under a new key.

    Steps 2 to 4 of ``seminars.pipeline.dedup.adopt_stored_uids``, for keys
    that change while the talk stays: a day key when the talk moves to
    another day, a day key without a speaker when the title changes, the
    address of a TBA page when it is renamed. ``orphans`` are in the order
    in which they were first seen, ``now`` is the time of the run and
    ``title_similarity`` the ``fuzz.token_set_ratio`` of two folded titles
    that makes them one, as in de-duplication ("RESCHEDULED: Quantum
    codes" is "Quantum codes"). The result maps positions in ``events`` to
    the uid of an orphan, and each orphan is taken once. Step 3 gives a
    slot to the first event that claims it, so the result depends on the
    order of ``events``: pass them in an order fixed by their values
    (``adopt_stored_uids`` sorts them by value).
    """
    adopted: dict[int, str] = {}

    def step(found: Callable[[list[int], list[Event]], dict[int, str]]) -> None:
        left = [index for index in range(len(events)) if index not in adopted]
        free = [orphan for orphan in orphans if orphan.uid not in adopted.values()]
        adopted.update(found(left, free))

    if now is not None:
        step(
            lambda left, free: _one_to_one(
                (index, orphan.uid)
                for index in left
                for orphan in free
                if _moved(orphan, events[index], now, title_similarity)
            )
        )
    step(
        lambda left, free: {
            left[index]: uid for index, uid in _slotted([events[i] for i in left], free).items()
        }
    )
    step(
        lambda left, free: _one_to_one(
            (index, orphan.uid)
            for index in left
            for orphan in free
            if _renamed(orphan, events[index])
        )
    )
    return adopted
