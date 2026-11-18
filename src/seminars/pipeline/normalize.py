"""Normalization: from the drafts of one source to candidates.

A draft says what a page says. An ``Event`` is what the project stores and
publishes, so this is where the rules for stored data are applied:

* times follow ``seminars.pipeline.times``; an event outside the window of
  the run is dropped and counted: one that starts after it, or ended
  before it began. A meeting of a source with ``meeting_horizon`` has a
  longer window, a title can make a talk a meeting, and a meeting with
  times of day that lasts more than a week keeps its days as an all-day
  event (``seminars.pipeline.meetings``). Any other end that the length
  rule drops (the same wall time on a later day, or more than a week after
  the start) is logged as a warning with the source and the uids, at most
  ``MAX_LOGGED_UIDS`` of them, once per source;
* every text field is plain text: markup removed, cleaned by
  ``seminars.textrules.clean_text``, whitespace tidied, length bounded;
* nothing that lets a stranger in or reaches a person is kept. A stream
  address that carries a credential is dropped (the event only stays
  marked as online); every text loses meeting credentials, e-mail
  addresses and telephone numbers (``seminars.pipeline.privacy``; the
  names and affiliations of speakers keep no marker, and a title or a
  series name keeps a code without a digit, which there is a word of what
  the talk is about), and an address with a
  credential, a login or an e-mail address in it, also encoded or wrapped
  in another link, is not used: the event then points at the page of its
  source and gets a key of its day;
* nothing credential-shaped is kept either: the secret scanner's
  ``redact`` runs over every text, and an address that looks like it holds
  a token is not used. data/ is public, so all of this happens here and
  not when output is written;
* the title "TBA" stands for every way of saying that there is none yet,
  and "Cancelled:" or "(postponed)" in a title sets the status instead;
  an abstract that is only its heading or says that there is none yet
  ("Abstract", "Abstract: TBA") is no abstract;
* a speaker line is split into people (``seminars.pipeline.speakers``). A
  draft that names no speaker at all gets the people its title names in
  one of the common forms (``speakers_from_title``; the campus calendar
  has no speaker field). A key of the day is built from the speakers the
  draft names, never from those of the title, which the title already is;
* the event gets its source key and uid (``seminars.pipeline.ids``).

A draft that cannot become an event (no usable start, no usable address,
values that the model refuses) is reported as excluded with the reason
``invalid``; it never stops the other drafts of its source.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from pydantic import HttpUrl, TypeAdapter, ValidationError

from seminars.config import Settings
from seminars.models import (
    TBA_TITLE,
    Candidate,
    Event,
    EventDraft,
    EventHints,
    EventStatus,
    EventType,
    ExcludedEvent,
    SourceConfig,
    Speaker,
    TimeWindow,
    UidMap,
    ascii_url,
    check_http_url,
    is_meeting_span,
    url_has_credential,
)
from seminars.pipeline.ids import draft_key, resolve_uid, shared_urls
from seminars.pipeline.meetings import in_window, meeting_over_days, meeting_type
from seminars.pipeline.privacy import is_private_link, scrub_private, scrub_title, strip_markers
from seminars.pipeline.speakers import ParsedSpeaker, split_speakers
from seminars.pipeline.text import (
    canonical_name,
    fold,
    is_empty_abstract,
    is_tba,
    one_line,
    paragraphs,
    split_status,
    tidy_name,
    truncate,
    word_set,
)
from seminars.pipeline.times import MAX_TIMED_LENGTH, EventTimes, TimeError, resolve_times
from seminars.pipeline.title_speakers import speakers_from_title

MAX_TITLE: Final = 300
MAX_SERIES: Final = 200
MAX_HOST: Final = 200
MAX_LOCATION: Final = 300
MAX_NAME: Final = 120
MAX_AFFILIATION: Final = 300
MAX_CATEGORY: Final = 100
MAX_SPEAKER_LINE: Final = 2000
MAX_URL: Final = 2000
MAX_SPEAKERS: Final = 20
MAX_CATEGORIES: Final = 50
UNTITLED: Final = "(no title)"
MAX_LOGGED_UIDS: Final = 5

logger: Final = logging.getLogger(__name__)

_HTTP_URL: Final = TypeAdapter(HttpUrl)
_LINK: Final = re.compile(r"https?://\S+", re.IGNORECASE)
_ONLINE_CORE: Final = word_set(
    "online virtual virtually remote remotely zoom webinar livestream webex teams youtube"
)
_ONLINE_FILLER: Final = word_set(
    "a and be by event google held link live meet meeting microsoft on only or stream streamed "
    "the via will"
)
_HYBRID_WORDS: Final = word_set("zoom online livestream webinar hybrid webex")
_NO_LOCATION: Final = frozenset(
    {"tba", "tbd", "tbc", "location tba", "location tbd", "to be announced", "to be determined"}
    | {"united states", "united states of america", "usa", "us"}
)
"""Locations that name no place: not announced yet, or only the country (the address
block of a Stanford Sites card can hold just "United States")."""


def _same(text: str) -> str:
    return text


def _never(text: str) -> bool:
    return False


@dataclass(frozen=True)
class NormalizeContext:
    """What normalization needs besides the drafts of a source.

    ``redact`` and ``is_secret`` come from the secret scanner; the defaults
    change nothing, which is enough for tests of other rules.
    """

    settings: Settings
    now: datetime
    window: TimeWindow
    uid_map: UidMap = field(default_factory=UidMap)
    redact: Callable[[str], str] = _same
    is_secret: Callable[[str], bool] = _never


@dataclass(frozen=True)
class Normalized:
    """The drafts of one source after normalization."""

    candidates: tuple[Candidate, ...] = ()
    invalid: tuple[ExcludedEvent, ...] = ()
    outside_window: int = 0
    redactions: int = 0  # texts and addresses in which something was withheld


class _Invalid(Exception):
    """A draft cannot become an event; the message is an ASCII reason."""


@dataclass
class _Cleaner:
    """Text and address cleaning with the context's redaction; counts what it withholds."""

    context: NormalizeContext
    redactions: int = field(default=0)

    def _finish(
        self, text: str | None, limit: int, scrub: Callable[[str], str] = scrub_private
    ) -> str | None:
        if text is None:
            return None
        safe = self.context.redact(scrub(text))
        if safe != text:
            self.redactions += 1
        return truncate(safe, limit).strip() or None

    def line(self, text: str | None, limit: int) -> str | None:
        return self._finish(one_line(text, limit=4 * limit), limit)

    def title(self, text: str | None, limit: int) -> str | None:
        """A title or a series name: scrubbed by ``scrub_title``, which keeps word codes."""
        return self._finish(one_line(text, limit=4 * limit), limit, scrub_title)

    def block(self, text: str | None, limit: int, *, is_html: bool = False) -> str | None:
        return self._finish(paragraphs(text, limit=4 * limit, is_html=is_html), limit)

    def url(self, url: str | None) -> HttpUrl | None:
        """``url`` when it may be published: http(s), no credential, login, e-mail or token.

        A credential also counts when it is percent-encoded or inside a
        link that this one wraps (``seminars.pipeline.privacy.is_private_link``).
        """
        if not url or len(url) > MAX_URL:
            return None
        try:
            candidate = check_http_url(ascii_url(url.strip()))
            public = _HTTP_URL.validate_python(candidate)
        except (ValueError, ValidationError):
            return None
        if (
            url_has_credential(candidate)
            or is_private_link(candidate)
            or self.context.is_secret(candidate)
        ):
            self.redactions += 1
            return None
        return public


