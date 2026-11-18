"""The section of the digest with the deadlines of the next DEADLINE_DAYS days."""

from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from deadline_samples import SYMPOSIUM_DATES_URL, make_deadline
from factories import make_event
from fakes import FakeIssueClient
from seminars import cli
from seminars.config import AppConfig
from seminars.models import Deadline, HealthFile, dump_deadlines
from seminars.outputs.digest import DEADLINE_DAYS, DEADLINE_ROWS, publish_digest, render_digest
from test_digest import (
    MONDAY_NOON,
    WEEK,
    blocks,
    both,
    rows,
    section,
    zh,
)

HEADING = both("digest.sections.deadlines", days=DEADLINE_DAYS)
PAGE = f"[{both('digest.labels.organizer')}](<{SYMPOSIUM_DATES_URL}>)"
"""The link of a deadline of the symposium: the page that states its dates."""


def render(config: AppConfig, deadlines: list[Deadline], **kwargs: Any) -> str:
    document = render_digest(
        kwargs.pop("events", []),
        HealthFile(),
        config=config,
        week_start=WEEK,
        previous_issue_at=None,
        deadlines=deadlines,
        **kwargs,
    )
    return document.body


def on(day: date, number: int = 1, **overrides: Any) -> Deadline:
    """A deadline of the symposium on ``day``; ``number`` tells the rounds apart."""
    uid = "dl.example-symposium-2027.paper" + ("" if number == 1 else f"-{number}")
    values: dict[str, Any] = {"uid": uid, "kind": "paper", "label": f"Round {number}"}
    return make_deadline(date=day, **{**values, **overrides})


def headings(body: str) -> list[str]:
    return [line for line in body.split("\n") if line.startswith("## ")]


class TestSection:
    def test_the_deadlines_of_the_next_days_after_the_meetings(self, app_config: AppConfig) -> None:
        abstract = make_deadline(
            uid="dl.example-symposium-2027.abstract",
            kind="abstract",
            label="Short talk abstract deadline",
            date=date(2026, 12, 3),
            time="23:59",
            zone="MT",
        )
        body = render(app_config, [abstract, make_deadline()])
        listed = headings(body)
        assert (
            listed.index(f"## {HEADING}")
            == listed.index(f"## {both('digest.sections.meetings')}") + 1
        )
        chinese = "\u672a\u6765 21 \u5929\u5185\u7684\u622a\u6b62\u65e5\u671f"
        assert f"Deadlines in the next 21 days / {chinese}" == HEADING
        note, table = section(body, HEADING)
        assert note == f"{LOCALE_NOTE}\n{zh('digest.labels.deadlines_note')}"
        header = table.split("\n")[0]
        assert header == (
            f"| {both('digest.table.date')} | {both('digest.table.time')} | "
            f"{both('digest.table.deadline')} | {both('digest.table.link')} |"
        )
        assert rows(table) == [
            ["2026-11-20", "AoE", "Example Symposium 2027: Scholarship deadline", PAGE],
            [
                "2026-12-03",
                "23:59 MT",
                "Example Symposium 2027: Short talk abstract deadline",
                PAGE,
            ],
        ]

    def test_a_day_is_in_from_the_monday_for_21_days(self, app_config: AppConfig) -> None:
        days = [
            WEEK - timedelta(days=1),
            WEEK,
            WEEK + timedelta(days=20),
            WEEK + timedelta(days=21),
        ]
        deadlines = [on(day, number) for number, day in enumerate(days, 1)]
        (_, table) = section(render(app_config, deadlines), HEADING)
        assert [cells[0] for cells in rows(table)] == ["2026-11-16", "2026-12-06"]

    def test_no_deadline_in_those_days_gives_no_section(self, app_config: AppConfig) -> None:
        later = on(WEEK + timedelta(days=DEADLINE_DAYS))
        with_none = render(app_config, [])
        assert render(app_config, [later]) == with_none
        assert HEADING not in with_none

    def test_the_qualifier_by_and_nothing_stated_are_said_as_such(
        self, app_config: AppConfig
    ) -> None:
        by_day = on(WEEK + timedelta(days=2), 1, zone=None, qualifier="by")
        by_time = on(WEEK + timedelta(days=3), 2, zone="PT", time="10:00", qualifier="by")
        plain = on(WEEK + timedelta(days=4), 3, zone=None)
        (_, table) = section(render(app_config, [by_day, by_time, plain]), HEADING)
        assert [cells[1] for cells in rows(table)] == [
            f"by this day<br>{zh('deadlines.when.by_day')}",
            f"by 10:00 PT<br>{zh('deadlines.when.by', when='10:00 PT')}",
            "-",
        ]

    @pytest.mark.parametrize("left", [1, 5])
    def test_the_table_has_a_limit_of_its_own(self, app_config: AppConfig, left: int) -> None:
        count = DEADLINE_ROWS + left
        deadlines = [on(WEEK + timedelta(days=n % 21), n) for n in range(1, count + 1)]
        talks = [make_event(uid=f"t{i:02}") for i in range(3)]
        (_, table, more) = section(render(app_config, deadlines, events=talks), HEADING)
        assert len(rows(table)) == DEADLINE_ROWS == 25
        # Without a noun, as below the other tables: the line reads well for one as well.
        assert more == f"{left} more\n{zh('digest.labels.more_deadlines', count=left)}"

    def test_the_texts_of_a_deadline_are_literal(self, app_config: AppConfig) -> None:
        hostile = on(WEEK, label="Talks of @octocat for #12 | *all* <b>x</b>")
        (_, table) = section(render(app_config, [hostile]), HEADING)
        (cells,) = rows(table)
        assert cells[2] == (
            "Example Symposium 2027: Talks of `@octocat` for `#12` "
            "\\| \\*all\\* &lt;b&gt;x&lt;/b&gt;"
        )

    def test_the_other_sections_stay_as_they_are(self, app_config: AppConfig) -> None:
        body = render(app_config, [make_deadline()])
        without = render(app_config, [])
        assert len(headings(body)) == len(headings(without)) + 1
        kept = [part for part in blocks(body) if part not in section(body, HEADING)]
        assert [part for part in kept if not part.startswith(f"## {HEADING}")] == blocks(without)


