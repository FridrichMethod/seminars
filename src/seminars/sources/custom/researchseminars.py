"""Custom adapter ``researchseminars``: the series feeds of researchseminars.org.

A source names the ICS feed of one series (``/seminar/<series id>/ics``).
``fetch`` requests it (``fetch_urls``); the feed has no pages and no date
parameter, it holds the whole history of the series. ``parse`` reads it
with the public helpers of the generic ``ics`` adapter (``read_calendar``,
``occurrences``, ``occurrence_draft``), which give the native id, the times
and the all-day convention, and replaces the fields that this site fills in
its own way:

* ``UID`` is ``<series id>/<talk counter>`` and the native id. The talk
  page, ``url``, is built from it: ``/talk/<series id>/<counter>/`` on the
  host of the feed. A VEVENT whose UID is not of that form for the
  configured series is skipped; a feed in which no VEVENT has such a UID
  is a ``ParseError`` (the layout changed).
* ``SUMMARY`` is the speaker, not the title. ``DESCRIPTION`` is read line
  by line, by position::

      Title: <a href="TALK PAGE">TITLE</a>     (missing while the title is TBA)
      by SPEAKERS as part of SERIES
      <empty line>
      Interactive livestream: URL              (optional)
      Password hint: TEXT                      (optional)
      View-only livestream: URL                (optional)
      Lecture held in ROOM.                    (optional)
      then "Abstract" on a line of its own and the abstract below it,
      or "Abstract: TBA"

  The title is the text of the link on the ``Title:`` line (parsed as
  HTML; nothing else of the description is markup). The by-line is the
  first line, or the second after a ``Title:`` line, never a later line
  that starts with "by " (abstracts have such lines). SPEAKERS is one or
  more ``Name (Affiliation)``; see ``parse_speakers``. Without a by-line the
  speakers come from ``SUMMARY``. SERIES becomes the draft's ``series``.
  The room of ``Lecture held in`` is the ``location`` without the final
  period; "TBA" means none. Everything after the ``Abstract`` line is the
  abstract, comments of the organizers included (they cannot be told
  apart); "Abstract: TBA" is no abstract. Other lines before the abstract
  are ignored.
* ``LOCATION`` is never read: it holds the talk page today, and in the
  site's current code it holds the livestream link with its ``pwd=``. The
  ``URL`` properties, which repeat the livestream links, are not read
  either.
* A livestream line makes the talk online (``is_online``). The
  ``Interactive livestream`` (the meeting link, with its password) and the
  ``Password hint`` are never copied; the ``View-only livestream`` becomes
  ``stream_url``. Without such a line ``is_online`` stays unknown.
* ``DTSTART`` / ``DTEND`` are UTC date-times; a ``VALUE=DATE`` value is an
  all-day event whose ``DTEND`` is the day after the last day (the ``ics``
  helpers subtract one day). Talks are returned whatever their date; the
  pipeline applies the run window.

Parameters (``params``; an unknown key is an error that names it,
``extra_hosts`` is read by the shared host check):

* ``series_id`` (required): the series id on the site, the first part of
  every UID of the feed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Annotated, Final

from pydantic import StringConstraints
from selectolax.lexbor import LexborHTMLParser

from seminars.httpbase import HttpClient
from seminars.models import (
    EventDraft,
    FetchedDocument,
    Model,
    RawPayload,
    SourceConfig,
    SpeakerDraft,
    TimeWindow,
)
from seminars.sources.base import ParseError, absolute_url, fetch_urls, register_adapter
from seminars.sources.generic.html_list import collapse, is_tba, load_params, node_text
from seminars.sources.generic.ics import (
    IcsOccurrence,
    occurrence_draft,
    occurrences,
    property_texts,
    read_calendar,
)

logger = logging.getLogger(__name__)

SERIES_ID_PATTERN: Final = r"^[A-Za-z0-9_.-]{1,100}$"
_UID: Final = re.compile(r"(?P<series>[A-Za-z0-9_.-]{1,100})/(?P<counter>[0-9]{1,9})")

TITLE_PREFIX: Final = "Title:"
BY_PREFIX: Final = "by "
SERIES_SEPARATOR: Final = " as part of "
ROOM_PREFIX: Final = "Lecture held in "
INTERACTIVE_PREFIX: Final = "Interactive livestream:"
VIEW_ONLY_PREFIX: Final = "View-only livestream:"
ABSTRACT_LINE: Final = "Abstract"
ABSTRACT_PREFIX: Final = "Abstract:"
_LIST_WORDS: Final = (" and ", " & ")


class ResearchSeminarsParams(Model):
    """``params`` of a ``researchseminars`` source; see the module docstring."""

    series_id: Annotated[str, StringConstraints(pattern=SERIES_ID_PATTERN)]


# --- speakers ------------------------------------------------------------------------------


def _list_word_at(text: str, index: int) -> int:
    """Length of the " and " / " & " that starts at ``index`` (any case), else 0."""
    for word in _LIST_WORDS:
        if text[index : index + len(word)].lower() == word:
            return len(word)
    return 0


def _top_level_parts(text: str, *, commas: bool) -> list[str]:
    """``text`` split at " and " / " & " (and commas) outside parentheses."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    index = 0
    while index < len(text):
        character = text[index]
        if depth == 0:
            width = 1 if commas and character == "," else _list_word_at(text, index)
            if width:
                parts.append("".join(current))
                current = []
                index += width
                continue
        if character == "(":
            depth += 1
        elif character == ")" and depth:
            depth -= 1
        current.append(character)
        index += 1
    parts.append("".join(current))
    return [part for part in (collapse(item) for item in parts) if part]