def _abstract(draft: EventDraft, cleaner: _Cleaner) -> str | None:
    """The plain abstract of ``draft``, else its HTML one, unless it says nothing."""
    limit = cleaner.context.settings.events.abstract_max_chars
    for text, is_html in ((draft.abstract, False), (draft.abstract_html, True)):
        block = cleaner.block(text, limit, is_html=is_html)
        if block is not None and not is_empty_abstract(block):
            return block
    return None


def _title_and_status(draft: EventDraft, cleaner: _Cleaner) -> tuple[str, EventStatus]:
    text = cleaner.title(draft.title, MAX_TITLE)
    title, marked = split_status(text) if text else (TBA_TITLE, None)
    if "cancelled" in (draft.status_hint, marked):
        status: EventStatus = "cancelled"
    else:
        status = draft.status_hint or marked or "confirmed"
    return (TBA_TITLE if is_tba(title) else title), status


def _person(name: str | None, affiliation: str | None) -> tuple[str, str | None] | None:
    """A speaker's name and affiliation without removal markers; ``None`` without a name.

    A name that ends in a colon is a label ("Contact:") whose value was
    taken out, not a person.
    """
    person = tidy_name(strip_markers(name or ""))
    if not person or person.endswith(":"):
        return None
    return person, strip_markers(affiliation or "") or None


def _structured_speakers(draft: EventDraft, cleaner: _Cleaner) -> list[Speaker]:
    speakers = []
    for item in draft.speakers:
        found = _person(
            cleaner.line(item.name, MAX_NAME), cleaner.line(item.affiliation, MAX_AFFILIATION)
        )
        if found is not None:
            name, affiliation = found
            speakers.append(Speaker(name=name, affiliation=affiliation, url=cleaner.url(item.url)))
    return speakers


