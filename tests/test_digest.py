"""Tests for the weekly digest: the markdown document and the issue it becomes.

tests/golden/digest.md holds one whole document. The body has Chinese text
and the charset gate accepts only ASCII there, so the file holds the body
with every other character as a backslash escape
(``seminars.textrules.ascii_safe``). Run the tests with
SEMINARS_UPDATE_GOLDEN=1 to write it again, then review the diff.

The markdown is also read by a real CommonMark parser (markdown-it-py, which
is installed as a dependency of the command line library); those tests are
skipped where it is missing.
"""

from __future__ import annotations

import dataclasses
import html
import json
import os
import random
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings

from factories import PACIFIC, REFERENCE_NOW, event_lists, make_event, make_source
from fakes import FakeIssueClient
from seminars.config import AppConfig, Interests
from seminars.contracts import LABEL_DIGEST, DigestPublisher, DigestRenderer, load_implementation
from seminars.i18n import bilingual, load_locales
from seminars.models import FIELD_IDS, Event, HealthFile, LLMHealthEntry, SourceHealthEntry
from seminars.outputs.digest import (
    MAX_BODY_CHARS,
    SPEAKERS_MAX_CHARS,
    publish_digest,
    render_digest,
)
from seminars.textio import read_text, write_text_atomic
from seminars.textrules import ascii_safe, decorative_characters

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
LOCALES = load_locales(Path(__file__).resolve().parents[1] / "locales")


def both(key: str, **params: object) -> str:
    """A locale message in both languages, English first, as headings and labels show it."""
    return bilingual(key, LOCALES["en"], LOCALES["zh-CN"], **params)


def zh(key: str, **params: object) -> str:
    """The Chinese line of a sentence."""
    return LOCALES["zh-CN"].t(key, **params)


WEEK = date(2026, 11, 16)  # a Monday
MONDAY_NOON = datetime(2026, 11, 16, 20, 0, tzinfo=UTC)
PREVIOUS_ISSUE = datetime(2026, 11, 9, 15, 7, tzinfo=UTC)
DETAIL_URL = "https://bioengineering.stanford.edu/events/protein-design-seminar"
DETAILS = f"[{both('digest.labels.details')}](<{DETAIL_URL}>)"
ALL_DAY = both("digest.labels.all_day")
ONLINE = both("digest.labels.online")
TBA = both("digest.labels.tba")
NEVER = both("digest.health.never")
ROCKET = "\U0001f680"
DIRTY = f"Launch{ROCKET}\u00a0day\u200b \ufb01ne \u2728"  # emoji, odd spaces, a ligature
HOSTILE = [
    "[click](https://evil.example/a) ![img](https://evil.example/x.png)",
    "<img src=x onerror=alert(1)> <script>alert(1)</script> <!-- c --> <b>bold</b>",
    "@octocat and @org/team: see #1, GH-12, evil/repo#3 and evil/repo@abc1234",
    "# Heading\n## Another\n- list\n1. item\n> quote\n```\ncode\n```\n\n---\n| a | b |",
    "pipes | and \\| backslashes \\ at the end \\",
    "*bold* **b** _i_ __u__ ~~s~~ `code` ``x`` ```",
    "https://evil.example www.evil.example mailto:a@b.co <https://e.example> HTTPS://E.EXAMPLE",
    ":rocket: :+1: 10:30 $x^2$ $$y$$ $`z`$",
    "&lt; &amp; &#64;octocat &copy; \\@octocat `@octocat` ``` |`@a|b`",
    "[^1] [x]: http://e [ ] task ![a][b] </td></tr></table><h1>x</h1>",
]


def at(month: int, day: int, hour: int, minute: int = 0, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=PACIFIC)


def render(
    config: AppConfig,
    events: list[Event] | None = None,
    health: HealthFile | None = None,
    week_start: date = WEEK,
    previous_issue_at: datetime | None = None,
    owner: str | None = None,
) -> str:
    document = render_digest(
        events or [],
        health or HealthFile(),
        config=config,
        week_start=week_start,
        previous_issue_at=previous_issue_at,
        owner=owner,
    )
    assert document.week_start == week_start
    return document.body


def blocks(body: str) -> list[str]:
    return body.rstrip("\n").split("\n\n")


def section(body: str, heading: str) -> list[str]:
    """The blocks below the heading that starts with ``## <heading>``, up to the next one."""
    parts = blocks(body)
    start = next(i for i, part in enumerate(parts) if part.startswith(f"## {heading}"))
    rest = parts[start + 1 :]
    stop = next((i for i, part in enumerate(rest) if part.startswith(("## ", "---"))), len(rest))
    return rest[:stop]


def rows(table: str) -> list[list[str]]:
    """The cells of the data rows of a markdown table; an escaped pipe does not split."""
    lines = table.split("\n")[2:]
    return [[cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]] for line in lines]


def one_row(config: AppConfig, event: Event, heading: str = "Biology") -> list[str]:
    (table,) = section(render(config, [event]), heading)
    (cells,) = rows(table)
    return cells


def with_sources(config: AppConfig, *ids: str, **overrides: Any) -> AppConfig:
    sources = tuple(make_source(id=source_id, name=f"Source {source_id}") for source_id in ids)
    return dataclasses.replace(config, sources=sources, **overrides)


