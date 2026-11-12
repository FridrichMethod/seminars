"""The weekly digest: one GitHub issue with the talks of a week.

``render_digest`` turns the stored events and the run history into a
markdown document: the picks first, then one table per field, the talks
that were first seen since the previous issue, and the state of the sources.
``publish_digest`` opens the issue for the week that contains ``now`` and
closes the earlier ones.

No state is kept. "New since the last issue" is whatever was first seen
after the previous digest issue was created, and a week is never posted
twice because an issue with this week's title is looked for first. Both
functions read no clock: the same arguments give the same document.

Language. The issue title is English and has to stay as it is (it is what
makes a week idempotent). Inside the body every heading, table header and
label is given in both languages, English first, and every sentence on two
lines. All of that text comes from the locale files; this module holds none.
Event content is not translated.

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
written by this module alone, to the detail page of an event, with the
address percent-encoded inside angle brackets.
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
    DigestDocument,
    DigestOutcome,
    Event,
    HealthFile,
    SourceHealth,
    Speaker,
    ascii_url,
)
from seminars.textrules import ascii_printable, clean_text

ROW_LIMITS: Final = (40, 30, 20, 15, 10, 5, 2, 1)
"""Rows per table: the first limit that keeps the body within MAX_BODY_CHARS is used."""

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


def _context(config: AppConfig, week_start: date) -> _Context:
    locales = load_locales(config.paths.locales_dir)
    return _Context(
        primary=locales[DEFAULT_LOCALE],
        secondary=locales[next(lang for lang in LOCALES if lang != DEFAULT_LOCALE)],
        zone=ZoneInfo(config.settings.timezone),
        week_start=week_start,
        picks_threshold=config.interests.threshold,
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


def _speaker(speaker: Speaker) -> str:
    name, affiliation = _one_line(speaker.name), _one_line(speaker.affiliation)
    return f"{name} ({affiliation})" if name and affiliation else name


# --- cells ---------------------------------------------------------------------------------


def _days(event: Event, zone: ZoneInfo) -> tuple[date, date]:
    """First and last local day of an event; a timed event counts for the day it starts."""
    first = event.start.astimezone(zone).date()
    if not event.all_day or event.end is None:
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
    return ", ".join(ctx.primary.t(f"fields.{field}") for field in event.fields) or EMPTY_CELL


def _reasons_cell(event: Event, ctx: _Context) -> str:
    return _cell("; ".join(event.picks_reasons), REASONS_MAX_CHARS) or EMPTY_CELL


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
_LINK: Final[_Column] = ("digest.table.link", _link_cell)
_FIELD_COLUMNS: Final = (*_WHEN_WHAT_WHO_WHERE, _LINK)
_PICK_COLUMNS: Final = (*_WHEN_WHAT_WHO_WHERE, _FIELD, _REASONS, _LINK)
_NEW_COLUMNS: Final = (*_WHEN_WHAT_WHO_WHERE, _FIELD, _LINK)


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


def _opening(week: Sequence[Event], ctx: _Context) -> list[str]:
    """Heading, the days covered and how many talks take place."""
    held = [event for event in week if event.status != "cancelled"]
    picks = sum(event.is_pick(ctx.picks_threshold) for event in held)
    sunday = ctx.week_start + _WEEK - _ONE_DAY
    return [
        f"# {ctx.both('digest.heading', date=ctx.week_start.isoformat())}",
        ctx.sentence("digest.intro", start=ctx.week_start.isoformat(), end=sunday.isoformat()),
        ctx.sentence("digest.summary", events=len(held), picks=picks),
    ]


def _talks(week: Sequence[Event], ctx: _Context, rows: int) -> list[str]:
    """The picks, then one section per field."""
    picks = [event for event in week if event.is_pick(ctx.picks_threshold)]
    heading = ctx.both("digest.sections.picks")
    blocks = _section(heading, picks, _PICK_COLUMNS, "digest.empty.picks", ctx, rows)
    for field in FIELD_IDS:
        tagged = [event for event in week if field in event.fields]
        heading = ctx.both(f"fields.{field}")
        blocks += _section(heading, tagged, _FIELD_COLUMNS, "digest.empty.field", ctx, rows)
    return blocks


def _new(
    events: Sequence[Event], previous_issue_at: datetime | None, ctx: _Context, rows: int
) -> list[str]:
    """Talks first seen after the previous issue that have not taken place yet."""
    heading = ctx.both("digest.sections.new")
    if previous_issue_at is None:
        return [f"## {heading}", ctx.sentence("digest.empty.first_issue")]
    new = [
        event
        for event in events
        if event.first_seen > previous_issue_at and _days(event, ctx.zone)[1] >= ctx.week_start
    ]
    return _section(heading, new, _NEW_COLUMNS, "digest.empty.new", ctx, rows)


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


def _health_row(name: str, history: SourceHealth, ctx: _Context) -> str:
    """One source that did not deliver: its state, and when it last did."""
    last = history.last
    status, events, error = EMPTY_CELL, 0, ""
    if last is not None:
        status, events = ctx.both(_HEALTH_LABELS[last.status]), last.events
        error = _cell(last.error, ERROR_MAX_CHARS)
    success = ctx.both("digest.health.never")
    if history.last_success_at is not None:
        success = history.last_success_at.astimezone(ctx.zone).date().isoformat()
    source = _cell(name, SOURCE_MAX_CHARS) or EMPTY_CELL
    return _row([source, status, str(events), success, error or EMPTY_CELL])


def _health(health: HealthFile, config: AppConfig, ctx: _Context, rows: int) -> list[str]:
    """How many sources delivered in their last run, and a row for each one that did not."""
    sources = sorted(config.enabled_sources, key=lambda source: source.id)
    states = [(source, health.sources.get(source.id, SourceHealth())) for source in sources]
    unwell = [
        (source, state)
        for source, state in states
        if state.last is None or state.last.status != "ok"
    ]
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
    provider = _llm_provider(health)
    if provider is None:
        blocks.append(ctx.sentence("digest.health.llm_none"))
    else:
        blocks.append(ctx.sentence("digest.health.llm", provider=_cell(provider, SOURCE_MAX_CHARS)))
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
) -> DigestDocument:
    """Render the digest for the week starting on ``week_start`` (a Monday).

    The contract is ``seminars.contracts.DigestRenderer``. The tables hold
    the events with a day in that week, by local days of the configured
    zone; a table that is too long ends with a count of the talks left out,
    and the limit shrinks until the body fits into a GitHub issue.
    """
    if week_start.weekday() != 0:
        raise ValueError(f"week_start must be a Monday: {week_start.isoformat()}")
    if previous_issue_at is not None and previous_issue_at.tzinfo is None:
        raise ValueError("previous_issue_at must be timezone-aware")
    ctx = _context(config, week_start)
    ordered = _ordered(events)
    week = [event for event in ordered if _in_week(event, ctx)]
    fixed_start = _opening(week, ctx)
    fixed_end = _closing(config, owner, ctx)

    def body(rows: int) -> str:
        middle = [
            *_talks(week, ctx, rows),
            *_new(ordered, previous_issue_at, ctx, rows),
            *_health(health, config, ctx, rows),
        ]
        return "\n\n".join([*fixed_start, *middle, *fixed_end]) + "\n"

    text = body(ROW_LIMITS[0])
    for rows in ROW_LIMITS[1:]:
        if len(text) <= MAX_BODY_CHARS:
            break
        text = body(rows)
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
