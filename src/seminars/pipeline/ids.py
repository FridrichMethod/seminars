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
     or else the time of day: ``<source id>:day:<date>:<words>``.

2. The key is looked up in data/uid_map.json (``UidMap``). A known key
   gives the uid it was bound to, in every run. A new key gets a uid
   derived from the key alone (``uid_for_key``), so the result does not
   depend on the order in which events are processed.

When several sources list one talk, de-duplication keeps the uid that was
seen first and binds the keys of the others to it (``seminars.pipeline.dedup``).
A key of the third kind changes when the talk gains its title or a speaker;
``seminars.pipeline.dedup.adopt_stored_uids`` then hands the stored uid on to
the new key, so the uid stays.

A native id is whatever a site sent. One that is very long, or that looks
like a credential, is replaced by its SHA-256 digest before it becomes
part of a key, so it can neither bloat the uid map nor trip the secret
scan of every later run.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from seminars.models import EventDraft, SourceConfig, UidMap, source_key, split_source_key
from seminars.pipeline.text import fold

MAX_NATIVE_ID_LENGTH: Final = 200
DIGEST_LENGTH: Final = 24
URL_KEY_PREFIX: Final = "url:"
DAY_KEY_PREFIX: Final = "day:"
HASHED_ID_PREFIX: Final = "sha256:"

_READABLE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
_SLUG_CHARACTERS: Final = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_LENGTH: Final = 60


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


def slug(text: str) -> str:
    """``text`` as lower-case ASCII words joined by hyphens (accents folded, rest dropped)."""
    return _SLUG_CHARACTERS.sub("-", fold(text)).strip("-")[:_MAX_SLUG_LENGTH].strip("-")


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
    words = slug(speaker or "") or slug(title or "") or start.strftime("t%H%M")
    return source_key(draft.source_id, f"{DAY_KEY_PREFIX}{start.date().isoformat()}:{words}")


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