def ran(
    health: HealthFile, source_id: str, status: str, events: int = 0, **more: Any
) -> HealthFile:
    entry = {"at": REFERENCE_NOW, "status": status, "events": events, **more}
    return health.with_source_entry(source_id, SourceHealthEntry.model_validate(entry))


def assert_matches_golden(name: str, text: str) -> None:
    path = GOLDEN_DIR / name
    if os.environ.get("SEMINARS_UPDATE_GOLDEN"):
        write_text_atomic(path, text)
    assert read_text(path) == text, f"{name} differs; see the module docstring"


def golden_events() -> list[Event]:
    """Talks of the golden week; only "tba" and "next-month" are new since the last issue."""
    seen_before = PREVIOUS_ISSUE - timedelta(days=3)

    def event(uid: str, title: str, start: datetime, **overrides: Any) -> Event:
        url = f"https://example.stanford.edu/events/{uid}"
        defaults = {"uid": uid, "title": title, "start": start, "url": url}
        defaults.update(end=start + timedelta(hours=1), first_seen=seen_before)
        return make_event(**{**defaults, **overrides})

    return [
        make_event(
            uid="pick",
            first_seen=seen_before,
            speakers=[
                {"name": "Jane Doe", "affiliation": "Stanford University"},
                {"name": "Li Wu"},
            ],
            fields=["bio", "ai-ml"],
            picks_score=9.0,
            picks_reasons=[
                '"protein design" in title (+3)',
                '"diffusion model" in title (+3)',
                'series "Bioengineering Seminar" (+3)',
            ],
        ),
        event(
            "tba",
            "TBA",
            at(11, 18, 16, 30),
            end=None,
            speakers=[{"name": "Zo\u00e9 M\u00fcller", "affiliation": "ETH Z\u00fcrich"}],
            series="Physical Chemistry Seminar",
            fields=["chem"],
            first_seen=PREVIOUS_ISSUE + timedelta(days=2),
        ),
        event(
            "all-day",
            "Bay Area Symposium on Structural Biology",
            at(11, 20, 0),
            end=at(11, 22, 0),
            all_day=True,
            speakers=[],
            series=None,
            host_org="SLAC",
            location="SLAC, Kavli Auditorium",
            fields=["chem", "bio", "physics"],
            picks_score=4.5,
            picks_reasons=['"cryo-EM" in abstract (+3)', 'penalty "undergraduate" (-0.5)', "x"],
        ),
        event(
            "cancelled",
            "Random matrices and number theory",
            at(11, 16, 16),
            speakers=[{"name": "Wei Zhang", "affiliation": "MIT"}],
            series="Math Colloquium",
            location="Building 380, Room 380-C",
            fields=["math"],
            status="cancelled",
        ),
        event(
            "online",
            "Serving <b>large</b> models | cheap & fast, see https://evil.example @octocat",
            at(11, 19, 15, 30),
            speakers=[{"name": "Priya Raman", "affiliation": "Example Labs"}],
            series="MLSys Seminar",
            location=None,
            is_online=True,
            fields=["ai-ml", "cs"],
            status="tentative",
        ),
        event(
            "hybrid",
            "Grand Rounds: cell therapies for solid tumors",
            at(11, 22, 23, 30),
            end=at(11, 23, 0, 30),
            speakers=[
                {"name": name} for name in ("Maria Chen", "Omar Haddad", "Ana Diaz", "Bo Li")
            ],
            series=None,
            host_org="Stanford Medicine",
            location="Li Ka Shing Center, Berg Hall",
            is_online=True,
            fields=["med"],
        ),
        event("last-week", "Too early", at(11, 15, 12), fields=["math"]),
        event(
            "next-month",
            "Language models for genome engineering",
            at(12, 10, 13),
            fields=["ai-ml", "bio"],
            first_seen=PREVIOUS_ISSUE + timedelta(hours=1),
        ),
    ]


def golden_setup(config: AppConfig) -> tuple[AppConfig, HealthFile]:
    config = with_sources(config, "sites-ok", "sites-empty", "sites-failed", "sites-new")
    health = ran(HealthFile(), "sites-ok", "ok", 12)
    health = ran(health, "sites-empty", "ok", 3, at=REFERENCE_NOW - timedelta(days=7))
    health = ran(health, "sites-empty", "empty")
    health = ran(health, "sites-failed", "failed", error="HTTP 503: https://failed.example/feed")
    llm = LLMHealthEntry(at=REFERENCE_NOW, status="ok", calls=4)
    return config, health.with_llm_entry("claude-cli", llm)


