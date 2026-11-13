"""What ``seminars llm-smoke`` prints about each provider: the reason it is unused, never a value.

The probe gives an unconfigured provider the status ``unused`` and the reason
as its first warning ("not configured: CLAUDE_CODE_OAUTH_TOKEN is not set").
The line of that provider carries the reason, so whoever reads the log of the
smoke job sees which secret or variable is missing. A reason names a
variable, never its value: a credential that is set but rejected appears in
no line.
"""

from __future__ import annotations

from typing import Any

import pytest

from seminars import cli
from seminars.llm import resolve
from seminars.llm.cli import CliResult
from seminars.llm.resolve import probe_providers
from seminars.models import LLMProviderResult
from test_llm_fakes import FAKE_TOKEN, FakeRunner
from test_llm_resolve import MODELS, config_for

CREDENTIALS = {
    "CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN,
    "ANTHROPIC_API_KEY": "an-" + FAKE_TOKEN,
    "OPENAI_API_KEY": "oa-" + FAKE_TOKEN,
    "LLM_API_KEY": "compat-" + FAKE_TOKEN,
}


class TestTheLineOfAnUnusedProvider:
    def test_it_gives_the_reason(self) -> None:
        result = LLMProviderResult(
            provider="claude-cli",
            model="haiku",
            status="unused",
            warnings=["not configured: CLAUDE_CODE_OAUTH_TOKEN is not set"],
        )
        assert cli.provider_line(result) == (
            "claude-cli: unused (model haiku): not configured: CLAUDE_CODE_OAUTH_TOKEN is not set"
        )

    def test_only_the_first_warning_is_the_reason(self) -> None:
        result = LLMProviderResult(provider="openai", status="unused", warnings=["one", "two"])
        assert cli.provider_line(result) == "openai: unused: one"

    def test_without_a_warning_it_says_unused(self) -> None:
        assert cli.provider_line(LLMProviderResult(provider="openai", status="unused")) == (
            "openai: unused"
        )

    def test_the_reason_is_printable_ascii(self) -> None:
        result = LLMProviderResult(
            provider="openai", status="unused", warnings=["bad \u00e9\nnext line"]
        )
        line = cli.provider_line(result)
        assert line.isascii()
        assert line.isprintable()

    def test_the_warnings_of_a_provider_that_answered_are_not_appended(self) -> None:
        result = LLMProviderResult(
            provider="claude-cli", model="haiku", status="ok", warnings=["version differs"]
        )
        assert cli.provider_line(result) == "claude-cli: ok (model haiku)"


def smoke_lines(results: list[LLMProviderResult]) -> list[str]:
    return [cli.provider_line(result) for result in results]


class TestSetButRejectedCredentials:
    """The probe of the real resolver, with every credential set, gives no line with a value."""

    def test_a_key_without_a_model_or_an_address_names_what_is_missing(self) -> None:
        env = dict(CREDENTIALS)
        config = config_for(
            env,
            models={**MODELS, "anthropic": None},
            providers=["anthropic", "openai-compat"],
        )
        lines = smoke_lines(probe_providers(config, env, runner=FakeRunner()))
        assert lines == [
            "anthropic: unused: not configured: no model is set "
            "(config/llm.yaml or LLM_MODEL_ANTHROPIC)",
            "openai-compat: unused (model local-test): not configured: LLM_BASE_URL is not set",
        ]
        assert not any(FAKE_TOKEN in line for line in lines)

    def test_a_backend_that_refuses_the_key_is_unused_and_shows_no_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refusing(**options: Any) -> None:
            raise ValueError(f"the key {options['api_key']} has the wrong shape")

        monkeypatch.setattr(resolve, "OpenAIProvider", refusing)
        env = dict(CREDENTIALS)
        (line,) = smoke_lines(probe_providers(config_for(env, providers=["openai"]), env))
        assert (
            line == "openai: unused (model gpt-test): not configured: cannot be set up (ValueError)"
        )
        assert FAKE_TOKEN not in line

    def test_a_token_that_the_cli_rejects_is_a_failure_without_the_value(self) -> None:
        runner = FakeRunner(CliResult(1, "", f"401 Unauthorized: invalid token {FAKE_TOKEN}"))
        env = dict(CREDENTIALS)
        (line,) = smoke_lines(
            probe_providers(config_for(env, providers=["claude-cli"]), env, runner=runner)
        )
        assert line.startswith("claude-cli: failed (model haiku): auth: ")
        assert FAKE_TOKEN not in line
        assert "sk-ant-" not in line

    def test_no_credential_value_in_any_line_of_a_full_probe(self) -> None:
        runner = FakeRunner(CliResult(1, "", f"Invalid API key {FAKE_TOKEN}"))
        env = {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN, "ANTHROPIC_API_KEY": " "}
        lines = smoke_lines(probe_providers(config_for(env), env, runner=runner))
        assert lines[1:] == [
            "anthropic: unused (model claude-test): not configured: ANTHROPIC_API_KEY is not set",
            "openai: unused (model gpt-test): not configured: OPENAI_API_KEY is not set",
            "openai-compat: unused (model local-test): not configured: LLM_API_KEY is not set",
        ]
        assert not any(value in line for value in CREDENTIALS.values() for line in lines)