class TestPublish:
    def test_the_publisher_hands_the_deadlines_to_the_renderer(
        self, app_config: AppConfig, fake_issues: FakeIssueClient
    ) -> None:
        outcome = publish_digest(
            [],
            HealthFile(),
            fake_issues,
            config=app_config,
            now=MONDAY_NOON,
            dry_run=True,
            deadlines=[make_deadline()],
        )
        assert outcome.action == "dry_run"
        assert HEADING in outcome.document.body


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project_with_a_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch project whose data/ holds one deadline of WEEK, with the clock at MONDAY_NOON.

    The command reads the clock once and lists the deadlines from the Monday of that
    week; a clock of its own keeps a Sunday midnight from falling between the day of
    the deadline and the week of the digest.
    """
    root = tmp_path / "project"
    (root / "config" / "sources.d").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "config" / "settings.yaml", root / "config" / "settings.yaml")
    shutil.copytree(REPO_ROOT / "locales", root / "locales")
    for name in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_REPOSITORY_OWNER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "current_time", lambda: MONDAY_NOON)
    (root / "data").mkdir()
    (root / "data" / "deadlines.json").write_text(
        dump_deadlines([make_deadline()]), encoding="utf-8"
    )
    monkeypatch.chdir(root)
    return root


@pytest.mark.usefixtures("project_with_a_deadline")
def test_the_digest_command_lists_the_stored_deadlines() -> None:
    result = CliRunner().invoke(cli.app, ["digest", "--dry-run"])
    assert result.exit_code == 0, result.output
    body = result.stdout_bytes.decode("utf-8")
    assert body.startswith(f"Issue title: Seminars - week of {WEEK.isoformat()}\n")
    assert f"\n## {HEADING}\n" in body


LOCALE_NOTE = (
    "Kept by hand from the organizers' pages; check the page before you rely on a date. "
    "Times and zones are as the page states them."
)