class TestDocument:
    def test_the_contracts_point_at_the_implementation(self) -> None:
        renderer: DigestRenderer = load_implementation("digest_render")
        publisher: DigestPublisher = load_implementation("digest_publish")
        assert (renderer, publisher) == (render_digest, publish_digest)

    def test_whole_document_matches_the_golden_file(self, app_config: AppConfig) -> None:
        config, health = golden_setup(app_config)
        document = render_digest(
            golden_events(),
            health,
            config=config,
            week_start=WEEK,
            previous_issue_at=PREVIOUS_ISSUE,
            owner="octocat",
        )
        assert document.title == "Seminars - week of 2026-11-16"
        assert document.body.endswith("\n")
        assert not document.body.endswith("\n\n")
        assert decorative_characters(document.body) == []
        assert_matches_golden("digest.md", ascii_safe(document.body))

    def test_the_title_is_english_and_names_the_monday(self, app_config: AppConfig) -> None:
        document = render_digest(
            [], HealthFile(), config=app_config, week_start=date(2027, 1, 4), previous_issue_at=None
        )
        assert document.title == "Seminars - week of 2027-01-04"
        assert document.title.isascii()

    def test_headings_are_bilingual_and_in_order(self, app_config: AppConfig) -> None:
        locales = load_locales(app_config.paths.locales_dir)

        def checked(key: str, **params: object) -> str:
            text = bilingual(key, locales["en"], locales["zh-CN"], **params)
            english, _, chinese = text.partition(" / " if key.startswith("digest") else "\x00")
            assert key.startswith("fields") or (english.isascii() and not chinese.isascii())
            return text

        body = render(app_config)
        headings = [line for line in body.split("\n") if line.startswith("#")]
        assert headings == [
            f"# {checked('digest.heading', date='2026-11-16')}",
            f"## {checked('digest.sections.picks')}",
            *(f"## {checked(f'fields.{field}')}" for field in FIELD_IDS),
            f"## {checked('digest.sections.new')}",
            f"## {checked('digest.sections.health')}",
        ]
        assert headings[1] == "## Picks / \u7cbe\u9009"
        assert headings[2] == "## Math / \u6570\u5b66"

    def test_an_empty_week(self, app_config: AppConfig) -> None:
        body = render(app_config)
        english = [part.split("\n")[0] for part in blocks(body) if not part.startswith("#")]
        assert english == [
            "Talks from 2026-11-16 to 2026-11-22. All times are Pacific Time.",
            "0 talks this week, 0 of them picks.",
            "No picks this week.",
            *["No talks this week."] * 7,
            "First issue: everything is new.",
            "All 0 sources are healthy.",
            "Field tagging used rules only (no LLM).",
            "---",
            "Full list and calendar subscriptions: https://fridrichmethod.github.io/seminars/",
        ]
        sentences = [part for part in blocks(body) if not part.startswith(("#", "---"))]
        for sentence in sentences:
            first, second = sentence.split("\n")
            assert first.isascii()
            assert not second.isascii()

    def test_no_text_is_hardcoded(self, app_config: AppConfig) -> None:
        for lang, mark in (("en", "EN"), ("zh-CN", "ZH")):
            path = app_config.paths.locales_dir / f"{lang}.json"

            def marked(tree: Any, mark: str = mark) -> Any:
                if isinstance(tree, dict):
                    return {key: marked(value) for key, value in tree.items()}
                return f"{mark}<{tree}>"

            write_text_atomic(path, json.dumps(marked(json.loads(read_text(path)))))
        config, health = golden_setup(app_config)
        body = render(config, golden_events(), health, previous_issue_at=PREVIOUS_ISSUE, owner="o")
        ours = re.sub(r"(?:EN|ZH)<[^>]*>", "", body)  # what is left is not from a locale file
        ours = re.sub(r"\[\]\(<[^>]*>\)", "", ours)  # links
        parts = [part.strip() for part in blocks(ours)]  # a sentence leaves a lone newline
        table_rows = [row for part in parts if part.startswith("|") for row in rows(part)]
        assert len(table_rows) > 10
        for cells in table_rows:  # time of a talk, or state of a source: only labels
            assert not re.search(r"[A-Za-z]", cells[1]), cells
        prose = "\n".join(part for part in parts if not part.startswith("|"))
        assert set(re.findall(r"[A-Za-z]{4,}", prose)) == set()

    def test_the_same_input_gives_the_same_document(self, app_config: AppConfig) -> None:
        config, health = golden_setup(app_config)
        events = golden_events()
        before = [event.model_dump_json() for event in events]
        first = render(config, events, health, previous_issue_at=PREVIOUS_ISSUE)
        random.Random(5).shuffle(events)
        assert render(config, events, health, previous_issue_at=PREVIOUS_ISSUE) == first
        assert sorted(event.model_dump_json() for event in events) == sorted(before)

    @pytest.mark.parametrize(
        ("arguments", "message"),
        [
            ({"week_start": date(2026, 11, 17)}, "week_start must be a Monday: 2026-11-17"),
            (
                {"previous_issue_at": datetime(2026, 11, 9)},
                "previous_issue_at must be timezone-aware",
            ),
            ({"events": [make_event(uid="same")] * 2}, "duplicate event uid: 'same'"),
            ({"owner": "not a login"}, "owner is not a GitHub login: 'not a login'"),
            ({"owner": "evil\n# x"}, "owner is not a GitHub login: 'evil\\\\n# x'"),
        ],
    )
    def test_arguments_are_checked(
        self, arguments: dict[str, Any], message: str, app_config: AppConfig
    ) -> None:
        with pytest.raises(ValueError, match=re.escape(message)):
            render(app_config, **arguments)