def _with_affiliation(part: str) -> tuple[str, str | None]:
    """``Name (Affiliation)`` as its two halves; a part without one is a name only."""
    if not part.endswith(")"):
        return part, None
    depth = 0
    for index in range(len(part) - 1, -1, -1):
        if part[index] == ")":
            depth += 1
        elif part[index] == "(":
            depth -= 1
            if depth == 0:
                name, affiliation = collapse(part[:index]), collapse(part[index + 1 : -1])
                return (name, affiliation) if name else (part, None)
    return part, None


def parse_speakers(text: str | None) -> list[SpeakerDraft]:
    """The speakers of a by-line or a SUMMARY: ``Name (Affiliation)``, possibly several.

    An affiliation is the text in the parentheses at the end of a name; it
    may contain commas and "and" ("Princeton and IAS", "Berkeley, Clay
    Mathematical Institute"). Speakers are separated by " and " or " & "
    outside parentheses, and by commas only in a list that also has one of
    those ("A, B, and C"): a comma alone does not split ("Jane Doe, Jr.").
    """
    value = collapse(text)
    if value is None:
        return []
    commas = len(_top_level_parts(value, commas=False)) > 1
    speakers: list[SpeakerDraft] = []
    for part in _top_level_parts(value, commas=commas):
        name, affiliation = _with_affiliation(part)
        if name:
            speakers.append(SpeakerDraft(name=name, affiliation=affiliation))
    return speakers


# --- the description -----------------------------------------------------------------------


@dataclass(frozen=True)
class Description:
    """What the DESCRIPTION of one talk says, in draft terms."""

    title: str | None = None
    speakers_text: str | None = None
    series: str | None = None
    location: str | None = None
    online: bool = False
    view_only_link: str | None = None
    abstract: str | None = None


def _title(line: str) -> str | None:
    """The text of the link on a ``Title:`` line (the line without its prefix)."""
    tree = LexborHTMLParser(line)
    link = tree.css_first("a")
    return node_text(link) if link is not None else node_text(tree.body)


def _by_line(line: str) -> tuple[str | None, str | None]:
    """``(speakers, series)`` of a by-line: ``by SPEAKERS as part of SERIES``."""
    body = line[len(BY_PREFIX) :]
    speakers, separator, series = body.rpartition(SERIES_SEPARATOR)
    if not separator:
        return collapse(body), None
    return collapse(speakers), collapse(series)


def _room(line: str) -> str | None:
    room = collapse(line[len(ROOM_PREFIX) :])
    if room is not None and room.endswith("."):
        room = collapse(room[:-1])
    return None if room is None or is_tba(room) else room


def _abstract(first: str | None, rest: list[str]) -> str | None:
    lines = ([first] if first else []) + rest
    return "\n".join(lines).strip() or None


