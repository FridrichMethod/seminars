"""Tests for health tracking: the run history and the issues that follow from it."""

from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta
from typing import Any

import pytest

from factories import REFERENCE_NOW, make_source
from fakes import FakeIssueClient
from seminars.config import AppConfig
from seminars.contracts import (
    LABEL_LLM_AUTH,
    LABEL_SOURCE_BROKEN,
    HealthUpdater,
    load_implementation,
)
from seminars.models import (
    HEALTH_HISTORY_LIMIT,
    HealthFile,
    LLMFailureKind,
    LLMProviderResult,
    LLMProviderStatus,
    LLMUsage,
    RunReport,
    SourceHealth,
    SourceRunResult,
    SourceStatus,
)
from seminars.outputs.health import (
    llm_failures,
    llm_issue_body,
    llm_issue_title,
    llm_recovered,
    record_run,
    source_issue_body,
    source_issue_title,
    source_verdict,
    update_health,
)

SOURCE = "sites-x"
TITLE = source_issue_title(SOURCE)


@pytest.fixture
def config(app_config: AppConfig) -> AppConfig:
    sources = (
        make_source(id=SOURCE, name="Example Department", url="https://x.example.edu/events"),
        make_source(id="sites-y", url="https://y.example.edu/events"),
        make_source(id="sites-off", url="https://off.example.edu/events", enabled=False),
    )
    return dataclasses.replace(app_config, sources=sources)


def source_result(status: SourceStatus, source_id: str = SOURCE, **extra: Any) -> SourceRunResult:
    events = extra.pop("events", 5 if status == "ok" else 0)
    error = extra.pop("error", "HttpStatusError: HTTP 500" if status == "failed" else None)
    return SourceRunResult(source_id=source_id, status=status, events=events, error=error, **extra)


def provider_result(
    status: LLMProviderStatus, provider: str = "claude-cli", kind: LLMFailureKind | None = "auth"
) -> LLMProviderResult:
    failed = status == "failed"
    return LLMProviderResult(
        provider=provider,
        status=status,
        calls=0 if status == "unused" else 3,
        failures=1 if failed else 0,
        failure_kind=kind if failed else None,
        error="credential rejected" if failed else None,
    )


def report(
    number: int,
    *sources: SourceRunResult,
    providers: tuple[LLMProviderResult, ...] = (),
    dry_run: bool = False,
) -> RunReport:
    moment = REFERENCE_NOW + timedelta(days=number)
    return RunReport(
        started_at=moment,
        finished_at=moment,
        mode="live",
        ok=True,
        dry_run=dry_run,
        sources=list(sources),
        llm=LLMUsage(enabled=bool(providers), providers=list(providers)),
    )


class Runs:
    """Plays one run after another against a fake issue client."""

    def __init__(self, config: AppConfig, issues: FakeIssueClient | None) -> None:
        self.config = config
        self.issues = issues
        self.health = HealthFile()
        self.number = 0

    def source(self, *statuses: SourceStatus, **extra: Any) -> None:
        for status in statuses:
            self.run(source_result(status, **extra))

    def llm(self, *statuses: LLMProviderStatus, **extra: Any) -> None:
        for status in statuses:
            self.run(providers=(provider_result(status, **extra),))

    def run(self, *sources: SourceRunResult, **options: Any) -> None:
        self.number += 1
        self.health = update_health(
            self.health, report(self.number, *sources, **options), self.issues, config=self.config
        )

    def open_titles(self) -> list[str]:
        assert self.issues is not None
        return sorted(issue.title for issue in self.issues.issues if issue.state == "open")

    def calls(self, name: str) -> list[dict[str, Any]]:
        assert self.issues is not None
        return [arguments for method, arguments in self.issues.calls if method == name]


@pytest.fixture
def runs(config: AppConfig, fake_issues: FakeIssueClient) -> Runs:
    return Runs(config, fake_issues)


