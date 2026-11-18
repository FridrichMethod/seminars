"""The weekly digest: one GitHub issue with the talks of a week and the meetings ahead.

``render_digest`` turns the stored events and the run history into a
markdown document: the picks first, then one table per field, the
conferences and workshops, the deadlines of the next days, the talks that
were first seen since the previous issue, and the state of the sources.
``publish_digest`` opens the issue for the week that contains ``now`` and
closes the earlier ones.

Meetings (``Event.is_meeting``: a conference, or a workshop over several days
or of at least three hours) have a section of their own: every meeting with a
day in the MEETING_WEEKS weeks from the Monday of the digest, and every later
one first seen since the previous issue, marked as new and as a pick where it
is one. A meeting is in no other table, and the sentence that counts the
talks of the week leaves it out, so nothing is listed twice.

Deadlines (data/deadlines.json, the milestones of the registry of meetings)
have a section of their own as well: every deadline whose date lies in the
DEADLINE_DAYS days from the Monday of the digest (that Monday included), by
date, with the name of its edition and its label, the time and the zone as
the organizer's page states them, and a link to that page. Its row limit
DEADLINE_ROWS is its own and never shrinks. There is no such section when no
deadline falls in those days.

No state is kept. "New since the last issue" is whatever was first seen
after the previous digest issue was created, and a week is never posted
twice because an issue with this week's title is looked for first. Both
functions read no clock: the same arguments give the same document.

Language. The issue title is English and has to stay as it is (it is what
makes a week idempotent). Inside the body every heading, table header and
label is given in both languages, English first, and every sentence on two
lines. A table cell that holds our own words besides labels, the field
names and the picks reasons, has an English line and a Chinese line. The
picks reasons are read back from the lines the picks scorer stored
(``seminars.models.PickReason``) and said with the messages of the
locale files; the terms of the configuration in them stay as they are. All
of that text comes from the locale files; this module holds none. Event
content is not translated.

Text from the web. Titles, names, places and error messages are untrusted
and must come out as literal text: no markup, no link, no image, no heading,
and nothing GitHub would act on. Every such text goes through ``_cell``:

* it is cleaned (``seminars.textrules.clean_text``) and put on one line, so
  it cannot leave its table cell or start a block;
* ``&``, ``<`` and ``>`` become entities and the characters that mean
  something inside a line of markdown get a backslash;
* a word that GitHub would turn into something else after rendering, where
  a backslash does not help, is written as a code span: a mention
  (``@name``), a reference to an issue (``#12``), a link (``https://...``,
  ``www...``), an emoji short code (``:name:``) or a formula (``$``). Code
  is shown as it is and none of these are looked for inside it.

Untrusted text only ever stands inside a line that this module begins (a
table cell, or the name of the LLM provider at the end of a sentence), never
at the start of one, which is what makes these rules sufficient. Links are
written by this module alone, to the detail page of an event or the
organizer's page of a deadline, with the address percent-encoded inside
angle brackets. The texts of a deadline come from the registry, which is
kept by hand, and go through ``_cell`` all the same.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from seminars.config import AppConfig
from seminars.contracts import LABEL_DIGEST, IssueClient
from seminars.i18n import DEFAULT_LOCALE, LOCALES, Locale, bilingual, load_locales
from seminars.models import (
    FIELD_IDS,
    TBA_TITLE,
    Deadline,
    DigestDocument,
    DigestOutcome,
    Event,
    HealthFile,
    PickReason,
    SourceHealth,
    SourceHealthEntry,
    Speaker,
    ascii_url,
    ordered_deadlines,
    signed_points,
)
from seminars.outputs.deadlines import stated
from seminars.textrules import ascii_printable, clean_text

ROW_LIMITS: Final = (40, 30, 20, 15, 10, 5, 2, 1)
"""Rows per table: the first limit that keeps the body within MAX_BODY_CHARS is used."""

MEETING_ROW_LIMITS: Final = (40, 20, 10, 5, 2, 1)
"""Rows of the table of meetings, which has a limit of its own: the first one holds
until the other tables are down to the last of ROW_LIMITS, and only then the next."""

MEETING_WEEKS: Final = 8
"""Weeks from the Monday of the digest whose meetings are all listed."""

DEADLINE_DAYS: Final = 21
"""Days from the Monday of the digest (that day included) whose deadlines are listed."""

DEADLINE_ROWS: Final = 25
"""Rows of the table of deadlines: a limit of its own, outside ROW_LIMITS and
MEETING_ROW_LIMITS, which never shrinks."""

MAX_BODY_CHARS: Final = 60_000
"""GitHub refuses an issue body of more than 65536 characters."""

TITLE_MAX_CHARS: Final = 200
SERIES_MAX_CHARS: Final = 100
SPEAKERS_MAX_CHARS: Final = 160
LOCATION_MAX_CHARS: Final = 100
REASONS_MAX_CHARS: Final = 160
SOURCE_MAX_CHARS: Final = 100
ERROR_MAX_CHARS: Final = 160
URL_MAX_CHARS: Final = 1000
MAX_SPEAKERS: Final = 3
EMPTY_CELL: Final = "-"
ELLIPSIS: Final = "..."

_ONE_DAY: Final = timedelta(days=1)
_WEEK: Final = timedelta(days=7)
_OWNER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_ENTITIES: Final = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_MARKUP: Final = re.compile(r"[\\`*_\[\]|~]")
_ACTED_ON: Final = re.compile(
    r"(?i)@|#(?=\d)|\bGH-(?=\d)|://|www\.|(?:mailto|xmpp):|:[a-z0-9_+-]+:|\$"
)
"""What marks a word that GitHub changes after rendering; see the module docstring."""

_STATUS_LABELS: Final = {
    "cancelled": "digest.labels.cancelled",
    "tentative": "digest.labels.tentative",
}
_HEALTH_LABELS: Final = {
    "ok": "digest.health.status_ok",
    "empty": "digest.health.status_empty",
    "failed": "digest.health.status_failed",
}


@dataclass(frozen=True)
class _Context:
    """What rendering needs besides the events."""

    primary: Locale
    secondary: Locale
    zone: ZoneInfo
    week_start: date
    picks_threshold: float
    previous_issue_at: datetime | None = None

    @property
    def locales(self) -> tuple[Locale, Locale]:
        """Both languages, the primary one first."""
        return self.primary, self.secondary

    def both(self, key: str, /, **params: object) -> str:
        """A heading or label in both languages on one line."""
        return bilingual(key, self.primary, self.secondary, **params)

    def sentence(self, key: str, /, **params: object) -> str:
        """A sentence in both languages, one line each."""
        lines = (self.primary.t(key, **params), self.secondary.t(key, **params))
        return "\n".join(dict.fromkeys(lines))


def _issue_title(primary: Locale, week_start: date) -> str:
    """The title of the issue of a week: English only, and the same on every run."""
    return primary.t("digest.issue_title", date=week_start.isoformat())


def _context(
    config: AppConfig, week_start: date, previous_issue_at: datetime | None = None
) -> _Context:
    locales = load_locales(config.paths.locales_dir)
    return _Context(
        primary=locales[DEFAULT_LOCALE],
        secondary=locales[next(lang for lang in LOCALES if lang != DEFAULT_LOCALE)],
        zone=ZoneInfo(config.settings.timezone),
        week_start=week_start,
        picks_threshold=config.interests.threshold,
        previous_issue_at=previous_issue_at,
    )


# --- untrusted text ------------------------------------------------------------------------


def _one_line(text: str | None) -> str:
    """Cleaned text on a single line: every run of whitespace becomes one space."""
    return " ".join(clean_text(text or "").split())


def _shorten(text: str, limit: int) -> str:
    """``text`` in at most ``limit`` characters; a cut is marked with three dots."""
    if len(text) <= limit:
        return text
    return text[: limit - len(ELLIPSIS)].rstrip() + ELLIPSIS


def _plain(word: str) -> str:
    """A word as literal markdown: entities for HTML, a backslash before markup."""
    escaped = re.sub(r"[&<>]", lambda found: _ENTITIES[found.group()], word)
    return _MARKUP.sub(lambda found: f"\\{found.group()}", escaped)


def _code(word: str) -> str:
    """A word as a code span of a table cell, whatever backticks and pipes it contains."""
    longest = max((len(run) for run in re.findall("`+", word)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if word.startswith("`") or word.endswith("`") else ""
    content = word.replace("|", "\\|")  # a pipe would end the cell, also inside code
    return f"{fence}{pad}{content}{pad}{fence}"


def _cell(text: str | None, limit: int) -> str:
    """Untrusted text as the literal content of a table cell; empty when there is none."""
    line = _shorten(_one_line(text), limit)
    words = [_code(word) if _ACTED_ON.search(word) else _plain(word) for word in line.split()]
    return " ".join(words)


def _lines(lines: Sequence[str]) -> str:
    """The lines of a cell in both languages, each once; empty when there is none."""
    return "<br>".join(dict.fromkeys(line for line in lines if line))


def _reason(reason: PickReason, locale: Locale) -> str:
    """One picks reason in one language; a line of no known form stays as it is."""
    if reason.kind == "other":
        return reason.term
    place = locale.t(f"digest.reasons.places.{reason.place}") if reason.place else ""
    points = signed_points(reason.points or 0.0)
    return locale.t(f"digest.reasons.{reason.kind}", term=reason.term, place=place, points=points)


def _speaker(speaker: Speaker) -> str:
    name, affiliation = _one_line(speaker.name), _one_line(speaker.affiliation)
    return f"{name} ({affiliation})" if name and affiliation else name


# --- cells ---------------------------------------------------------------------------------


def _days(event: Event, zone: ZoneInfo) -> tuple[date, date]:
    """First and last local day of an event.

    An all-day event and a meeting, with hours or without, last until their end, which
    is exclusive: an end at local midnight closes the day before. Any other timed event
    counts for the day it starts.
    """
    first = event.start.astimezone(zone).date()
    if event.end is None or not (event.all_day or event.is_meeting):
        return first, first
    end = event.end.astimezone(zone)
    last = end.date() - _ONE_DAY if end.time() == time.min else end.date()
    return first, max(first, last)


def _date_cell(event: Event, ctx: _Context) -> str:
    first, last = _days(event, ctx.zone)
    return first.isoformat() if first == last else f"{first.isoformat()} - {last.isoformat()}"


def _time_cell(event: Event, ctx: _Context) -> str:
    if event.all_day:
        return ctx.both("digest.labels.all_day")
    text = f"{event.start.astimezone(ctx.zone):%H:%M}"
    if event.end is not None and event.end.astimezone(UTC) > event.start.astimezone(UTC):
        text = f"{text}-{event.end.astimezone(ctx.zone):%H:%M}"
    return text


def _title_cell(event: Event, ctx: _Context) -> str:
    """The title, marked when the talk is cancelled or tentative, above its series or host."""
    title = _cell(event.title, TITLE_MAX_CHARS)
    if not title or _one_line(event.title) == TBA_TITLE:
        title = ctx.both("digest.labels.tba")
    if event.status in _STATUS_LABELS:
        title = f"**{ctx.both(_STATUS_LABELS[event.status])}**: {title}"
    below = _cell(event.series, SERIES_MAX_CHARS) or _cell(event.host_org, SERIES_MAX_CHARS)
    return f"{title}<br>{below}" if below else title


def _speaker_cell(event: Event, ctx: _Context) -> str:
    people = [text for speaker in event.speakers if (text := _speaker(speaker))]
    more = [ELLIPSIS] if len(people) > MAX_SPEAKERS else []
    return _cell("; ".join([*people[:MAX_SPEAKERS], *more]), SPEAKERS_MAX_CHARS) or EMPTY_CELL


def _location_cell(event: Event, ctx: _Context) -> str:
    place = _cell(event.location, LOCATION_MAX_CHARS)
    if not event.is_online:
        return place or EMPTY_CELL
    online = ctx.both("digest.labels.online")
    return f"{place} ({online})" if place else online


def _field_cell(event: Event, ctx: _Context) -> str:
    names = [
        ", ".join(locale.t(f"fields.{field}") for field in event.fields) for locale in ctx.locales
    ]
    return _lines(names) or EMPTY_CELL


def _reasons_cell(event: Event, ctx: _Context) -> str:
    reasons = [PickReason.parse(text) for text in event.picks_reasons if text]
    lines = [
        _cell("; ".join(_reason(reason, locale) for reason in reasons), REASONS_MAX_CHARS)
        for locale in ctx.locales
    ]
    return _lines(lines) or EMPTY_CELL


def _note_cell(event: Event, ctx: _Context) -> str:
    """Whether a meeting is new since the previous issue and whether it is a pick."""
    marks = [
        ctx.both(key)
        for key, marked in (
            ("digest.labels.new", _is_new(event, ctx)),
            ("digest.labels.pick", event.is_pick(ctx.picks_threshold)),
        )
        if marked
    ]
    return ", ".join(marks) or EMPTY_CELL


def _link_cell(event: Event, ctx: _Context) -> str:
    url = ascii_url(str(event.url))
    if len(url) > URL_MAX_CHARS:
        return EMPTY_CELL
    return f"[{ctx.both('digest.labels.details')}](<{url}>)"


type _Column = tuple[str, Callable[[Event, _Context], str]]
"""Locale key of a column header, and the function that fills its cells."""

_WHEN_WHAT_WHO_WHERE: Final[tuple[_Column, ...]] = (
    ("digest.table.date", _date_cell),
    ("digest.table.time", _time_cell),
    ("digest.table.title", _title_cell),
    ("digest.table.speaker", _speaker_cell),
    ("digest.table.location", _location_cell),
)
_FIELD: Final[_Column] = ("digest.table.field", _field_cell)
_REASONS: Final[_Column] = ("digest.table.reasons", _reasons_cell)
_NOTE: Final[_Column] = ("digest.table.note", _note_cell)
_LINK: Final[_Column] = ("digest.table.link", _link_cell)
_FIELD_COLUMNS: Final = (*_WHEN_WHAT_WHO_WHERE, _LINK)
_PICK_COLUMNS: Final = (*_WHEN_WHAT_WHO_WHERE, _FIELD, _REASONS, _LINK)
_NEW_COLUMNS: Final = (*_WHEN_WHAT_WHO_WHERE, _FIELD, _LINK)
_MEETING_COLUMNS: Final = (
    *(column for column in _WHEN_WHAT_WHO_WHERE if column[0] != "digest.table.speaker"),
    _FIELD,
    _NOTE,
    _LINK,
)


# --- blocks --------------------------------------------------------------------------------


def _row(cells: Sequence[str]) -> str:
    return f"| {' | '.join(cells)} |"


def _table(
    events: Sequence[Event], columns: Sequence[_Column], ctx: _Context, rows: int
) -> list[str]:
    """A table of at most ``rows`` events, followed by a line that counts the rest."""
    lines = [_row([ctx.both(key) for key, _ in columns]), _row(["---"] * len(columns))]
    lines.extend(_row([cell(event, ctx) for _, cell in columns]) for event in events[:rows])
    blocks = ["\n".join(lines)]
    if len(events) > rows:
        blocks.append(ctx.sentence("digest.labels.more", count=len(events) - rows))
    return blocks


def _section(
    heading: str,
    events: Sequence[Event],
    columns: Sequence[_Column],
    empty_key: str,
    ctx: _Context,
    rows: int,
) -> list[str]:
    body = _table(events, columns, ctx, rows) if events else [ctx.sentence(empty_key)]
    return [f"## {heading}", *body]


def _in_week(event: Event, ctx: _Context) -> bool:
    """True when the event has at least one day in the week of the digest."""
    first, last = _days(event, ctx.zone)
    return first < ctx.week_start + _WEEK and last >= ctx.week_start


def _is_new(event: Event, ctx: _Context) -> bool:
    """True when the event was first seen after the previous issue was opened."""
    return ctx.previous_issue_at is not None and event.first_seen > ctx.previous_issue_at


def _opening(week: Sequence[Event], ctx: _Context) -> list[str]:
    """Heading, the days covered and how many talks (``week``, no meeting) take place."""
    held = [event for event in week if event.status != "cancelled"]
    picks = sum(event.is_pick(ctx.picks_threshold) for event in held)
    sunday = ctx.week_start + _WEEK - _ONE_DAY
    return [
        f"# {ctx.both('digest.heading', date=ctx.week_start.isoformat())}",
        ctx.sentence("digest.intro", start=ctx.week_start.isoformat(), end=sunday.isoformat()),
        ctx.sentence("digest.summary", events=len(held), picks=picks),
    ]


def _talks(week: Sequence[Event], ctx: _Context, rows: int) -> list[str]:
    """The picks, then one section per field, of the talks of the week (no meeting)."""
    picks = [event for event in week if event.is_pick(ctx.picks_threshold)]
    heading = ctx.both("digest.sections.picks")
    blocks = _section(heading, picks, _PICK_COLUMNS, "digest.empty.picks", ctx, rows)
    for field in FIELD_IDS:
        tagged = [event for event in week if field in event.fields]
        heading = ctx.both(f"fields.{field}")
        blocks += _section(heading, tagged, _FIELD_COLUMNS, "digest.empty.field", ctx, rows)
    return blocks


def _meetings_until(ctx: _Context) -> date:
    """The first day after the MEETING_WEEKS weeks whose meetings are all listed."""
    return ctx.week_start + MEETING_WEEKS * _WEEK


def _is_listed_meeting(event: Event, ctx: _Context) -> bool:
    """A meeting not over before the week, within MEETING_WEEKS weeks or new."""
    if not event.is_meeting:
        return False
    first, last = _days(event, ctx.zone)
    return last >= ctx.week_start and (first < _meetings_until(ctx) or _is_new(event, ctx))


def _meetings(events: Sequence[Event], ctx: _Context, rows: int) -> list[str]:
    """The meetings of the next MEETING_WEEKS weeks and the later ones that are new."""
    heading = ctx.both("digest.sections.meetings")
    listed = [event for event in events if _is_listed_meeting(event, ctx)]
    if not listed:
        last_day = (_meetings_until(ctx) - _ONE_DAY).isoformat()
        return [f"## {heading}", ctx.sentence("digest.empty.meetings", end=last_day)]
    return [f"## {heading}", *_table(listed, _MEETING_COLUMNS, ctx, rows)]


def _deadline_row(deadline: Deadline, ctx: _Context) -> str:
    """Date, the time and zone the page states, edition and label, the organizer's page."""
    hour = _lines([_cell(stated(deadline, locale), TITLE_MAX_CHARS) for locale in ctx.locales])
    what = _cell(f"{deadline.edition_short}: {deadline.label}", TITLE_MAX_CHARS)
    url = ascii_url(deadline.url)
    page = EMPTY_CELL
    if len(url) <= URL_MAX_CHARS:
        page = f"[{ctx.both('digest.labels.organizer')}](<{url}>)"
    return _row([deadline.date.isoformat(), hour or EMPTY_CELL, what or EMPTY_CELL, page])