def read_description(text: str | None) -> Description:
    """Read the DESCRIPTION of a talk; see the module docstring for the layout."""
    lines = (text or "").split("\n")
    title = None
    index = 0
    if lines[0].startswith(TITLE_PREFIX):
        title = _title(lines[0][len(TITLE_PREFIX) :])
        index = 1
    speakers = series = None
    if index < len(lines) and lines[index].startswith(BY_PREFIX):
        speakers, series = _by_line(lines[index])
        index += 1
    location = view_only = abstract = None
    online = False
    for position in range(index, len(lines)):
        line = lines[position].strip()
        rest = lines[position + 1 :]
        if line == ABSTRACT_LINE:
            abstract = _abstract(None, rest)
            break
        if line.startswith(ABSTRACT_PREFIX):
            first = collapse(line[len(ABSTRACT_PREFIX) :])
            abstract = _abstract(None if is_tba(first) else first, rest)
            break
        if line.startswith(ROOM_PREFIX):
            location = _room(line)
        elif line.startswith(INTERACTIVE_PREFIX):
            online = True
        elif line.startswith(VIEW_ONLY_PREFIX):
            online = True
            view_only = collapse(line[len(VIEW_ONLY_PREFIX) :])
    return Description(
        title=title,
        speakers_text=speakers,
        series=series,
        location=location,
        online=online,
        view_only_link=view_only,
        abstract=abstract,
    )


# --- drafts --------------------------------------------------------------------------------


def talk_counter(uid: str | None, series_id: str) -> str | None:
    """The talk counter of ``uid`` when it is ``<series_id>/<counter>``, else ``None``."""
    match = _UID.fullmatch(uid or "")
    if match is None or match["series"] != series_id:
        return None
    return match["counter"]


def talk_draft(
    occurrence: IcsOccurrence,
    source: SourceConfig,
    params: ResearchSeminarsParams,
    *,
    base_url: str,
    calendar_timezone: str | None = None,
) -> EventDraft | None:
    """The draft of one talk; ``None`` when its UID is not a talk of the series.

    Raises ``ValueError`` (pydantic's ``ValidationError`` included) when the
    talk cannot be a draft.
    """
    vevent = occurrence.vevent
    uids = property_texts(vevent, "UID")
    counter = talk_counter(uids[0] if uids else None, params.series_id)
    if counter is None:
        return None
    common = occurrence_draft(
        occurrence, source, base_url=base_url, calendar_timezone=calendar_timezone
    )
    descriptions = property_texts(vevent, "DESCRIPTION")
    description = read_description(descriptions[0] if descriptions else None)
    summaries = property_texts(vevent, "SUMMARY")
    speakers = parse_speakers(description.speakers_text) or parse_speakers(
        summaries[0] if summaries else None
    )
    return common.evolve(
        url=absolute_url(base_url, f"/talk/{params.series_id}/{counter}/"),
        title=description.title,
        speakers=speakers,
        speaker_text=None,
        series=description.series,
        location=description.location,
        is_online=True if description.online else None,
        stream_url=absolute_url(base_url, description.view_only_link),
        abstract=description.abstract,
        abstract_html=None,
    )


def _feed_drafts(
    document: FetchedDocument,
    source: SourceConfig,
    params: ResearchSeminarsParams,
    window: TimeWindow,
) -> list[EventDraft]:
    try:
        calendar = read_calendar(document)
        found = occurrences(calendar, window, timezone=calendar.timezone or source.timezone)
    except ParseError as error:
        raise ParseError(f"source {source.id}: {error}") from error
    drafts: list[EventDraft] = []
    foreign = malformed = 0
    for occurrence in found:
        try:
            draft = talk_draft(
                occurrence,
                source,
                params,
                base_url=document.final_url,
                calendar_timezone=calendar.timezone,
            )
        except (ValueError, TypeError):  # pydantic's ValidationError is a ValueError
            malformed += 1
            continue
        if draft is None:
            foreign += 1
        else:
            drafts.append(draft)
    if calendar.vevents and foreign and not drafts and not malformed:
        raise ParseError(
            f"source {source.id}: no VEVENT of the feed has a UID of the series "
            f"{params.series_id} (<series id>/<counter>)"
        )
    if foreign or malformed:
        logger.warning(
            "source %s: %d VEVENT(s) without a talk UID of the series and %d malformed "
            "one(s) skipped",
            source.id,
            foreign,
            malformed,
        )
    return drafts


@register_adapter("researchseminars")
class ResearchSeminarsAdapter:
    """Reads the series feeds of researchseminars.org; see the module docstring."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        load_params(ResearchSeminarsParams, source)
        return fetch_urls(http, source, window=window)

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        params = load_params(ResearchSeminarsParams, source)
        drafts: list[EventDraft] = []
        seen: set[str | None] = set()
        for document in payload.documents:
            for draft in _feed_drafts(document, source, params, payload.window):
                if draft.native_id not in seen:  # every talk has one: its UID
                    seen.add(draft.native_id)
                    drafts.append(draft)
        return drafts