class TestHistory:
    def test_a_run_is_appended_for_every_source_and_provider(self) -> None:
        first = report(
            1,
            source_result("ok", events=7),
            source_result("failed", "sites-y"),
            providers=(provider_result("failed"), provider_result("unused", "anthropic")),
        )
        health = record_run(HealthFile(), first)
        (entry,) = health.sources[SOURCE].history
        assert (entry.at, entry.status, entry.events, entry.error) == (
            first.started_at,
            "ok",
            7,
            None,
        )
        assert health.sources[SOURCE].last_success_at == first.started_at
        failed = health.sources["sites-y"]
        assert failed.history[0].error == "HttpStatusError: HTTP 500"
        assert failed.last_success_at is None
        (llm_entry,) = health.llm["claude-cli"].history
        assert (llm_entry.status, llm_entry.calls, llm_entry.failure_kind, llm_entry.error) == (
            "failed",
            3,
            "auth",
            "credential rejected",
        )
        assert health.llm["anthropic"].history[0].status == "unused"

    def test_the_history_is_capped_and_the_last_success_survives(self) -> None:
        health = record_run(HealthFile(), report(0, source_result("ok")))
        for number in range(1, HEALTH_HISTORY_LIMIT + 5):
            health = record_run(health, report(number, source_result("failed")))
        history = health.sources[SOURCE]
        assert len(history.history) == HEALTH_HISTORY_LIMIT
        assert history.last_success_at == REFERENCE_NOW

    def test_the_input_is_not_changed(self) -> None:
        before = HealthFile()
        after = record_run(before, report(1, source_result("ok")))
        assert before == HealthFile()
        assert after != before

    def test_no_issue_client_still_records_the_run(self, config: AppConfig) -> None:
        runs = Runs(config, None)
        runs.source("failed", "failed", "failed")
        assert runs.health.sources[SOURCE].trailing("failed") == 3

    def test_a_source_that_parsed_events_outside_the_window_is_healthy(self) -> None:
        quiet = source_result("empty", drafts=4, outside_window=4)
        health = record_run(HealthFile(), report(1, quiet))
        (entry,) = health.sources[SOURCE].history
        assert (entry.status, entry.events) == ("ok", 0)
        assert health.sources[SOURCE].last_success_at == REFERENCE_NOW + timedelta(days=1)

    @pytest.mark.parametrize(
        "extra", [{}, {"drafts": 2, "invalid": 2}], ids=["nothing-parsed", "all-invalid"]
    )
    def test_a_source_that_parsed_no_usable_draft_is_empty(self, extra: dict[str, Any]) -> None:
        health = record_run(HealthFile(), report(1, source_result("empty", **extra)))
        assert health.sources[SOURCE].history[0].status == "empty"

    def test_it_is_the_registered_implementation(self) -> None:
        updater: HealthUpdater = load_implementation("health_update")
        assert updater is update_health


class TestQuietSources:
    def test_a_page_without_talks_in_the_window_is_never_reported(self, runs: Runs) -> None:
        runs.source("ok")
        for _ in range(6):
            runs.run(source_result("empty", drafts=3, outside_window=3))
        assert runs.open_titles() == []
        verdict = source_verdict(runs.health.sources[SOURCE], runs.config.settings.health)
        assert verdict == "ok"

    def test_a_page_that_parses_nothing_is_reported(self, runs: Runs) -> None:
        runs.source("ok", "empty", "empty", "empty")
        assert runs.open_titles() == [TITLE]


class TestSourceVerdict:
    def verdict(self, config: AppConfig, *statuses: SourceStatus) -> str:
        runs = Runs(config, None)
        runs.source(*statuses)
        return source_verdict(runs.health.sources[SOURCE], config.settings.health)

    @pytest.mark.parametrize(
        ("statuses", "expected"),
        [
            (["ok"], "ok"),
            (["failed", "failed"], "unsettled"),
            (["failed", "failed", "failed"], "failing"),
            (["ok", "failed", "failed", "failed"], "failing"),
            (["failed", "failed", "failed", "ok"], "ok"),
            (["ok", "empty", "empty"], "unsettled"),
            (["ok", "empty", "empty", "empty"], "empty"),
            (["empty", "empty", "empty", "empty"], "unsettled"),
            (["ok", "empty", "failed", "empty"], "failing"),
            (["empty", "failed", "empty"], "failing"),
            (["ok", "failed", "empty"], "unsettled"),
            (["failed", "failed", "failed", "empty"], "failing"),
        ],
    )
    def test_verdicts(self, config: AppConfig, statuses: list[SourceStatus], expected: str) -> None:
        assert self.verdict(config, *statuses) == expected

    def test_a_source_without_history_is_fine(self, config: AppConfig) -> None:
        assert source_verdict(SourceHealth(), config.settings.health) == "ok"