class TestTalks:
    def test_a_row_of_a_field_table(self, app_config: AppConfig) -> None:
        (table,) = section(render(app_config, [make_event()]), "Biology")
        header, rule, row = table.split("\n")
        keys = ("date", "time", "title", "speaker", "location", "link")
        assert header == f"| {' | '.join(both(f'digest.table.{key}') for key in keys)} |"
        assert header.startswith("| Date / \u65e5\u671f | Time / ")
        assert rule == "| --- | --- | --- | --- | --- | --- |"
        assert row == (
            "| 2026-11-17 | 16:00-17:00 "
            "| Protein design with diffusion models<br>Bioengineering Seminar "
            f"| Jane Doe (Stanford University) | Shriram Center, Room 104 | {DETAILS} |"
        )

    def test_a_row_of_the_picks_table(self, app_config: AppConfig) -> None:
        pick = make_event(picks_score=3.0, picks_reasons=["title: protein design", "series: x"])
        low = make_event(uid="low", picks_score=2.9, picks_reasons=["title: protein"])
        (table,) = section(render(app_config, [pick, low]), "Picks")
        assert table.split("\n")[0].split(" | ")[5:7] == [
            both("digest.table.field"),
            both("digest.table.reasons"),
        ]
        (cells,) = rows(table)
        biology = f"Biology<br>{zh('fields.bio')}"
        assert cells[5:] == [biology, "title: protein design; series: x", DETAILS]
        eager = dataclasses.replace(app_config, interests=Interests(threshold=2.5))
        assert len(rows(section(render(eager, [pick, low]), "Picks")[0])) == 2

    @pytest.mark.parametrize(
        ("overrides", "date_cell", "time_cell"),
        [
            ({}, "2026-11-17", "16:00-17:00"),
            ({"end": None}, "2026-11-17", "16:00"),
            ({"end": at(11, 17, 16)}, "2026-11-17", "16:00"),
            ({"start": at(11, 17, 23, 30), "end": at(11, 18, 0, 30)}, "2026-11-17", "23:30-00:30"),
            (
                {"start": datetime(2026, 11, 18, 3, 5, tzinfo=UTC), "end": None},
                "2026-11-17",
                "19:05",
            ),
            ({"all_day": True, "start": at(11, 17, 0), "end": None}, "2026-11-17", ALL_DAY),
            (
                {"all_day": True, "start": at(11, 17, 0), "end": at(11, 20, 0)},
                "2026-11-17 - 2026-11-19",
                ALL_DAY,
            ),
        ],
    )
    def test_date_and_time(
        self, overrides: dict[str, Any], date_cell: str, time_cell: str, app_config: AppConfig
    ) -> None:
        assert one_row(app_config, make_event(**overrides))[:2] == [date_cell, time_cell]

    @pytest.mark.parametrize(
        ("overrides", "title_cell"),
        [
            ({"title": "TBA"}, f"{TBA}<br>Bioengineering Seminar"),
            ({"title": ROCKET, "series": None}, f"{TBA}<br>Stanford Bioengineering"),
            (
                {"title": "T", "status": "cancelled"},
                f"**{both('digest.labels.cancelled')}**: T<br>Bioengineering Seminar",
            ),
            (
                {"title": "T", "status": "tentative"},
                f"**{both('digest.labels.tentative')}**: T<br>Bioengineering Seminar",
            ),
            ({"title": "Two\nlines", "series": " "}, "Two lines<br>Stanford Bioengineering"),
            ({"title": "x" * 300, "series": "y" * 150}, "x" * 197 + "...<br>" + "y" * 97 + "..."),
        ],
    )
    def test_title(self, overrides: dict[str, Any], title_cell: str, app_config: AppConfig) -> None:
        assert one_row(app_config, make_event(**overrides))[2] == title_cell

    @pytest.mark.parametrize(
        ("overrides", "speaker_cell", "location_cell"),
        [
            ({"speakers": [], "location": None}, "-", "-"),
            (
                {"speakers": [{"name": "A"}, {"name": "B", "affiliation": "X"}]},
                "A; B (X)",
                "Shriram Center, Room 104",
            ),
            ({"speakers": [{"name": n} for n in "ABCD"], "location": " "}, "A; B; C; ...", "-"),
            ({"is_online": True, "location": None}, "Jane Doe (Stanford University)", ONLINE),
            (
                {"is_online": True, "location": "Room 1"},
                "Jane Doe (Stanford University)",
                f"Room 1 ({ONLINE})",
            ),
        ],
    )
    def test_speakers_and_location(
        self,
        overrides: dict[str, Any],
        speaker_cell: str,
        location_cell: str,
        app_config: AppConfig,
    ) -> None:
        assert one_row(app_config, make_event(**overrides))[3:5] == [speaker_cell, location_cell]

    def test_the_week_is_seven_local_days(self, app_config: AppConfig) -> None:
        events = [
            make_event(uid="sunday-before", start=at(11, 15, 23, 59), end=None),
            make_event(uid="monday", start=at(11, 16, 0), end=None),
            make_event(uid="sunday", start=at(11, 22, 23, 59), end=None),
            make_event(uid="monday-after", start=at(11, 23, 0), end=None),
            make_event(uid="utc", start=datetime(2026, 11, 23, 7, 59, tzinfo=UTC), end=None),
            make_event(uid="running", start=at(11, 14, 0), end=at(11, 17, 0), all_day=True),
            make_event(uid="over", start=at(11, 14, 0), end=at(11, 16, 0), all_day=True),
        ]
        (table,) = section(render(app_config, events), "Biology")
        links = [cells[-1] for cells in rows(table)]
        assert [re.findall(r"\(<(.*)>\)", link) for link in links] == [[DETAIL_URL]] * 4
        assert [cells[:2] for cells in rows(table)] == [
            ["2026-11-14 - 2026-11-16", ALL_DAY],
            ["2026-11-16", "00:00"],
            ["2026-11-22", "23:59"],
            ["2026-11-22", "23:59"],
        ]
        assert "4 talks this week, 0 of them picks." in render(app_config, events)

    def test_a_talk_is_listed_under_each_of_its_fields(self, app_config: AppConfig) -> None:
        body = render(app_config, [make_event(fields=["math", "physics"], status="cancelled")])
        for heading, listed in (("Math", True), ("Physics", True), ("Biology", False)):
            assert section(body, heading)[0].startswith("| Date") == listed
        assert "0 talks this week, 0 of them picks." in body  # a cancelled talk is not counted

    def test_long_tables_are_cut_and_say_how_much_is_missing(self, app_config: AppConfig) -> None:
        events = [make_event(uid=f"e{i:03}", start=at(11, 16, 9, i), end=None) for i in range(45)]
        table, more = section(render(app_config, events), "Biology")
        assert [cells[1] for cells in rows(table)] == [f"09:{i:02}" for i in range(40)]
        assert more == f"5 more\n{zh('digest.labels.more', count=5)}"