_DEADLINE_COLUMNS: Final = (
    "digest.table.date",
    "digest.table.time",
    "digest.table.deadline",
    "digest.table.link",
)


def _deadlines(deadlines: Sequence[Deadline], ctx: _Context) -> list[str]:
    """The deadlines of the DEADLINE_DAYS days from the Monday; nothing when there is none."""
    until = ctx.week_start + timedelta(days=DEADLINE_DAYS)
    listed = [item for item in deadlines if ctx.week_start <= item.date < until]
    if not listed:
        return []
    header = _row([ctx.both(key) for key in _DEADLINE_COLUMNS])
    rows = [_deadline_row(item, ctx) for item in listed[:DEADLINE_ROWS]]
    blocks = [
        f"## {ctx.both('digest.sections.deadlines', days=DEADLINE_DAYS)}",
        ctx.sentence("digest.labels.deadlines_note"),
        "\n".join([header, _row(["---"] * len(_DEADLINE_COLUMNS)), *rows]),
    ]
    if len(listed) > DEADLINE_ROWS:
        left = len(listed) - DEADLINE_ROWS
        blocks.append(ctx.sentence("digest.labels.more_deadlines", count=left))
    return blocks


def _new(talks: Sequence[Event], ctx: _Context, rows: int) -> list[str]:
    """Talks (no meeting) first seen after the previous issue that have not taken place yet."""
    heading = ctx.both("digest.sections.new")
    if ctx.previous_issue_at is None:
        return [f"## {heading}", ctx.sentence("digest.empty.first_issue")]
    new = [
        event
        for event in talks
        if _is_new(event, ctx) and _days(event, ctx.zone)[1] >= ctx.week_start
    ]
    return _section(heading, new, _NEW_COLUMNS, "digest.empty.new", ctx, rows)


