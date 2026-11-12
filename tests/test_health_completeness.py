"""Tests for the health of a source that delivers events but is not read in full.

Pages that could not be fetched, or a listing far smaller than usual, are
recorded with the run. Three such runs in a row open a source-broken
issue: while they last, the events the source does not deliver are kept,
so somebody should look. A full read closes it.
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
    record_run,
    source_issue_title,
    source_verdict,
    update_health,
)

SOURCE = "bayarea-slac"
TITLE = source_issue_title(SOURCE)


@pytest.fixture
def config(app_config: AppConfig) -> AppConfig:
    source = make_source(id=SOURCE, group="bayarea", url="https://slac.example.org/events")
    return dataclasses.replace(app_config, sources=(source,))


def result(status: SourceStatus = "ok", **extra: Any) -> SourceRunResult:
    values: dict[str, Any] = {"source_id": SOURCE, "status": status}
    if status == "ok":
        values |= {"events": 3, "drafts": 4}
    if status == "failed":
        values["error"] = "HttpStatusError: HTTP 500"
    return SourceRunResult(**{**values, **extra})


SKIPPED: dict[str, Any] = {"skipped_pages": 1, "drafts": 3}
SHRUNK: dict[str, Any] = {"shrank_from": 56, "drafts": 7}


class Runs:
    def __init__(self, config: AppConfig, issues: FakeIssueClient) -> None:
        self.config = config
        self.issues = issues
        self.health = HealthFile()

    def run(self, *results: SourceRunResult) -> None:
        number = len(self.health.sources[SOURCE].history) if SOURCE in self.health.sources else 0
        moment = REFERENCE_NOW + timedelta(days=number)
        report = RunReport(
            started_at=moment, finished_at=moment, mode="live", ok=True, sources=list(results)
        )
        self.health = update_health(self.health, report, self.issues, config=self.config)

    def open_titles(self) -> list[str]:
        return sorted(issue.title for issue in self.issues.issues if issue.state == "open")

    def verdict(self) -> str:
        return source_verdict(self.health.sources[SOURCE], self.config.settings.health)


@pytest.fixture
def runs(config: AppConfig, fake_issues: FakeIssueClient) -> Runs:
    return Runs(config, fake_issues)


class TestHistory:
    def test_the_entry_keeps_what_was_missing(self) -> None:
        moment = REFERENCE_NOW
        report = RunReport(
            started_at=moment,
            finished_at=moment,
            mode="live",
            ok=True,
            sources=[result(**SKIPPED, invalid=1), result("failed", source_id="sites-y")],
            llm=LLMUsage(),
        )
        health = record_run(HealthFile(), report)
        (entry,) = health.sources[SOURCE].history
        assert (entry.status, entry.accepted, entry.skipped_pages, entry.shrank_from) == (
            "ok",
            2,
            1,
            None,
        )
        assert not entry.read_in_full
        assert health.sources["sites-y"].history[0].accepted is None

    def test_a_full_read_records_its_size(self) -> None:
        moment = REFERENCE_NOW
        report = RunReport(
            started_at=moment, finished_at=moment, mode="live", ok=True, sources=[result()]
        )
        (entry,) = record_run(HealthFile(), report).sources[SOURCE].history
        assert (entry.accepted, entry.read_in_full) == (4, True)


class TestIncompleteSources:
    def test_three_partial_reads_open_an_issue(
        self, runs: Runs, fake_issues: FakeIssueClient
    ) -> None:
        runs.run(result())
        runs.run(result(**SKIPPED))
        runs.run(result(**SHRUNK))
        assert runs.open_titles() == []
        assert runs.verdict() == "unsettled"
        runs.run(result(**SKIPPED))
        assert runs.open_titles() == [TITLE]
        assert runs.verdict() == "incomplete"
        body = fake_issues.get(1).body
        assert "`bayarea-slac` has delivered events but was not read in full in its last 3" in body
        assert "- Last run: 1 page(s) could not be fetched" in body
        assert "the events it does not deliver are kept" in body
        assert body.endswith("closed automatically when the source is read in full again.\n")
        assert body.isascii()

    def test_a_shrunk_listing_is_described(self, runs: Runs, fake_issues: FakeIssueClient) -> None:
        for _ in range(3):
            runs.run(result(**SHRUNK))
        assert "- Last run: 7 usable draft(s) where it usually has 56" in fake_issues.get(1).body

    def test_a_full_read_closes_the_issue(self, runs: Runs, fake_issues: FakeIssueClient) -> None:
        for _ in range(3):
            runs.run(result(**SKIPPED))
        runs.run(result())
        assert runs.open_titles() == []
        assert fake_issues.comments == {1: ["The source is read in full again."]}

    def test_a_partial_read_does_not_close_a_failing_issue(self, runs: Runs) -> None:
        for _ in range(3):
            runs.run(result("failed"))
        assert runs.open_titles() == [TITLE]
        runs.run(result(**SKIPPED))
        assert runs.open_titles() == [TITLE]
        runs.run(result())
        assert runs.open_titles() == []
        assert runs.issues.comments == {1: ["The source delivers events again."]}

    def test_failures_in_between_start_the_count_again(self, runs: Runs) -> None:
        runs.run(result(**SKIPPED))
        runs.run(result(**SKIPPED))
        runs.run(result("failed"))
        runs.run(result(**SKIPPED))
        assert runs.open_titles() == []