class TestNewSinceLastIssue:
    def test_only_what_was_first_seen_later_and_is_still_ahead(self, app_config: AppConfig) -> None:
        seen = {"first_seen": PREVIOUS_ISSUE + timedelta(seconds=1)}
        events = [
            make_event(uid="old", first_seen=PREVIOUS_ISSUE),
            make_event(uid="new-this-week", title="This week", **seen),
            make_event(
                uid="new-later", title="In March", start=at(3, 2, 12, year=2027), end=None, **seen
            ),
            make_event(uid="new-but-over", title="Over", start=at(11, 15, 12), end=None, **seen),
        ]
        body = render(app_config, events, previous_issue_at=PREVIOUS_ISSUE)
        (table,) = section(body, "New since last issue")
        assert table.split("\n")[0].count("|") == 8
        biology = f"Biology<br>{zh('fields.bio')}"
        assert [(cells[0], cells[2].split("<br>")[0], cells[5]) for cells in rows(table)] == [
            ("2026-11-17", "This week", biology),
            ("2027-03-02", "In March", biology),
        ]

    def test_nothing_new_and_first_issue(self, app_config: AppConfig) -> None:
        events = [make_event(first_seen=PREVIOUS_ISSUE)]
        body = render(app_config, events, previous_issue_at=PREVIOUS_ISSUE)
        assert section(body, "New since last issue") == [
            f"Nothing new since the last issue.\n{zh('digest.empty.new')}"
        ]
        (first,) = section(render(app_config, events), "New since last issue")
        assert first.startswith("First issue: everything is new.\n")


class TestHealthAndFooter:
    def test_every_source_is_fine(self, app_config: AppConfig) -> None:
        config = with_sources(app_config, "sites-a", "sites-b")
        health = ran(ran(HealthFile(), "sites-a", "ok", 3), "sites-b", "ok", 1)
        assert section(render(config, health=health), "Source health") == [
            f"All 2 sources are healthy.\n{zh('digest.health.all_ok', total=2)}",
            f"Field tagging used rules only (no LLM).\n{zh('digest.health.llm_none')}",
        ]

    def test_sources_that_did_not_deliver_are_listed(self, app_config: AppConfig) -> None:
        config = with_sources(app_config, "sites-ok", "sites-empty", "sites-failed", "sites-new")
        off = make_source(id="sites-off", enabled=False)
        config = dataclasses.replace(config, sources=(*config.sources, off))
        health = ran(HealthFile(), "sites-ok", "ok", 12)
        health = ran(health, "sites-empty", "ok", 3, at=datetime(2026, 11, 4, 7, 59, tzinfo=UTC))
        health = ran(health, "sites-empty", "empty")
        health = ran(health, "sites-failed", "failed", error="HTTP 503 <b>")
        health = ran(ran(health, "sites-off", "failed"), "sites-removed", "failed")
        summary, table, _ = section(render(config, health=health), "Source health")
        assert summary.split("\n")[0] == "1 of 4 sources are healthy."
        keys = ("source", "status", "events", "last_success", "error")
        header = f"| {' | '.join(both(f'digest.health.{key}') for key in keys)} |"
        assert table.split("\n")[0] == header
        assert rows(table) == [
            ["Source sites-empty", both("digest.health.status_empty"), "0", "2026-11-03", "-"],
            [
                "Source sites-failed",
                both("digest.health.status_failed"),
                "0",
                NEVER,
                "HTTP 503 &lt;b&gt;",
            ],
            ["Source sites-new", "-", "0", NEVER, "-"],
        ]

    def test_the_provider_of_the_last_run_is_named(self, app_config: AppConfig) -> None:
        def entry(days_ago: int, status: str, calls: int = 0) -> LLMHealthEntry:
            moment = REFERENCE_NOW - timedelta(days=days_ago)
            return LLMHealthEntry.model_validate({"at": moment, "status": status, "calls": calls})

        health = HealthFile().with_llm_entry("anthropic", entry(1, "ok", 9))
        health = health.with_llm_entry("claude-cli", entry(1, "failed"))
        health = health.with_llm_entry("claude-cli", entry(0, "ok", 2))
        health = health.with_llm_entry("openai", entry(0, "ok", 5))
        health = health.with_llm_entry("codex-cli", entry(0, "failed"))
        line = section(render(app_config, health=health), "Source health")[-1]
        assert line.split("\n")[0] == "Field tagging used the LLM provider openai."
        failed = HealthFile().with_llm_entry("claude-cli", entry(0, "failed"))
        line = section(render(app_config, health=failed), "Source health")[-1]
        assert line.split("\n")[0] == "Field tagging used rules only (no LLM)."

    def test_footer_links_the_site_and_mentions_the_owner(self, app_config: AppConfig) -> None:
        site = "https://fridrichmethod.github.io/seminars/"
        assert blocks(render(app_config, owner=" FridrichMethod "))[-3:] == [
            "---",
            f"Full list and calendar subscriptions: {site}\n{zh('digest.footer.site', url=site)}",
            f"cc @FridrichMethod\n{zh('digest.footer.notify', mention='@FridrichMethod')}",
        ]
        for owner in (None, "", "  "):
            body = render(app_config, owner=owner)
            assert blocks(body)[-2] == "---"
            assert "@" not in body