def _llm_sentence(health: HealthFile, ctx: _Context) -> str:
    """What the LLM of the most recent run used: a provider, stored answers or rules only.

    ``HealthFile.llm_last_run`` says it; a file written before it was kept
    names the provider that answered in its most recent run, if any did.
    """
    last = health.llm_last_run
    provider = _llm_provider(health) if last is None else last.provider
    if provider is not None:
        return ctx.sentence("digest.health.llm", provider=_cell(provider, SOURCE_MAX_CHARS))
    if last is not None and last.stored_answers is not None:
        return ctx.sentence("digest.health.llm_stored", count=last.stored_answers)
    return ctx.sentence("digest.health.llm_none")


def _llm_provider(health: HealthFile) -> str | None:
    """The provider that answered in the most recent run, if any did."""
    last = {name: item.history[-1] for name, item in health.llm.items() if item.history}
    if not last:
        return None
    latest = max(entry.at for entry in last.values())
    used = [
        (entry.calls, name)
        for name, entry in last.items()
        if entry.at == latest and entry.status == "ok"
    ]
    return max(used)[1] if used else None


def _well(history: SourceHealth) -> bool:
    """Whether the last run of a source delivered events and is known to have missed none."""
    last = history.last
    return last is not None and last.status == "ok" and last.read_in_full