def _line_speakers(draft: EventDraft, cleaner: _Cleaner) -> list[Speaker]:
    line = strip_markers(cleaner.block(draft.speaker_text, MAX_SPEAKER_LINE) or "")
    return _parsed_speakers(split_speakers(line) if line else [])


def _title_speakers(title: str, abstract: str | None) -> list[Speaker]:
    """The people a cleaned title names, for a draft that names no speaker itself."""
    if title == TBA_TITLE:
        return []
    return _parsed_speakers(speakers_from_title(title, abstract))


def _parsed_speakers(people: Sequence[ParsedSpeaker]) -> list[Speaker]:
    found = (_person(person.name, person.affiliation) for person in people)
    return [
        Speaker(
            name=truncate(name, MAX_NAME),
            affiliation=truncate(affiliation, MAX_AFFILIATION) if affiliation else None,
        )
        for name, affiliation in (item for item in found if item is not None)
    ]


def merge_speakers(speakers: Sequence[Speaker]) -> list[Speaker]:
    """``speakers`` without repeats: one entry per person, in order of first appearance.

    Two entries are one person when their names are equal apart from case,
    accents, honorifics and degrees. The first spelling of the name is
    kept; a missing affiliation or address is filled in from a later entry.
    """
    merged: dict[str, Speaker] = {}
    for speaker in speakers:
        key = canonical_name(speaker.name)
        if not key or is_tba(speaker.name):
            continue
        known = merged.get(key)
        if known is None:
            merged[key] = speaker
        elif (known.affiliation is None and speaker.affiliation) or (
            known.url is None and speaker.url
        ):
            merged[key] = known.evolve(
                affiliation=known.affiliation or speaker.affiliation, url=known.url or speaker.url
            )
    return list(merged.values())[:MAX_SPEAKERS]


def _place(draft: EventDraft, cleaner: _Cleaner) -> tuple[str | None, bool, HttpUrl | None]:
    """Location, the online flag and the stream address of a draft."""
    stream = cleaner.url(draft.stream_url)
    online = draft.is_online is True or draft.stream_url is not None
    link = (draft.location or "").strip()
    if draft.is_online is not False and _LINK.fullmatch(link):
        return None, True, stream or cleaner.url(link)
    location = cleaner.line(draft.location, MAX_LOCATION)
    words = fold(location).split() if location is not None else []
    if " ".join(words) in _NO_LOCATION:
        return None, online, stream
    if location is None or draft.is_online is False:
        return location, online, stream
    if _ONLINE_CORE.intersection(words) and all(
        word in _ONLINE_CORE or word in _ONLINE_FILLER for word in words
    ):
        return None, True, stream
    return location, online or bool(_HYBRID_WORDS.intersection(words)), stream


def _times(
    draft: EventDraft, source: SourceConfig, context: NormalizeContext, *, all_day: bool = False
) -> EventTimes:
    """The times of ``draft``; with ``all_day`` read as all day whatever the draft says."""
    try:
        return resolve_times(
            draft.start,
            draft.end,
            all_day=all_day or draft.all_day,
            source_zone=draft.timezone or source.timezone,
            project_zone=context.settings.timezone,
            reference=context.now,
        )
    except TimeError as error:
        raise _Invalid(f"start: {error}") from error


def _listed_end(
    draft: EventDraft, source: SourceConfig, context: NormalizeContext
) -> datetime | None:
    """The end of ``draft`` as a moment in the zone of the project, read as a start would be.

    The times of an event drop an end that breaks the length rule; the
    hours of a meeting over its days are told by it (``meeting_over_days``).
    """
    if draft.end is None:
        return None
    end = draft.model_copy(update={"start": draft.end, "end": None})
    try:
        return _times(end, source, context).start
    except _Invalid:
        return None


def _labels(labels: Iterable[str], cleaner: _Cleaner) -> list[str]:
    """Categories or tags of a draft: cleaned, without empty ones, at most ``MAX_CATEGORIES``."""
    cleaned = (cleaner.line(label, MAX_CATEGORY) for label in labels)
    return [label for label in cleaned if label][:MAX_CATEGORIES]