def parsed(body: str) -> tuple[list[Any], str]:
    """Tokens of the document, flattened, and its HTML, from a CommonMark parser with tables."""
    markdown_it = pytest.importorskip("markdown_it", reason="markdown-it-py is not installed")
    parser = markdown_it.MarkdownIt("commonmark").enable(["table", "strikethrough"])
    tokens = []
    for token in parser.parse(body):
        tokens.extend([token, *(token.children or [])])
    return tokens, parser.render(body)


def shortened(text: str, limit: int) -> str:
    """What a cell keeps of a long text: a cut marked with three dots."""
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def hostile_event(text: str, uid: str = "hostile") -> Event:
    return make_event(
        uid=uid,
        title=text,
        speakers=[{"name": text, "affiliation": text}],
        series=text,
        host_org=text,
        location=text,
        picks_score=9.0,
        picks_reasons=[text],
        first_seen=PREVIOUS_ISSUE + timedelta(days=1),
    )


def hostile_document(config: AppConfig, text: str) -> str:
    config = dataclasses.replace(config, sources=(make_source(name=text),))
    health = ran(HealthFile(), "sites-bioengineering", "failed", error=text)
    health = health.with_llm_entry(text, LLMHealthEntry(at=REFERENCE_NOW, status="ok", calls=1))
    return render(config, [hostile_event(text)], health, previous_issue_at=PREVIOUS_ISSUE)