def _shortfall(last: SourceHealthEntry, locale: Locale) -> str:
    """What a run that was not read in full missed, in one language."""
    reasons = []
    if last.skipped_pages:
        reasons.append(locale.t("digest.health.skipped", count=last.skipped_pages))
    if last.shrank_from is not None:
        listed = last.accepted or 0
        reasons.append(locale.t("digest.health.shrank", listed=listed, usual=last.shrank_from))
    return "; ".join(reasons)


def _health_row(name: str, history: SourceHealth, ctx: _Context) -> str:
    """One source that is not well: its state, when it last delivered, and what went wrong."""
    last = history.last
    status, events, error = EMPTY_CELL, 0, ""
    if last is not None:
        label = (
            "digest.health.status_partial" if last.status == "ok" else _HEALTH_LABELS[last.status]
        )
        status, events = ctx.both(label), last.events
        error = _cell(last.error, ERROR_MAX_CHARS)
        if not error and not last.read_in_full:
            error = _lines(
                [_cell(_shortfall(last, locale), ERROR_MAX_CHARS) for locale in ctx.locales]
            )
    success = ctx.both("digest.health.never")
    if history.last_success_at is not None:
        success = history.last_success_at.astimezone(ctx.zone).date().isoformat()
    source = _cell(name, SOURCE_MAX_CHARS) or EMPTY_CELL
    return _row([source, status, str(events), success, error or EMPTY_CELL])


