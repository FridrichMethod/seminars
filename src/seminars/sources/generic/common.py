"""Helpers that the page and feed adapters share.

The generic adapters ``html_list``, ``rss`` and ``jsonld``, the source probe
and the custom adapters read pages the same way, so the pieces live here:

* params: ``load_params`` validates ``source.params`` against a model and
  raises ``AdapterConfigError`` naming every bad key; ``Selector`` is a CSS
  selector checked by the parser that uses it, ``Pattern`` a compiled
  regular expression from the YAML, and ``require_groups`` checks that a
  pattern has the named groups an adapter reads;
* text of HTML nodes (``node_text``, ``node_lines``, ``node_value``,
  ``inner_html``) and patterns applied to at most ``PATTERN_INPUT_LIMIT``
  characters (``search``, ``any_match``);
* lists of speaker names (``split_names``, ``make_speakers``) and the
  affiliations of speakers in the lines of a page (``text_lines``,
  ``with_affiliations``, below), and speakers named after a label in them
  (``speakers_after_label``);
* dates and times: times of day and their ranges (``parse_clock``,
  ``split_range``, ``time_range``), days without a year (``parse_day``,
  closest to the fetch), ISO 8601 moments, English month names
  (``month_number``) and the window test (``outside_window``);
* fetching that stays on the source's hosts (``get_document``,
  ``fetch_optional``).

Affiliations from the text. The lines of a part of a page (``text_lines``)
end at ``<br>`` and at the start and the end of a nested block element.
The line of a speaker holds their name (the first and the last word of it,
at most two words between them, such as a middle initial) and at most four
other words ("Prof. Dr.", "PhD"), so a sentence that mentions the speaker
is not it. Up to three lines below it are the affiliation, joined with ", ",
ending before an empty line, a weekday ("Monday," "Thu."), a month and a day
("December 3"), a line that starts with a digit (a date or a time), a label
("Talk Title:", "Host:"), a heading of at most four words with a word such
as "Abstract", "Bio" or "Talk" (a whole word: "Stanford Bio-X" is no
heading), prose (more than 15 words, or a lowercase word and a period at
the end), an e-mail address or a link, or a line that names the speaker
again or another speaker of the talk (the next one of an announcement of
several). The first line that names the speaker decides; when the line
right after it names them too (a heading "Seminar: Jane Doe" above "Jane
Doe, PhD"), that line is the line of the name. Only as many speakers of a
talk as the pipeline keeps are looked for (the first
``seminars.pipeline.normalize.MAX_SPEAKERS``).
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, time
from typing import Annotated, Any, Final
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser
from pydantic import AfterValidator, ValidationError
from selectolax.lexbor import LexborHTMLParser, LexborNode, SelectolaxError

from seminars.contracts import PrivateTextScrubber, load_implementation
from seminars.http import HttpClient, HttpError
from seminars.models import (
    FetchedDocument,
    Model,
    SourceConfig,
    SpeakerDraft,
    TimeWindow,
)
from seminars.pipeline.normalize import MAX_SPEAKERS
from seminars.sources.base import AdapterError, ForeignHostError, on_source_host
from seminars.textrules import ascii_printable

logger = logging.getLogger(__name__)

MONTHS: Final = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
"""English month names by their first three letters, January first."""

PATTERN_INPUT_LIMIT: Final = 2000
"""Characters of a page text that a pattern from the YAML is applied to; bounds the work."""

# --- params ------------------------------------------------------------------------------


class AdapterConfigError(AdapterError, ValueError):
    """The ``params`` of a source do not fit its adapter: an unknown key or a wrong value."""


def _describe(problem: Mapping[str, Any]) -> str:
    location = ".".join(["params", *(str(part) for part in problem["loc"])])
    detail = "unknown key" if problem["type"] == "extra_forbidden" else problem["msg"]
    return f"{location}: {detail}"


def load_params[P: Model](model: type[P], source: SourceConfig) -> P:
    """``source.params`` validated as ``model``; ``extra_hosts`` is left to the host check."""
    data = {key: value for key, value in source.params.items() if key != "extra_hosts"}
    try:
        return model.model_validate(data)
    except ValidationError as error:
        problems = "; ".join(_describe(problem) for problem in error.errors())
        message = f"source {source.id}: params of adapter {source.adapter}: {problems}"
        raise AdapterConfigError(ascii_printable(message)) from None


def check_selector(value: str) -> str:
    """Validate a CSS selector with the parser that will use it."""
    try:
        LexborHTMLParser("<html><body></body></html>").css(value)
    except (SelectolaxError, ValueError) as error:
        raise ValueError(f"invalid CSS selector {value!r}") from error
    return value


Selector = Annotated[str, AfterValidator(check_selector)]
Pattern = re.Pattern[str]


# --- text helpers ------------------------------------------------------------------------


def collapse(text: str | None) -> str | None:
    """Runs of whitespace as one space, stripped; ``None`` for empty text."""
    return None if text is None else " ".join(text.split()) or None


def node_text(node: LexborNode | None) -> str | None:
    """All text below ``node``, whitespace collapsed."""
    return None if node is None else collapse(node.text(deep=True, strip=False))


def node_lines(node: LexborNode | None) -> str | None:
    """Text below ``node`` with every ``<br>`` as a line break, lines joined with ", "."""
    if node is None:
        return None
    lines: list[list[str]] = [[]]
    for child in node.traverse(include_text=True):
        if child.tag == "br":
            lines.append([])
        elif child.is_text_node:
            lines[-1].append(child.text_content or "")
    parts = [collapse("".join(line)) for line in lines]
    return ", ".join(part for part in parts if part) or None


def node_value(node: LexborNode | None, attribute: str | None) -> str | None:
    """The attribute ``attribute`` of ``node`` (collapsed), or its text without one."""
    if node is None or attribute is None:
        return node_text(node)
    return collapse(node.attributes.get(attribute))


def inner_html(node: LexborNode | None) -> str | None:
    """The markup inside ``node`` without ``<summary>`` captions; ``None`` when blank."""
    if node is None:
        return None
    copy = node.clone()
    for caption in copy.css("summary"):
        caption.decompose()
    markup = copy.inner_html
    return markup if isinstance(markup, str) and markup.strip() else None


def search(pattern: Pattern, text: str) -> re.Match[str] | None:
    """``pattern.search`` over at most PATTERN_INPUT_LIMIT characters of ``text``."""
    return pattern.search(text[:PATTERN_INPUT_LIMIT])


def any_match(patterns: Sequence[Pattern], text: str | None) -> bool:
    return text is not None and any(search(pattern, text) for pattern in patterns)


_NAME_LIST: Final = re.compile(r"\s(?:and|&)\s", re.IGNORECASE)
_NAME_SEPARATOR: Final = re.compile(r"\s*,\s*(?:(?:and|&)\s+)?|\s+(?:and|&)\s+", re.IGNORECASE)


def split_names(text: str | None) -> list[str]:
    """Names of a list such as "A and B" or "A, B, and C"; one name otherwise.

    A comma alone does not split ("Jane Doe, Jr."); it does in a list that
    also has "and" or "&" between two names. At most PATTERN_INPUT_LIMIT
    characters of ``text`` are read, so that many talks that share one long
    text (a speaker line of a page they all link to) each cost little.
    """
    value = collapse(None if text is None else text[:PATTERN_INPUT_LIMIT])
    if value is None:
        return []
    if not _NAME_LIST.search(value[:PATTERN_INPUT_LIMIT]):
        return [value]
    names = (collapse(part) for part in _NAME_SEPARATOR.split(value[:PATTERN_INPUT_LIMIT]))
    return [name for name in names if name]


def make_speakers(names: str | None, affiliation: str | None) -> list[SpeakerDraft]:
    """One speaker per name of ``names``, all with ``affiliation``."""
    return [
        SpeakerDraft(name=name, affiliation=collapse(affiliation)) for name in split_names(names)
    ]


# --- affiliations from the text ---------------------------------------------------------

_BLOCK_TAGS: Final = frozenset(
    {
        *("address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption"),
        *("footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "ol", "p"),
        *("section", "table", "td", "th", "tr", "ul"),
    }
)
MAX_AFFILIATION_LINES: Final = 3
MAX_AFFILIATION_WORDS: Final = 15  # a longer line is prose: a bio, an abstract
MAX_HEADING_WORDS: Final = 4
MAX_OTHER_WORDS: Final = 4  # on the line of a name: titles and degrees ("Prof. Dr.", "PhD")
_WEEKDAYS: Final = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_NO_AFFILIATION: Final = re.compile(
    r"\s*(?:\d"  # a date or a time: "12/3/26", "11:00 AM"
    rf"|(?:{_WEEKDAYS})\b|(?:mon|tues?|wed|thu|thurs|fri|sat|sun)[.,]"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d"
    r"|[^\W\d][^:]{0,40}:(?:\s|$))",  # a label: "Talk Title:", "Host:"
    re.IGNORECASE,
)
_HEADING_WORD: Final = re.compile(  # a whole word: "Bio", not the start of "Bio-X"
    r"(?<![\w-])(?:abstract|about|bio|biography|biosketch|description|hosts?|hosted|location"
    r"|overview|register|registration|rsvp|summary|talk|title|venue|when|where)(?![\w-])",
    re.IGNORECASE,
)
_SENTENCE_END: Final = re.compile(r"(?:\b[a-z]{2,}[.!?]|\.\.\.)$")  # "We study things."
_CONTACT: Final = ("@", "://", "www.")  # an e-mail address or a link


def text_lines(node: LexborNode) -> list[str]:
    """The non-empty lines below ``node``.

    ``<br>`` and the start of a block element end a line, and so does the end
    of one: text after a nested block is on a line of its own. The walk is in
    document order, so the parent of a node comes before it: the innermost
    block around every element (``node`` at most) is known when its children
    come, and each node is looked at once.
    """
    top = node.mem_id
    blocks: dict[int, int] = {top: top}  # mem_id of an element -> of the block around it
    lines: list[list[str]] = [[]]
    block = top
    for child in node.traverse(include_text=True):
        parent = child.parent
        around = blocks.get(parent.mem_id, top) if parent is not None else top
        if child.is_text_node:
            if around != block:
                lines.append([])
                block = around
            lines[-1].append(child.text_content or "")
        elif child.tag in _BLOCK_TAGS:
            blocks[child.mem_id] = child.mem_id
            lines.append([])
        else:
            blocks[child.mem_id] = around
    return [text for line in lines if (text := collapse("".join(line)))]


def name_pattern(name: str) -> re.Pattern[str] | None:
    """How a line names ``name``: its first and last word, at most two words between."""
    words = name.split()
    if not words:
        return None
    middle = r"(?:\s+\S+){0,2}?\s+" + re.escape(words[-1]) if len(words) > 1 else ""
    return re.compile(rf"(?<!\w){re.escape(words[0])}{middle}(?!\w)", re.IGNORECASE)


def _name_in(line: str, name: str) -> re.Match[str] | None:
    """Where ``line`` names ``name`` (see ``name_pattern``)."""
    pattern = name_pattern(name)
    return None if pattern is None else pattern.search(line[:PATTERN_INPUT_LIMIT])


def names_line(line: str, name: str) -> bool:
    """True when ``line`` is the line of ``name``: the name and at most a few other words."""
    match = _name_in(line, name)
    if match is None:
        return False
    rest = line[: match.start()] + " " + line[match.end() :]
    return len(re.findall(r"\w+", rest)) <= MAX_OTHER_WORDS


def ends_affiliation(line: str, names: Sequence[re.Pattern[str]]) -> bool:
    """True when ``line`` cannot be part of an affiliation of the speakers ``names``.

    That is an empty line, a date, a time, a label, a heading ("Abstract",
    "Speaker Bio"), prose (a sentence, a long line), an e-mail address or a
    link, and a line that names one of the speakers (the same one again, or
    another one); ``names`` are their ``name_pattern``.
    """
    words = line.split()
    return (
        not words
        or _NO_AFFILIATION.match(line) is not None
        or (len(words) <= MAX_HEADING_WORDS and _HEADING_WORD.search(line) is not None)
        or len(words) > MAX_AFFILIATION_WORDS
        or _SENTENCE_END.search(line) is not None
        or any(mark in line for mark in _CONTACT)
        or any(name.search(line[:PATTERN_INPUT_LIMIT]) is not None for name in names)
    )


def affiliation_after(
    lines: Sequence[str], name: str, names: Sequence[re.Pattern[str]] = ()
) -> str | None:
    """The lines below the line that names ``name``; see the module docstring.

    ``names`` are the ``name_pattern`` of the speakers of the talk: a line
    that names one of them ends the affiliation, like a line that names
    ``name`` again.
    """
    own = name_pattern(name)
    if own is None:
        return None
    stops = (own, *names)
    found: list[str] | None = None  # None until a line names the speaker
    for line in lines:
        if not found and names_line(line, name):
            found = []  # the first line of the name, or the next one right after it
        elif found is not None:
            if len(found) == MAX_AFFILIATION_LINES or ends_affiliation(line, stops):
                break
            found.append(line)
    return ", ".join(found) if found else None


def with_affiliations(speakers: Sequence[SpeakerDraft], lines: Sequence[str]) -> list[SpeakerDraft]:
    """``speakers``, each without an affiliation given the one the text has for them.

    Only the first ``MAX_SPEAKERS`` are looked for: the pipeline keeps no
    more speakers of a talk, and a page that names thousands would otherwise
    cost thousands of walks through its text. The others are kept as they
    are. The names of all of them still end an affiliation, each compiled
    once.
    """
    if not lines:
        return list(speakers)
    names = [pattern for speaker in speakers if (pattern := name_pattern(speaker.name))]
    completed: list[SpeakerDraft] = []
    for speaker in speakers[:MAX_SPEAKERS]:
        found = None if speaker.affiliation else affiliation_after(lines, speaker.name, names)
        completed.append(speaker.evolve(affiliation=found) if found else speaker)
    return [*completed, *speakers[MAX_SPEAKERS:]]


def speakers_after_label(
    lines: Sequence[str], label: Pattern, pattern: Pattern | None
) -> list[SpeakerDraft]:
    """The speakers named right after the first of ``lines`` that ``label`` matches.

    They are in the rest of that line, or else in the next line: the groups
    ``speaker`` and ``affiliation`` of ``pattern`` there (no match: nobody),
    or without a pattern the whole text as a list of names. A speaker
    without an affiliation takes the lines below the line of their name
    (``with_affiliations``).
    """
    for index, line in enumerate(lines):
        found = search(label, line)
        if found is None:
            continue
        rest = collapse(line[found.end() :])
        following = lines[index:] if rest else lines[index + 1 :]
        text = rest or (following[0] if following else None)
        if pattern is None:
            return with_affiliations(make_speakers(text, None), following)
        match = search(pattern, text or "")
        if match is None:
            return []
        groups = match.groupdict()
        speakers = make_speakers(groups.get("speaker"), groups.get("affiliation"))
        return with_affiliations(speakers, following)
    return []


# --- dates and times ---------------------------------------------------------------------

_CLOCK: Final = re.compile(
    r"(?P<hour>\d{1,2})(?:[:.h](?P<minute>\d{2}))?\s*(?:(?P<meridiem>[ap])\.?\s*m\.?)?",
    re.IGNORECASE,
)
_ZONE_SUFFIX: Final = re.compile(r"\s*\(?\b(?!am\b|pm\b|noon\b)[a-z]{2,4}\)?\s*$", re.IGNORECASE)
_RANGE: Final = re.compile(r"\s*[-\u2013\u2014]\s*|\s+to\s+", re.IGNORECASE)
_TBA: Final = re.compile(r"\W*(?:tba|tbd|tbc|to be (?:announced|determined|confirmed))\W*", re.I)
_NAMED_TIMES: Final = {"noon": time(12, 0), "midnight": time(0, 0)}
_NOON: Final = re.compile(r"12(?:[:.h]00)?\s*noon", re.IGNORECASE)


def is_tba(text: str | None) -> bool:
    """True for "TBA", "TBD", "to be announced" and the like."""
    return text is not None and _TBA.fullmatch(text[:PATTERN_INPUT_LIMIT]) is not None


def with_meridiem(value: time, meridiem: str) -> time:
    """``value`` read on a 12-hour clock with ``meridiem`` ("am" / "pm", any case)."""
    hour = value.hour % 12 + (12 if meridiem.lower().startswith("p") else 0)
    return value.replace(hour=hour)


def parse_clock(text: str | None, fmt: str | None = None) -> tuple[time, str | None] | None:
    """A time of day and its meridiem ("am" / "pm", ``None`` when not written)."""
    value = collapse(text)
    if value is None or is_tba(value):
        return None
    if fmt is not None:
        try:
            parsed = datetime.strptime(value, fmt).time()
        except ValueError:
            return None
        return parsed, ("pm" if parsed.hour >= 12 else "am") if "%p" in fmt else None
    value = _ZONE_SUFFIX.sub("", value[:PATTERN_INPUT_LIMIT])
    if value.lower() in _NAMED_TIMES:
        return _NAMED_TIMES[value.lower()], "pm" if value.lower() == "noon" else "am"
    if _NOON.fullmatch(value):  # "12:00 noon"
        return time(12, 0), "pm"
    match = _CLOCK.fullmatch(value)
    if match is None:
        return None
    hour, minute = int(match["hour"]), int(match["minute"] or 0)
    meridiem = match["meridiem"]
    if hour > 23 or minute > 59 or (meridiem is not None and not 1 <= hour <= 12):
        return None
    clock = time(hour, minute)
    if meridiem is None:
        return clock, None
    return with_meridiem(clock, meridiem), f"{meridiem.lower()}m"


def split_range(text: str | None) -> tuple[str | None, str | None]:
    """ "4:30 - 5:30pm" as its two ends; a single time as ``(time, None)``."""
    value = collapse(text)
    if value is None:
        return None, None
    parts = _RANGE.split(value[:PATTERN_INPUT_LIMIT], maxsplit=1)
    return parts[0], parts[1] if len(parts) > 1 else None


def time_range(
    start: str | None, end: str | None, *, meridiem: str | None = None, fmt: str | None = None
) -> tuple[time | None, time | None]:
    """Start and end times of day; a meridiem written on one end completes the other.

    When only the end says AM or PM (or ``meridiem`` does) the start gets the
    same one, unless that would put it after the end: "11:30 - 1:00 PM"
    starts at 11:30 in the morning. When only the start says it (noon is PM),
    an end before the start is read as PM if that puts it after the start:
    "11am - 1:30" ends at 13:30, "4:30 PM - 3:30" keeps 3:30.
    """
    first, second = parse_clock(start, fmt), parse_clock(end, fmt)
    shared = meridiem or (second[1] if second else None)
    end_time = None
    if second is not None:
        end_time = second[0] if second[1] or shared is None else with_meridiem(second[0], shared)
    if first is None:
        return None, end_time
    start_time = first[0]
    if first[1] is None and shared is not None and start_time.hour <= 12:
        start_time = with_meridiem(first[0], shared)
        if end_time is not None and start_time > end_time:
            start_time = with_meridiem(first[0], "am")
    elif first[1] and shared is None and end_time is not None and end_time < start_time:
        afternoon = with_meridiem(end_time, "pm")
        end_time = afternoon if end_time.hour and afternoon > start_time else end_time
    return start_time, end_time


def parse_day(text: str | None, fmt: str | None, reference: date) -> date | None:
    """A calendar date from ``text``; see the module docstring for the rules."""
    value = collapse(text)
    if value is None or is_tba(value):
        return None
    if fmt is None:
        if len(value) == 10:
            with contextlib.suppress(ValueError):
                return date.fromisoformat(value)
        text = value[:PATTERN_INPUT_LIMIT]
        return closest_day(
            lambda year: date_parser.parse(text, default=datetime(year, 1, 1)).date(), reference
        )
    if "%Y" in fmt or "%y" in fmt:
        with contextlib.suppress(ValueError):
            return datetime.strptime(value, fmt).date()
        return None
    return closest_day(
        lambda year: datetime.strptime(f"{value} {year}", f"{fmt} %Y").date(), reference
    )


def closest_day(read: Callable[[int], date], reference: date) -> date | None:
    """Of the dates ``read`` gives for the years around ``reference``, the closest one.

    A text with a year of its own gives the same date for every year; one
    without gets the year that puts it closest to ``reference``.
    """
    candidates: list[date] = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        with contextlib.suppress(ValueError, OverflowError):
            if (day := read(year)) not in candidates:
                candidates.append(day)
    return min(candidates, key=lambda day: abs(day - reference), default=None)


def parse_moment(text: str | None) -> datetime | date | None:
    """An ISO 8601 date (all day, "20261117" too) or date and time; ``None`` for anything else."""
    value = collapse(text)
    if value is None:
        return None
    for reader in (date.fromisoformat, datetime.fromisoformat):
        with contextlib.suppress(ValueError):
            return reader(value)
    return None


def local_day(moment: datetime | date, zone: ZoneInfo) -> date:
    """The local day of ``moment`` (a naive one is local; at the end of the calendar its own)."""
    if isinstance(moment, datetime) and moment.tzinfo is not None:
        with contextlib.suppress(OverflowError):
            return moment.astimezone(zone).date()
    return moment.date() if isinstance(moment, datetime) else moment


def outside_window(
    start: datetime | date | None, end: datetime | date | None, window: TimeWindow, zone: ZoneInfo
) -> bool:
    """True when a talk certainly lies outside ``window`` (unknown starts are inside)."""
    if start is None:
        return False
    first = local_day(start, zone)
    last = max(first, local_day(end, zone)) if end is not None else first
    return last < local_day(window.start, zone) or first >= local_day(window.end, zone)


# --- fetching ----------------------------------------------------------------------------


def get_document(http: HttpClient, source: SourceConfig, url: str, label: str) -> FetchedDocument:
    """``http.get(url)`` labelled; ``ForeignHostError`` before any request off the source."""
    if not on_source_host(source, url):
        raise ForeignHostError(source.id, url)
    return http.get(url).evolve(label=label)


def detail_pages_optional(source: SourceConfig) -> bool:
    """Whether ``source`` keeps a row whose detail page cannot be fetched.

    That is what ``detail_page.required: false`` of ``html_list`` says: the
    row is kept with what the list page gives, so a detail page that fails
    loses no talk (``fetch_optional``). Two settings still drop such a row,
    and then a failed page loses its talk as a required one does:
    ``keep_tags`` (the tags are on the page) and a list without
    ``selectors.date`` (the times are only on the page). No other adapter
    has that param.
    """
    params = source.params
    detail = params.get("detail_page")
    if not (isinstance(detail, Mapping) and detail.get("required") is False):
        return False
    selectors = params.get("selectors")
    dated = isinstance(selectors, Mapping) and selectors.get("date") is not None
    return dated and not params.get("keep_tags")


def fetch_optional(
    http: HttpClient, source: SourceConfig, urls: Iterable[str], label: str
) -> list[FetchedDocument]:
    """Documents of ``urls`` that could be fetched; a failure is logged and skipped.

    The pipeline sees every failed request of an adapter (it hands each
    source a client that counts them), so a source that skipped a page
    counts as not read in full: its events are used, but the run says
    nothing about the ones it did not deliver (``seminars.pipeline.collect``).
    A source whose rows do not need their detail pages
    (``detail_pages_optional``) loses no talk that way: it still counts as
    read in full, and the run reports the pages it went on without.
    The address in the logged error comes from a page, so the line is
    scrubbed first (the ``private_scrubber`` contract): a meeting password
    in a link never reaches the job log.
    """
    documents = []
    for url in urls:
        try:
            documents.append(get_document(http, source, url, label))
        except HttpError as error:
            scrub: PrivateTextScrubber = load_implementation("private_scrubber")
            logger.warning("source %s: %s page skipped: %s", source.id, label, scrub(str(error)))
    return documents


def require_groups(pattern: Pattern | None, name: str, *groups: str) -> None:
    """Raise ``ValueError`` when ``pattern`` (the param ``name``) lacks one of ``groups``."""
    if pattern is not None and (missing := [g for g in groups if g not in pattern.groupindex]):
        raise ValueError(f"{name} needs the group(s) {', '.join(missing)}")


def month_number(name: str | None) -> int | None:
    """1 to 12 for an English month name or its abbreviation ("Sept", "March")."""
    key = (name or "")[:3].lower()
    return MONTHS.index(key) + 1 if key in MONTHS else None
