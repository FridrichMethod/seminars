"""Tests for the health of a source whose page may legitimately list nothing (allow_empty).

bayarea-biohub lists its upcoming events in a block that is often empty
between events. Its entry says ``allow_empty: true``: a run that parsed no
item without a ParseError found a page that works, so it is healthy and
never opens a source-broken issue. Without the flag, the same runs are
empty and open one after three runs.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

import pytest

from factories import REFERENCE_NOW, make_source
from fakes import FakeIssueClient
from seminars.config import AppConfig
from seminars.models import HealthFile, LLMUsage, RunReport, SourceRunResult, SourceStatus
from seminars.outputs.health import (
    allows_empty,
    health_status,
    record_run,
    source_issue_title,
    update_health,
)

QUIET = "bayarea-quiet"
PLAIN = "bayarea-plain"


@pytest.fixture
def config(app_config: AppConfig) -> AppConfig:
    sources = (
        make_source(
            id=QUIET,
            group="bayarea",
            adapter="html_list",
            url="https://quiet.example.org/events/",
            params={"allow_empty": True},
        ),
        make_source(
            id=PLAIN, group="bayarea", adapter="html_list", url="https://plain.example.org/events/"
        ),
    )
    return dataclasses.replace(app_config, sources=sources)


def result(source_id: str, status: SourceStatus, **extra: Any) -> SourceRunResult:
    values: dict[str, Any] = {"events": 1 if status == "ok" else 0, "drafts": 0}
    if status == "ok":
        values["drafts"] = 1
    if status == "failed":
        values["error"] = "ParseError: selectors.item matches nothing"
    return SourceRunResult(source_id=source_id, status=status, **{**values, **extra})


def report(number: int, *results: SourceRunResult) -> RunReport:
    moment = REFERENCE_NOW + timedelta(days=number)
    return RunReport(
        started_at=moment,
        finished_at=moment,
        mode="live",
        ok=True,
        sources=list(results),
        llm=LLMUsage(),
    )


def play(
    config: AppConfig,
    issues: FakeIssueClient,
    source_id: str,
    statuses: list[SourceStatus],
    health: HealthFile | None = None,
) -> HealthFile:
    current = health or HealthFile()
    for status in statuses:
        number = len(current.sources[source_id].history) if source_id in current.sources else 0
        current = update_health(
            current, report(number, result(source_id, status)), issues, config=config
        )
    return current


def open_titles(issues: FakeIssueClient) -> list[str]:
    return sorted(issue.title for issue in issues.issues if issue.state == "open")


class TestAllowEmpty:
    def test_the_flag_is_read_from_the_params(self, config: AppConfig) -> None:
        assert [allows_empty(source) for source in config.sources] == [True, False]

    def test_a_page_without_items_is_healthy(self, config: AppConfig) -> None:
        assert health_status(result(QUIET, "empty"), allow_empty=True) == "ok"
        assert health_status(result(QUIET, "empty")) == "empty"

    def test_three_quiet_runs_open_no_issue(
        self, config: AppConfig, fake_issues: FakeIssueClient
    ) -> None:
        health = play(config, fake_issues, QUIET, ["ok", "empty", "empty", "empty", "empty"])
        assert open_titles(fake_issues) == []
        history = health.sources[QUIET]
        assert [entry.status for entry in history.history] == ["ok"] * 5
        assert history.last_success_at == REFERENCE_NOW + timedelta(days=4)

    def test_an_open_issue_is_closed_by_a_quiet_run(
        self, config: AppConfig, fake_issues: FakeIssueClient
    ) -> None:
        health = play(config, fake_issues, QUIET, ["failed", "failed", "failed"])
        assert open_titles(fake_issues) == [source_issue_title(QUIET)]
        play(config, fake_issues, QUIET, ["empty"], health)
        assert open_titles(fake_issues) == []
        assert fake_issues.comments == {1: ["The source delivers events again."]}

    def test_without_the_flag_the_same_runs_open_an_issue(
        self, config: AppConfig, fake_issues: FakeIssueClient
    ) -> None:
        play(config, fake_issues, PLAIN, ["ok", "empty", "empty", "empty"])
        assert open_titles(fake_issues) == [source_issue_title(PLAIN)]

    def test_a_page_that_is_gone_still_fails(
        self, config: AppConfig, fake_issues: FakeIssueClient
    ) -> None:
        play(config, fake_issues, QUIET, ["ok", "failed", "failed", "failed"])
        assert open_titles(fake_issues) == [source_issue_title(QUIET)]

    def test_items_that_all_break_are_still_empty(self, config: AppConfig) -> None:
        broken = result(QUIET, "empty", drafts=3, invalid=3)
        health = record_run(HealthFile(), report(1, broken), allow_empty={QUIET})
        assert health.sources[QUIET].history[0].status == "empty"