def _resized(name: str, last: SourceHealthEntry, ctx: _Context) -> str:
    """The sentence about a source whose last run took its smaller listing as the usual size."""
    return ctx.sentence(
        "digest.health.resized",
        source=_cell(name, SOURCE_MAX_CHARS) or EMPTY_CELL,
        listed=last.accepted or 0,
        usual=last.resized_from,
    )


def _health(health: HealthFile, config: AppConfig, ctx: _Context, rows: int) -> list[str]:
    """How many sources are well, a row for each one that is not, and new usual sizes.

    A source is well when its last run delivered events and was read in
    full (``SourceHealthEntry.read_in_full``). ``rows`` bounds the rows of
    the table and, on their own, the sentences about new usual sizes.
    """
    sources = sorted(config.enabled_sources, key=lambda source: source.id)
    states = [(source, health.sources.get(source.id, SourceHealth())) for source in sources]
    unwell = [(source, state) for source, state in states if not _well(state)]
    counts = {"total": len(states), "ok": len(states) - len(unwell)}
    blocks = [f"## {ctx.both('digest.sections.health')}"]
    if not unwell:
        blocks.append(ctx.sentence("digest.health.all_ok", **counts))
    else:
        keys = ("source", "status", "events", "last_success", "error")
        header = _row([ctx.both(f"digest.health.{key}") for key in keys])
        lines = [_health_row(source.name, state, ctx) for source, state in unwell[:rows]]
        blocks.append(ctx.sentence("digest.health.summary", **counts))
        blocks.append("\n".join([header, _row(["---"] * len(keys)), *lines]))
    resized = [
        (source, state.last)
        for source, state in states
        if state.last is not None and state.last.resized_from is not None
    ]
    blocks += [_resized(source.name, last, ctx) for source, last in resized[:rows]]
    blocks.append(_llm_sentence(health, ctx))
    return blocks


