"""Tests for the end of an llm-auth issue whose provider is not asked any more.

The chain asks a provider only when the ones before it fail, and a
provider leaves the chain when its credential is deleted or LLM_PROVIDERS
changes. Neither may leave its issue open for good.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from factories import REFERENCE_NOW
from fakes import FakeIssueClient
from seminars.config import AppConfig
from seminars.models import (
    HealthFile,
    LLMFailureKind,
    LLMProviderResult,
    LLMProviderStatus,
    LLMUsage,
    RunReport,
)
from seminars.outputs.health import llm_issue_body, llm_issue_title, update_health

NOT_IN_CHAIN = "The provider is no longer in the provider chain."
ANOTHER_ANSWERS = (
    "Another provider of the chain answered in this run, so this one was not asked. "
    "A new issue is opened when it fails again."
)


def provider(
    name: str, status: LLMProviderStatus, kind: LLMFailureKind = "auth"
) -> LLMProviderResult:
    failed = status == "failed"
    return LLMProviderResult(
        provider=name,
        status=status,
        calls=0 if status == "unused" else 1,
        failures=int(failed),
        failure_kind=kind if failed else None,
        error="credential rejected" if failed else None,
    )


class Chain:
    """Plays runs of the LLM provider chain against a fake issue client."""

    def __init__(self, config: AppConfig, issues: FakeIssueClient) -> None:
        self.config = config
        self.issues = issues
        self.health = HealthFile()
        self.number = 0

    def run(self, *providers: LLMProviderResult, enabled: bool = True) -> None:
        self.number += 1
        moment = REFERENCE_NOW + timedelta(days=self.number)
        report = RunReport(
            started_at=moment,
            finished_at=moment,
            mode="live",
            ok=True,
            llm=LLMUsage(enabled=enabled, providers=list(providers)),
        )
        self.health = update_health(self.health, report, self.issues, config=self.config)

    def open_titles(self) -> list[str]:
        return sorted(issue.title for issue in self.issues.issues if issue.state == "open")


@pytest.fixture
def chain(app_config: AppConfig, fake_issues: FakeIssueClient) -> Chain:
    return Chain(app_config, fake_issues)


def failing_claude(chain: Chain) -> None:
    for _ in range(2):
        chain.run(provider("claude-cli", "failed"), provider("anthropic", "ok"))
    assert chain.open_titles() == [llm_issue_title("claude-cli")]


class TestProviderLeavesTheChain:
    def test_a_provider_that_left_the_chain_has_its_issue_closed(
        self, chain: Chain, fake_issues: FakeIssueClient
    ) -> None:
        failing_claude(chain)
        chain.run(provider("anthropic", "ok"))
        assert chain.open_titles() == []
        assert fake_issues.comments == {1: [NOT_IN_CHAIN]}

    def test_a_run_without_any_provider_closes_it_too(
        self, chain: Chain, fake_issues: FakeIssueClient
    ) -> None:
        failing_claude(chain)
        chain.run(enabled=False)
        assert chain.open_titles() == []
        assert fake_issues.comments == {1: [NOT_IN_CHAIN]}

    def test_an_unknown_chain_leaves_the_issue_alone(self, chain: Chain) -> None:
        """The LLM was on, but what it did could not be read: nothing is concluded."""
        failing_claude(chain)
        chain.run(enabled=True)
        assert chain.open_titles() == [llm_issue_title("claude-cli")]

    def test_a_provider_that_comes_back_and_fails_gets_a_new_issue(self, chain: Chain) -> None:
        failing_claude(chain)
        chain.run(provider("anthropic", "ok"))
        chain.run(provider("claude-cli", "failed"), provider("anthropic", "ok"))
        assert chain.open_titles() == [llm_issue_title("claude-cli")]
        assert len(chain.issues.issues) == 2


class TestEarlierProviderAnswers:
    def test_a_fallback_that_is_not_asked_any_more_has_its_issue_closed(
        self, chain: Chain, fake_issues: FakeIssueClient
    ) -> None:
        both_down = (
            provider("claude-cli", "failed", "quota"),
            provider("anthropic", "failed", "rate_limit"),
            provider("openai", "ok"),
        )
        chain.run(*both_down)
        chain.run(*both_down)
        assert chain.open_titles() == [llm_issue_title("anthropic"), llm_issue_title("claude-cli")]
        chain.run(
            provider("claude-cli", "ok"),
            provider("anthropic", "unused"),
            provider("openai", "unused"),
        )
        assert chain.open_titles() == []
        comments = {
            fake_issues.get(number).title: texts for number, texts in fake_issues.comments.items()
        }
        assert comments == {
            llm_issue_title("claude-cli"): ["The provider answers again."],
            llm_issue_title("anthropic"): [ANOTHER_ANSWERS],
        }

    def test_a_run_in_which_nobody_was_asked_closes_nothing(self, chain: Chain) -> None:
        failing_claude(chain)
        chain.run(provider("claude-cli", "unused"), provider("anthropic", "unused"))
        assert chain.open_titles() == [llm_issue_title("claude-cli")]

    def test_the_body_says_when_the_issue_closes(self, chain: Chain) -> None:
        failing_claude(chain)
        body = llm_issue_body("claude-cli", chain.health.llm["claude-cli"])
        assert "closed automatically when the provider answers again" in body
        assert "when another provider of the chain answers instead" in body
        assert "when the provider leaves the chain" in body
        assert body.isascii()

    def test_the_body_says_what_the_pipeline_uses_without_the_provider(self, chain: Chain) -> None:
        failing_claude(chain)
        body = " ".join(llm_issue_body("claude-cli", chain.health.llm["claude-cli"]).split())
        assert (
            "The pipeline goes on without this provider: it uses the next one of the chain, "
            "or, when none is left, the answers stored by earlier runs and the rules."
        ) in body