class TestSourceIssues:
    def test_no_issue_before_the_third_failure(self, runs: Runs) -> None:
        runs.source("failed", "failed")
        assert runs.open_titles() == []
        assert runs.calls("open") == []

    def test_three_failures_open_one_issue_with_the_label(
        self, runs: Runs, fake_issues: FakeIssueClient
    ) -> None:
        runs.source("failed", "failed", "failed")
        assert runs.open_titles() == [TITLE]
        (issue,) = fake_issues.issues
        assert issue.labels == ["source-broken"]
        assert fake_issues.labels == {"source-broken": LABEL_SOURCE_BROKEN}
        assert "`sites-x` has failed or delivered nothing in its last 3 runs" in issue.body
        assert "- Last healthy run: never" in issue.body
        assert "- Name: Example Department" in issue.body
        assert "- Page: <https://x.example.edu/events>" in issue.body
        assert "    HttpStatusError: HTTP 500" in issue.body
        assert "| 2026-11-13 20:00 UTC | failed | 0 |" in issue.body
        assert issue.body.isascii()

    def test_further_failures_update_the_same_issue(
        self, runs: Runs, fake_issues: FakeIssueClient
    ) -> None:
        runs.source("failed", "failed", "failed", "failed", "failed")
        assert len(fake_issues.issues) == 1
        assert len(runs.calls("open")) == 1
        assert len(runs.calls("update")) == 2
        assert "in its last 5 runs" in fake_issues.get(1).body

    def test_recovery_closes_the_issue_with_a_comment(
        self, runs: Runs, fake_issues: FakeIssueClient
    ) -> None:
        runs.source("failed", "failed", "failed", "ok")
        assert runs.open_titles() == []
        assert fake_issues.comments == {1: ["The source delivers events again."]}
        runs.source("ok")
        assert len(runs.calls("close")) == 1

    def test_a_source_that_had_events_and_is_empty_three_times(
        self, runs: Runs, fake_issues: FakeIssueClient
    ) -> None:
        runs.source("ok", "empty", "empty")
        assert runs.open_titles() == []
        runs.source("empty")
        assert runs.open_titles() == [TITLE]
        body = fake_issues.get(1).body
        assert "has parsed no events in its last 3 runs" in body
        assert "- Last healthy run: 2026-11-11 20:00 UTC" in body
        assert "Error of the last run" not in body

    def test_a_source_that_never_had_events_is_not_reported_for_being_empty(
        self, runs: Runs
    ) -> None:
        runs.source("empty", "empty", "empty", "empty", "empty")
        assert runs.open_titles() == []

    def test_an_empty_run_does_not_close_an_issue(self, runs: Runs) -> None:
        runs.source("failed", "failed", "failed", "empty")
        assert runs.open_titles() == [TITLE]

    def test_one_issue_per_source(self, runs: Runs) -> None:
        for _ in range(3):
            runs.run(source_result("failed"), source_result("failed", "sites-y"))
        assert runs.open_titles() == [TITLE, source_issue_title("sites-y")]
        runs.run(source_result("ok"), source_result("failed", "sites-y"))
        assert runs.open_titles() == [source_issue_title("sites-y")]

    def test_a_partial_run_leaves_the_other_sources_alone(
        self, runs: Runs, fake_issues: FakeIssueClient
    ) -> None:
        runs.source("failed", "failed", "failed")
        runs.run(source_result("ok", "sites-y"))
        assert runs.open_titles() == [TITLE]
        assert fake_issues.get(1).body.count("| failed |") == 3

    def test_the_issue_of_a_disabled_or_removed_source_is_closed(
        self, runs: Runs, fake_issues: FakeIssueClient
    ) -> None:
        fake_issues.seed(source_issue_title("sites-off"), labels=["source-broken"])
        fake_issues.seed(source_issue_title("sites-gone"), labels=["source-broken"])
        fake_issues.seed("Something else", labels=["source-broken"])
        runs.source("ok")
        assert runs.open_titles() == ["Something else"]
        assert fake_issues.comments[1] == ["The source was disabled or removed."]

    def test_an_issue_that_exists_already_is_reused(
        self, runs: Runs, fake_issues: FakeIssueClient
    ) -> None:
        fake_issues.seed(TITLE, body="older text", labels=["source-broken"])
        runs.source("failed", "failed", "failed")
        assert len(fake_issues.issues) == 1
        assert "in its last 3 runs" in fake_issues.get(1).body

    def test_an_unchanged_body_is_not_sent_again(self, runs: Runs) -> None:
        runs.source("failed", "failed", "failed")
        runs.health = runs.health.evolve(
            sources={SOURCE: runs.health.sources[SOURCE].evolve(history=[])}
        )
        runs.number = 0
        runs.source("failed", "failed", "failed")
        assert len(runs.calls("update")) == 0

    def test_a_hostile_error_text_stays_inert(
        self, runs: Runs, fake_issues: FakeIssueClient
    ) -> None:
        hostile = "boom\n\n@everyone [x](https://evil.example) `code` \x1b[31m caf\u00e9"
        runs.source("failed", "failed", "failed", error=hostile)
        body = fake_issues.get(1).body
        assert "    boom\\n\\n@everyone [x](https://evil.example) `code` \\x1b[31m caf\\xe9" in body
        assert "\x1b" not in body
        assert body.isascii()
        assert not any(line.startswith("@everyone") for line in body.splitlines())

    def test_a_dry_run_touches_no_issue(
        self, config: AppConfig, fake_issues: FakeIssueClient
    ) -> None:
        runs = Runs(config, fake_issues)
        for _ in range(4):
            runs.run(source_result("failed"), dry_run=True)
        assert fake_issues.calls == []
        assert runs.health.sources[SOURCE].trailing("failed") == 4

    def test_the_body_of_a_source_that_is_not_configured(self) -> None:
        health = HealthFile()
        for number in range(3):
            health = record_run(health, report(number, source_result("failed")))
        body = source_issue_body(None, SOURCE, health.sources[SOURCE], "failing")
        assert "- Name:" not in body
        assert body.endswith("delivers events again.\n")