def _closing(config: AppConfig, owner: str | None, ctx: _Context) -> list[str]:
    """Where the full list is, and the mention that makes GitHub send a notification."""
    blocks = ["---", ctx.sentence("digest.footer.site", url=config.settings.site.base_url)]
    login = (owner or "").strip()
    if login and not _OWNER.fullmatch(login):
        raise ValueError(f"owner is not a GitHub login: {ascii_printable(login)[:60]!r}")
    if login:
        blocks.append(ctx.sentence("digest.footer.notify", mention=f"@{login}"))
    return blocks


def _ordered(events: Sequence[Event]) -> list[Event]:
    """Events by start and uid; a uid that appears twice is an error."""
    repeated = sorted(
        uid for uid, count in Counter(event.uid for event in events).items() if count > 1
    )
    if repeated:
        raise ValueError(f"duplicate event uid: {repeated[0]!r}")
    return sorted(events, key=lambda event: (event.start.astimezone(UTC), event.uid))


def render_digest(
    events: Sequence[Event],
    health: HealthFile,
    *,
    config: AppConfig,
    week_start: date,
    previous_issue_at: datetime | None,
    owner: str | None = None,
    deadlines: Sequence[Deadline] = (),
) -> DigestDocument:
    """Render the digest for the week starting on ``week_start`` (a Monday).

    The contract is ``seminars.contracts.DigestRenderer``. The tables of
    talks hold the talks with a day in that week, by local days of the
    configured zone; the tables of meetings and of deadlines are described
    in the module docstring. A table that is too long ends with a count of
    the rows left out, and the limits shrink until the body fits into a
    GitHub issue: first ROW_LIMITS, then MEETING_ROW_LIMITS.
    """
    if week_start.weekday() != 0:
        raise ValueError(f"week_start must be a Monday: {week_start.isoformat()}")
    if previous_issue_at is not None and previous_issue_at.tzinfo is None:
        raise ValueError("previous_issue_at must be timezone-aware")
    ctx = _context(config, week_start, previous_issue_at)
    ordered = _ordered(events)
    deadline_blocks = _deadlines(ordered_deadlines(deadlines), ctx)
    talks = [event for event in ordered if not event.is_meeting]
    week = [event for event in talks if _in_week(event, ctx)]
    fixed_start = _opening(week, ctx)
    fixed_end = _closing(config, owner, ctx)

    def body(rows: int, meeting_rows: int) -> str:
        middle = [
            *_talks(week, ctx, rows),
            *_meetings(ordered, ctx, meeting_rows),
            *deadline_blocks,
            *_new(talks, ctx, rows),
            *_health(health, config, ctx, rows),
        ]
        return "\n\n".join([*fixed_start, *middle, *fixed_end]) + "\n"

    limits = [
        *((rows, MEETING_ROW_LIMITS[0]) for rows in ROW_LIMITS),
        *((ROW_LIMITS[-1], meeting_rows) for meeting_rows in MEETING_ROW_LIMITS[1:]),
    ]
    text = body(*limits[0])
    for rows, meeting_rows in limits[1:]:
        if len(text) <= MAX_BODY_CHARS:
            break
        text = body(rows, meeting_rows)
    title = _issue_title(ctx.primary, week_start)
    return DigestDocument(week_start=week_start, title=title, body=text)