class TestUntrustedText:
    @pytest.mark.parametrize("text", HOSTILE)
    def test_hostile_text_is_rendered_as_literal_text(
        self, text: str, app_config: AppConfig
    ) -> None:
        body = hostile_document(app_config, text)
        tokens, rendered = parsed(body)
        kinds = [token.type for token in tokens]
        expected = " ".join(text.split())
        assert kinds.count("heading_open") == 11
        assert kinds.count("table_open") == 4  # picks, biology, new, health
        assert kinds.count("hr") == 1
        allowed = {"heading", "paragraph", "table", "thead", "tbody", "tr", "th", "td", "link"}
        structure = {kind.rsplit("_", 1)[0] for kind in kinds if kind.endswith(("_open", "_close"))}
        assert structure <= allowed
        assert not {"image", "html_block", "fence", "code_block", "em_open", "strong_close"} & (
            set(kinds) - {"strong_close"}
        )
        assert {token.content for token in tokens if token.type == "html_inline"} <= {"<br>"}
        links = [token.attrs["href"] for token in tokens if token.type == "link_open"]
        assert links == [DETAIL_URL] * 3
        cells = re.findall(r"<td>(.*?)</td>", rendered, re.DOTALL)
        assert len(cells) == 8 + 6 + 7 + 5
        shown = [html.unescape(re.sub(r"</?code>", "", cell)) for cell in cells]
        assert shown[4] == expected  # the location, in the picks table
        assert shown[2].split("<br>") == [expected, expected]  # title and series
        assert shown[3] == shortened(f"{expected} ({expected})", SPEAKERS_MAX_CHARS)
        assert shown[6] == expected  # why picked
        assert (shown[-5], shown[-1]) == (expected, expected)  # name and error of the source

    @pytest.mark.parametrize("text", HOSTILE)
    def test_what_github_acts_on_is_inside_code(self, text: str, app_config: AppConfig) -> None:
        body = hostile_document(app_config, text)
        _, rendered = parsed(body)
        outside = re.sub(r"<code>.*?</code>", "", rendered, flags=re.DOTALL)
        outside = re.sub(r'<a href="[^"]*">[^<]*</a>', "", outside)
        outside = html.unescape(
            re.sub(r"https://fridrichmethod\.github\.io/seminars/", "", outside)
        )
        outside = re.sub(r"\b\d\d:\d\d(?:-\d\d:\d\d)?\b", "", outside)  # the time cells
        assert "@" not in outside
        assert "$" not in outside
        assert not re.search(r"#\d|GH-\d|://|www\.|:[a-z0-9_+-]+:", outside, re.IGNORECASE)

    def test_a_row_cannot_be_ended_or_extended(self, app_config: AppConfig) -> None:
        for text in HOSTILE:
            body = hostile_document(app_config, text)
            for part in (part for part in blocks(body) if part.startswith("|")):
                lines = part.split("\n")
                columns = len(re.split(r"(?<!\\)\|", lines[0])) - 2
                assert columns in (5, 6, 7, 8)
                assert all(len(re.split(r"(?<!\\)\|", line)) - 2 == columns for line in lines)
                assert all(line.startswith("| ") and line.endswith(" |") for line in lines)

    def test_a_link_goes_to_the_detail_page_and_nowhere_else(self, app_config: AppConfig) -> None:
        url = 'https://example.org/a b)](<x>)|c`d\\e"?q=1&r=<s>'
        cell = one_row(app_config, make_event(url=url))[-1]
        assert cell == (
            f"[{both('digest.labels.details')}]"
            "(<https://example.org/a%20b)](%3Cx%3E)%7Cc%60d/e%22?q=1&r=%3Cs%3E>)"
        )
        tokens, _ = parsed(render(app_config, [make_event(url=url)]))
        (link,) = [token.attrs["href"] for token in tokens if token.type == "link_open"]
        assert link.startswith("https://example.org/a%20b)%5D(%3Cx%3E)")
        long_url = "https://example.org/" + "a" * 1000
        assert one_row(app_config, make_event(url=long_url))[-1] == "-"

    def test_emoji_and_decorative_characters_never_reach_the_document(
        self, app_config: AppConfig
    ) -> None:
        body = hostile_document(app_config, DIRTY)
        assert decorative_characters(body) == []
        assert "| Launch day fine |" in body

    def test_the_body_always_fits_into_an_issue(self, app_config: AppConfig) -> None:
        text = "&<>|`*_[]~\\ " * 40
        events = [
            hostile_event(text, uid=f"e{i:03}").evolve(fields=list(FIELD_IDS)) for i in range(120)
        ]
        body = render(app_config, events, previous_issue_at=PREVIOUS_ISSUE)
        assert len(body) <= MAX_BODY_CHARS < 65536
        tables = [part for part in blocks(body) if part.startswith("|")]
        assert len(tables) == 9
        assert len({len(rows(table)) for table in tables}) == 1
        assert f"{120 - len(rows(tables[0]))} more" in body

    @given(events=event_lists())
    @settings(
        max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    def test_any_events_give_a_well_formed_document(
        self, events: list[Event], app_config: AppConfig
    ) -> None:
        week = date(2027, 6, 7)
        body = render(app_config, events, week_start=week, previous_issue_at=PREVIOUS_ISSUE)
        assert body == render(
            app_config, events[::-1], week_start=week, previous_issue_at=PREVIOUS_ISSUE
        )
        assert decorative_characters(body) == []
        assert len(body) <= MAX_BODY_CHARS
        tokens, _ = parsed(body)
        kinds = [token.type for token in tokens]
        assert kinds.count("heading_open") == 11
        assert not {
            "image",
            "html_block",
            "fence",
            "code_block",
            "em_open",
            "bullet_list_open",
        } & set(kinds)
        assert {token.content for token in tokens if token.type == "html_inline"} <= {"<br>"}
        links = {token.attrs["href"] for token in tokens if token.type == "link_open"}
        assert all(link.startswith("https://") and ".example.edu/" in link for link in links)


class TestPublishing:
    def publish(
        self,
        config: AppConfig,
        issues: FakeIssueClient | None,
        now: datetime = MONDAY_NOON,
        **more: Any,
    ) -> Any:
        events = more.pop("events", [make_event()])
        return publish_digest(events, HealthFile(), issues, config=config, now=now, **more)

    @pytest.mark.parametrize(
        ("now", "monday"),
        [
            (REFERENCE_NOW, "2026-11-09"),
            (datetime(2026, 11, 16, 7, 59, tzinfo=UTC), "2026-11-09"),  # still Sunday in California
            (datetime(2026, 11, 16, 8, 0, tzinfo=UTC), "2026-11-16"),
            (at(11, 22, 23, 59), "2026-11-16"),
            (at(11, 23, 0), "2026-11-23"),
        ],
    )
    def test_the_week_is_the_local_week_that_contains_now(
        self, now: datetime, monday: str, app_config: AppConfig
    ) -> None:
        outcome = self.publish(app_config, None, now)
        assert outcome.document.title == f"Seminars - week of {monday}"
        assert outcome.document.week_start == date.fromisoformat(monday)

    def test_a_dry_run_writes_nothing(
        self, app_config: AppConfig, fake_issues: FakeIssueClient
    ) -> None:
        fake_issues.seed(
            "Seminars - week of 2026-11-09", labels=["digest"], created_at=PREVIOUS_ISSUE
        )
        outcome = self.publish(app_config, fake_issues, dry_run=True, owner="octocat")
        assert (outcome.action, outcome.issue_number, outcome.closed_issues) == (
            "dry_run",
            None,
            [],
        )
        assert {name for name, _ in fake_issues.calls} <= {"find_by_label", "find_by_title"}
        assert [issue.state for issue in fake_issues.issues] == ["open"]
        expected = render(
            app_config, [make_event()], previous_issue_at=PREVIOUS_ISSUE, owner="octocat"
        )
        assert outcome.document.body == expected
        assert "cc @octocat" in expected

    def test_without_a_client_the_document_is_still_rendered(self, app_config: AppConfig) -> None:
        outcome = self.publish(app_config, None)
        assert outcome.action == "dry_run"
        assert outcome.document.body == render(app_config, [make_event()])
        assert "First issue: everything is new." in outcome.document.body

    def test_the_issue_is_opened_and_the_earlier_ones_are_closed(
        self, app_config: AppConfig, fake_issues: FakeIssueClient
    ) -> None:
        old = fake_issues.seed(
            "Seminars - week of 2026-11-02",
            labels=["digest"],
            created_at=PREVIOUS_ISSUE - timedelta(days=7),
        )
        last = fake_issues.seed(
            "Seminars - week of 2026-11-09", labels=["digest"], created_at=PREVIOUS_ISSUE
        )
        done = fake_issues.seed(
            "Seminars - week of 2026-10-26",
            labels=["digest"],
            state="closed",
            created_at=PREVIOUS_ISSUE - timedelta(days=14),
        )
        other = fake_issues.seed("Source broken: x", labels=["source-broken"])
        outcome = self.publish(app_config, fake_issues, owner="octocat")
        created = fake_issues.get(5)
        assert (outcome.action, outcome.issue_number) == ("created", 5)
        assert outcome.closed_issues == [last.number, old.number]
        assert (created.title, created.labels) == ("Seminars - week of 2026-11-16", ["digest"])
        assert created.body == outcome.document.body
        assert created.body == render(
            app_config, [make_event()], previous_issue_at=PREVIOUS_ISSUE, owner="octocat"
        )
        states = [
            fake_issues.get(issue.number).state for issue in (old, last, done, other, created)
        ]
        assert states == ["closed", "closed", "closed", "open", "open"]
        assert fake_issues.labels == {"digest": LABEL_DIGEST}
        assert fake_issues.comments == {}
        written = [
            name for name, _ in fake_issues.calls if name in ("ensure_label", "open", "close")
        ]
        assert written == ["ensure_label", "open", "close", "close"]

    def test_a_week_is_posted_once(
        self, app_config: AppConfig, fake_issues: FakeIssueClient
    ) -> None:
        fake_issues.seed(
            "Seminars - week of 2026-11-09", labels=["digest"], created_at=PREVIOUS_ISSUE
        )
        first = self.publish(app_config, fake_issues)
        calls = len(fake_issues.calls)
        later = MONDAY_NOON + timedelta(days=1, hours=3)  # the catch-up run on Tuesday
        second = self.publish(app_config, fake_issues, later, events=[make_event(title="Changed")])
        assert (first.action, second.action) == ("created", "exists")
        assert second.issue_number == first.issue_number
        assert second.closed_issues == []
        assert len(fake_issues.issues) == 2
        assert {name for name, _ in fake_issues.calls[calls:]} == {"find_by_label", "find_by_title"}
        assert fake_issues.get(2).body == first.document.body
        # what is new is still counted from the issue of the week before, not from this one
        changed = render(
            app_config, [make_event(title="Changed")], previous_issue_at=PREVIOUS_ISSUE
        )
        assert second.document.body == changed
        assert section(changed, "New since last issue")[0].count("Changed") == 1

    def test_a_closed_issue_of_this_week_counts_too(
        self, app_config: AppConfig, fake_issues: FakeIssueClient
    ) -> None:
        fake_issues.seed("Seminars - week of 2026-11-16", labels=["digest"], state="closed")
        outcome = self.publish(app_config, fake_issues)
        assert (outcome.action, outcome.issue_number) == ("exists", 1)
        assert len(fake_issues.issues) == 1
        assert "First issue: everything is new." in outcome.document.body

    def test_the_next_week_gets_its_own_issue(
        self, app_config: AppConfig, fake_issues: FakeIssueClient
    ) -> None:
        first = self.publish(app_config, fake_issues)
        second = self.publish(app_config, fake_issues, MONDAY_NOON + timedelta(days=7))
        assert (first.action, second.action) == ("created", "created")
        assert second.document.title == "Seminars - week of 2026-11-23"
        assert second.closed_issues == [first.issue_number]

    def test_now_must_be_timezone_aware(self, app_config: AppConfig) -> None:
        with pytest.raises(ValueError, match="now must be timezone-aware"):
            self.publish(app_config, None, datetime(2026, 11, 16, 12, 0))