class TestLlmIssues:
    def test_two_failures_in_a_row_open_an_issue(
        self, runs: Runs, fake_issues: FakeIssueClient
    ) -> None:
        runs.llm("failed")
        assert runs.open_titles() == []
        runs.llm("failed")
        assert runs.open_titles() == [llm_issue_title("claude-cli")]
        (issue,) = fake_issues.issues
        assert issue.labels == ["llm-auth"]
        assert fake_issues.labels == {"llm-auth": LABEL_LLM_AUTH}
        assert (
            "`claude-cli` failed in 2 runs in a row (kind of the last failure: auth)" in issue.body
        )
        assert "- Credential: repository secret `CLAUDE_CODE_OAUTH_TOKEN`" in issue.body
        assert "- Create a new token first: `claude setup-token`" in issue.body
        assert "`scripts/set-llm-credential.sh claude-cli`" in issue.body
        assert "`gh secret set CLAUDE_CODE_OAUTH_TOKEN`" in issue.body
        assert "mode `llm-smoke`" in issue.body
        assert "    credential rejected" in issue.body
        assert issue.body.isascii()

    def test_runs_without_a_call_are_skipped(self, runs: Runs) -> None:
        runs.llm("failed", "unused", "unused", "failed")
        assert runs.open_titles() == [llm_issue_title("claude-cli")]
        runs.llm("unused")
        assert runs.open_titles() == [llm_issue_title("claude-cli")]

    def test_an_answer_closes_the_issue(self, runs: Runs, fake_issues: FakeIssueClient) -> None:
        runs.llm("failed", "failed", "failed", "ok")
        assert runs.open_titles() == []
        assert fake_issues.comments == {1: ["The provider answers again."]}
        assert len(runs.calls("update")) == 1

    def test_a_success_in_between_starts_the_count_again(self, runs: Runs) -> None:
        runs.llm("failed", "ok", "failed")
        assert runs.open_titles() == []

    def test_one_issue_per_provider(self, runs: Runs) -> None:
        both = (provider_result("failed"), provider_result("failed", "anthropic", "quota"))
        runs.run(providers=both)
        runs.run(providers=both)
        assert runs.open_titles() == [llm_issue_title("anthropic"), llm_issue_title("claude-cli")]

    @pytest.mark.parametrize(
        ("provider", "expected"),
        [
            ("anthropic", "repository secret `ANTHROPIC_API_KEY`"),
            ("openai", "repository secret `OPENAI_API_KEY`"),
            ("openai-compat", "repository secret `LLM_API_KEY`"),
            ("codex-cli", "`codex-cli` has no repository secret"),
        ],
    )
    def test_the_body_names_the_credential_of_the_provider(
        self, config: AppConfig, provider: str, expected: str
    ) -> None:
        runs = Runs(config, None)
        runs.llm("failed", "failed", provider=provider, kind="quota")
        body = llm_issue_body(provider, runs.health.llm[provider])
        assert expected in body
        assert "allowance or the prepaid credit is used up" in body
        assert "claude setup-token" not in body

    def test_counting_helpers(self, config: AppConfig) -> None:
        runs = Runs(config, None)
        runs.llm("ok", "failed", "unused", "failed")
        history = runs.health.llm["claude-cli"]
        assert (llm_failures(history), llm_recovered(history)) == (2, False)
        runs.llm("ok", "unused")
        history = runs.health.llm["claude-cli"]
        assert (llm_failures(history), llm_recovered(history)) == (0, True)
        runs.llm("unused", provider="openai")
        assert llm_recovered(runs.health.llm["openai"]) is False


class BrokenIssueClient(FakeIssueClient):
    """An issue client whose every listing fails, as during an outage of the API."""

    def find_by_label(self, label: str, *, state: Any = "open") -> Any:
        raise RuntimeError("API outage")


class TestApiFailure:
    def test_a_failing_issue_client_does_not_lose_the_history(
        self, config: AppConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        runs = Runs(config, BrokenIssueClient())
        with caplog.at_level(logging.WARNING):
            runs.source("failed", "failed", "failed")
            runs.llm("failed", "failed")
        assert runs.health.sources[SOURCE].trailing("failed") == 3
        assert llm_failures(runs.health.llm["claude-cli"]) == 2
        messages = [record.getMessage() for record in caplog.records]
        assert "source health issues were not updated (RuntimeError)" in messages
        assert "LLM health issues were not updated (RuntimeError)" in messages
        assert not any("API outage" in message for message in messages)