def publish_digest(
    events: Sequence[Event],
    health: HealthFile,
    issues: IssueClient | None,
    *,
    config: AppConfig,
    now: datetime,
    owner: str | None = None,
    dry_run: bool = False,
    deadlines: Sequence[Deadline] = (),
) -> DigestOutcome:
    """Open this week's digest issue and close the earlier ones.

    The contract is ``seminars.contracts.DigestPublisher``. The week is the
    Monday-to-Sunday week that contains ``now`` in the configured zone. The
    newest other issue with the digest label gives the moment since which a
    talk counts as new. When an issue with this week's title exists, whether
    open or closed, nothing is changed. With ``dry_run`` or without a client
    nothing is written either; the outcome then holds what would be posted.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    today = now.astimezone(ZoneInfo(config.settings.timezone)).date()
    week_start = today - timedelta(days=today.weekday())
    title = _issue_title(Locale.load(DEFAULT_LOCALE, config.paths.locales_dir), week_start)
    found = issues.find_by_label(LABEL_DIGEST.name, state="all") if issues is not None else []
    earlier = [issue for issue in found if issue.title != title]
    document = render_digest(
        events,
        health,
        config=config,
        week_start=week_start,
        previous_issue_at=earlier[0].created_at if earlier else None,
        owner=owner,
        deadlines=deadlines,
    )
    if dry_run or issues is None:
        return DigestOutcome(action="dry_run", document=document)
    existing = issues.find_by_title(document.title)
    if existing is not None:
        return DigestOutcome(action="exists", document=document, issue_number=existing.number)
    issues.ensure_label(LABEL_DIGEST)
    created = issues.open(document.title, document.body, labels=[LABEL_DIGEST.name])
    closed = [issue.number for issue in earlier if issue.state == "open"]
    for number in closed:
        issues.close(number)
    return DigestOutcome(
        action="created", document=document, issue_number=created.number, closed_issues=closed
    )