def _event(
    draft: EventDraft,
    source: SourceConfig,
    times: EventTimes,
    event_type: EventType,
    meeting: bool,
    shared: frozenset[str],
    cleaner: _Cleaner,
) -> Event:
    context = cleaner.context
    title, status = _title_and_status(draft, cleaner)
    named = merge_speakers([*_structured_speakers(draft, cleaner), *_line_speakers(draft, cleaner)])
    abstract = _abstract(draft, cleaner)
    speakers = named or merge_speakers(_title_speakers(title, abstract))
    location, is_online, stream = _place(draft, cleaner)
    own_page = cleaner.url(draft.url)
    url = own_page or cleaner.url(source.homepage) or cleaner.url(source.all_urls[0])
    if url is None:
        raise _Invalid("url: neither the draft nor its source has an address that can be shown")
    key = draft_key(
        draft if own_page is not None else draft.evolve(url=None),
        start=times.start,
        speaker=named[0].name if named else None,
        title=None if title == TBA_TITLE else title,
        shared=shared,
        is_secret=context.is_secret,
        meeting=meeting,
    )
    seen = context.now.astimezone(UTC)
    return Event(
        uid=resolve_uid(key, context.uid_map),
        title=title,
        speakers=speakers,
        series=cleaner.title(draft.series, MAX_SERIES) or cleaner.title(source.series, MAX_SERIES),
        host_org=cleaner.line(draft.host_org, MAX_HOST) or source.host_org,
        institution=source.institution,
        event_type=event_type,
        start=times.start,
        end=times.end,
        all_day=times.all_day,
        location=location,
        is_online=is_online,
        stream_url=stream,
        abstract=abstract,
        url=url,
        source_urls=[url],
        source_ids=[key],
        fields=[],
        field_tag_method="source",
        status=status,
        extraction=draft.extraction,
        first_seen=seen,
        last_changed=seen,
    )


def _describe(error: ValidationError) -> str:
    places = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors()})
    return "not a valid event: " + ", ".join(places or ["(event)"])


def _invalid(
    draft: EventDraft, source: SourceConfig, reason: str, cleaner: _Cleaner
) -> ExcludedEvent:
    shown = cleaner.url(draft.url)
    return ExcludedEvent(
        source_ids=[source.id],
        title=cleaner.title(draft.title, MAX_TITLE) or UNTITLED,
        url=str(shown) if shown is not None else None,
        reason="invalid",
        detail=reason,
    )


def _log_dropped_ends(source_id: str, uids: Sequence[str]) -> None:
    shown = ", ".join(uids[:MAX_LOGGED_UIDS]) + (", ..." if len(uids) > MAX_LOGGED_UIDS else "")
    logger.warning(
        "%s: the end of %d timed event(s) is not kept (the same time on a later day, or more"
        " than %d days after the start): %s",
        source_id,
        len(uids),
        MAX_TIMED_LENGTH.days,
        shown,
    )


def normalize_drafts(
    drafts: Sequence[EventDraft], source: SourceConfig, context: NormalizeContext
) -> Normalized:
    """Turn the drafts of ``source`` into candidates (see the module docstring).

    The candidates come back in the order of their drafts. Their events
    have no fields yet and carry the time of the run as ``first_seen`` and
    ``last_changed``; later stages replace both with what is stored.
    """
    cleaner = _Cleaner(context)
    shared = shared_urls(drafts, source)
    meeting_window = context.settings.meeting_window_at(context.now)
    candidates: list[Candidate] = []
    invalid: list[ExcludedEvent] = []
    outside = 0
    without_end: list[str] = []
    for draft in drafts:
        try:
            if draft.source_id != source.id:
                raise _Invalid("source_id: the draft belongs to another source")
            times = _times(draft, source, context)
            stated = draft.event_type_hint or source.default_event_type
            event_type = meeting_type(stated, draft.title, times)
            if times.end_dropped_as_too_long and (end := _listed_end(draft, source, context)):
                days = _times(draft, source, context, all_day=True)
                typed = meeting_over_days(stated, draft.title, days, start=times.start, end=end)
                if typed is not None:
                    times, event_type = days, typed
            meeting = is_meeting_span(event_type, times.start, times.end, all_day=times.all_day)
            if not in_window(
                times,
                meeting=meeting,
                source=source,
                window=context.window,
                meeting_window=meeting_window,
            ):
                outside += 1
                continue
            event = _event(draft, source, times, event_type, meeting, shared, cleaner)
        except _Invalid as error:
            invalid.append(_invalid(draft, source, str(error), cleaner))
            continue
        except ValidationError as error:
            invalid.append(_invalid(draft, source, _describe(error), cleaner))
            continue
        if times.end_dropped_as_too_long:
            without_end.append(event.uid)
        hints = EventHints(
            source_ids=[source.id],
            default_fields=source.default_fields,
            field_hints=draft.field_hints,
            categories=_labels(draft.categories, cleaner),
            tags=_labels(draft.tags, cleaner),
        )
        candidates.append(Candidate(event=event, hints=hints))
    if without_end:
        _log_dropped_ends(source.id, without_end)
    return Normalized(
        candidates=tuple(candidates),
        invalid=tuple(invalid),
        outside_window=outside,
        redactions=cleaner.redactions,
    )
