"""Building the provider chain from the configuration and the environment, and probing it."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from seminars.config import PROVIDER_IDS, LLMConfig, apply_llm_env
from seminars.contracts import (
    LLMAuthError,
    LLMBudgetExceededError,
    LLMChainClient,
    LLMClientFactory,
    LLMProber,
    LLMUnavailableError,
    load_implementation,
)
from seminars.llm import resolve
from seminars.llm.api import AnthropicProvider, OpenAICompatProvider, OpenAIProvider
from seminars.llm.chain import ProviderChain
from seminars.llm.claude_cli import ClaudeCliProvider
from seminars.llm.cli import CliRequest, CliResult, CliStartError
from seminars.llm.codex_cli import CodexCliProvider
from seminars.llm.resolve import (
    PROBE_MAX_TOKENS,
    ProbeAnswer,
    build_llm_client,
    build_providers,
    probe_providers,
)
from seminars.models import LLMProviderResult
from test_llm_fakes import FAKE_TOKEN, FakeRunner, StubProvider

MODELS = {
    "claude-cli": "haiku",
    "codex-cli": "gpt-6-luna",
    "anthropic": "claude-test",
    "openai": "gpt-test",
    "openai-compat": "local-test",
}
ALL_CREDENTIALS = {
    "CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN,
    "ANTHROPIC_API_KEY": "an-" + FAKE_TOKEN,
    "OPENAI_API_KEY": "oa-" + FAKE_TOKEN,
    "LLM_API_KEY": "compat-" + FAKE_TOKEN,
    "LLM_BASE_URL": "https://llm.example.org/v1",
    "PATH": "/usr/bin",
    "HOME": "/home/owner",
}


def config_for(env: Mapping[str, str], **overrides: Any) -> LLMConfig:
    """The effective configuration, the way the loader hands it to the LLM layer."""
    base: dict[str, Any] = {
        "models": MODELS,
        "cli_versions": {"claude-cli": "2.1.322", "codex-cli": "0.167.0"},
    }
    base.update(overrides)
    return apply_llm_env(LLMConfig.model_validate(base), env)


def names(env: Mapping[str, str], **overrides: Any) -> list[str]:
    return [provider.name for provider in build_providers(config_for(env, **overrides), env)]


def claude_ok(request: CliRequest) -> CliResult:
    document = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "structured_output": {"ok": True},
        "usage": {"input_tokens": 5, "output_tokens": 2},
        "total_cost_usd": 0.0001,
    }
    return CliResult(0, json.dumps(document), "")


class TestContract:
    def test_the_contract_points_at_these_functions(self) -> None:
        build: LLMClientFactory = load_implementation("llm_client")
        probe: LLMProber = load_implementation("llm_probe")
        assert build is build_llm_client
        assert probe is probe_providers


class TestWhichProvidersJoin:
    def test_without_any_credential_there_is_no_chain(self) -> None:
        env = {"PATH": "/usr/bin", "HOME": "/home/owner"}
        assert names(env) == []
        assert build_llm_client(config_for(env), env) is None

    def test_only_the_claude_token_gives_a_chain_of_one(self) -> None:
        env = {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN, "GITHUB_ACTIONS": "true"}
        assert names(env) == ["claude-cli"]
        client = build_llm_client(config_for(env), env)
        assert isinstance(client, ProviderChain)
        assert isinstance(client, LLMChainClient)
        assert client.name == "claude-cli"

    def test_every_credential_gives_the_default_order_without_codex(self) -> None:
        assert names(ALL_CREDENTIALS) == ["claude-cli", "anthropic", "openai", "openai-compat"]

    def test_codex_joins_only_when_it_is_named(self) -> None:
        env = {**ALL_CREDENTIALS, "LLM_PROVIDERS": "claude-cli,codex-cli"}
        assert names(env) == ["claude-cli", "codex-cli"]
        assert names({"LLM_PROVIDERS": "codex-cli"}) == ["codex-cli"]

    def test_codex_never_joins_inside_github_actions(self) -> None:
        env = {**ALL_CREDENTIALS, "LLM_PROVIDERS": "codex-cli,claude-cli", "GITHUB_ACTIONS": "true"}
        assert names(env) == ["claude-cli"]

    def test_codex_is_dropped_in_github_actions_even_when_the_config_still_lists_it(self) -> None:
        """A configuration that did not go through the environment overrides."""
        env = {**ALL_CREDENTIALS, "GITHUB_ACTIONS": "true"}
        raw = LLMConfig.model_validate(
            {
                "models": MODELS,
                "providers": list(PROVIDER_IDS),
                "base_url": "https://llm.example.org/v1",
            }
        )
        assert "codex-cli" not in [provider.name for provider in build_providers(raw, env)]
        local = [provider.name for provider in build_providers(raw, ALL_CREDENTIALS)]
        assert local == list(PROVIDER_IDS)

    def test_the_order_of_llm_providers_is_kept(self) -> None:
        env = {**ALL_CREDENTIALS, "LLM_PROVIDERS": "openai,claude-cli"}
        assert names(env) == ["openai", "claude-cli"]

    def test_none_disables_the_llm(self) -> None:
        env = {**ALL_CREDENTIALS, "LLM_PROVIDERS": "none"}
        assert build_llm_client(config_for(env), env) is None

    @pytest.mark.parametrize("value", ["", "   ", "\n"])
    def test_a_blank_credential_counts_as_missing(self, value: str) -> None:
        env = {**ALL_CREDENTIALS, "CLAUDE_CODE_OAUTH_TOKEN": value, "ANTHROPIC_API_KEY": value}
        assert names(env) == ["openai", "openai-compat"]

    def test_an_api_provider_without_a_model_is_skipped(self) -> None:
        models = {**MODELS, "anthropic": None, "openai-compat": None}
        assert names(ALL_CREDENTIALS, models=models) == ["claude-cli", "openai"]

    def test_a_model_from_the_environment_counts(self) -> None:
        models = {**MODELS, "openai-compat": None}
        env = {**ALL_CREDENTIALS, "LLM_MODEL_OPENAI_COMPAT": "served-model"}
        providers = build_providers(config_for(env, models=models), env)
        assert providers[-1].name == "openai-compat"
        assert providers[-1].model == "served-model"

    def test_openai_compat_needs_an_address(self) -> None:
        env = {key: value for key, value in ALL_CREDENTIALS.items() if key != "LLM_BASE_URL"}
        assert "openai-compat" not in names(env)
        assert "openai-compat" in names(env, base_url="https://configured.example.org/v1")

    def test_a_cli_provider_without_a_model_uses_the_default_of_the_cli(self) -> None:
        models = {**MODELS, "claude-cli": None}
        (claude,) = build_providers(
            config_for({"CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN}, models=models),
            {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN},
        )
        assert claude.name == "claude-cli"
        assert claude.model is None

    def test_a_provider_listed_twice_joins_once(self) -> None:
        raw = LLMConfig.model_validate({"models": MODELS, "providers": ["openai", "openai"]})
        assert [provider.name for provider in build_providers(raw, ALL_CREDENTIALS)] == ["openai"]

    def test_the_backend_classes(self) -> None:
        env = {**ALL_CREDENTIALS, "LLM_PROVIDERS": ",".join(PROVIDER_IDS)}
        providers = build_providers(config_for(env), env)
        assert [type(provider) for provider in providers] == [
            ClaudeCliProvider,
            CodexCliProvider,
            AnthropicProvider,
            OpenAIProvider,
            OpenAICompatProvider,
        ]
        assert [provider.model for provider in providers] == list(MODELS.values())

    def test_building_runs_no_command_and_sends_nothing(self) -> None:
        runner = FakeRunner()
        env = {**ALL_CREDENTIALS, "LLM_PROVIDERS": ",".join(PROVIDER_IDS)}
        build_providers(config_for(env), env, runner=runner)
        assert runner.requests == []


class TestChainSettings:
    def test_the_caps_of_the_configuration_apply(self) -> None:
        env = {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN}
        config = config_for(env, max_calls_per_run=0, max_input_chars=5)
        client = build_llm_client(config, env)
        assert client is not None
        with pytest.raises(LLMBudgetExceededError, match="max_input_chars"):
            client.complete_json("s", "too long", ProbeAnswer, max_tokens=5)
        with pytest.raises(LLMBudgetExceededError, match="call cap"):
            client.complete_json("s", "ok", ProbeAnswer, max_tokens=5)

    def test_the_cli_backends_get_the_timeout_the_token_and_the_verified_version(self) -> None:
        runner = FakeRunner(claude_ok, version="2.1.322")
        env = {**ALL_CREDENTIALS, "CLAUDE_CODE_OAUTH_TOKEN": f" {FAKE_TOKEN}\n"}
        config = config_for(env, timeout_seconds=45)
        (claude, *_) = build_providers(config, env, runner=runner)
        completion = claude.complete("s", "u", ProbeAnswer, max_tokens=5)
        assert completion.value == ProbeAnswer(ok=True)
        assert completion.warnings == ()
        (call,) = runner.calls
        assert call.timeout == 45
        assert call.env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_TOKEN
        assert call.argv[call.argv.index("--model") + 1] == "haiku"

    def test_a_chain_built_here_fails_over_and_ends_in_none(self) -> None:
        runner = FakeRunner(CliStartError("FileNotFoundError"))
        env = {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN}
        client = ProviderChain(
            build_providers(config_for(env), env, runner=runner),
            max_calls=10,
            max_input_chars=100,
        )
        with pytest.raises(LLMUnavailableError):
            client.complete_json("s", "u", ProbeAnswer, max_tokens=5)
        (result,) = client.usage().providers
        assert (result.provider, result.status, result.failure_kind) == (
            "claude-cli",
            "failed",
            "network",
        )


class TestProbe:
    def test_without_credentials_every_provider_is_reported_as_not_configured(self) -> None:
        env = {"PATH": "/usr/bin"}
        results = probe_providers(config_for(env), env, runner=FakeRunner())
        assert [result.provider for result in results] == [
            "claude-cli",
            "anthropic",
            "openai",
            "openai-compat",
        ]
        assert {result.status for result in results} == {"unused"}
        assert {result.calls for result in results} == {0}
        assert results[0].warnings == ["not configured: CLAUDE_CODE_OAUTH_TOKEN is not set"]
        assert results[1].warnings == ["not configured: ANTHROPIC_API_KEY is not set"]
        assert results[3].warnings == ["not configured: LLM_API_KEY is not set"]

    def test_the_reasons_for_a_provider_that_has_a_key_but_is_not_usable(self) -> None:
        env = {key: value for key, value in ALL_CREDENTIALS.items() if key != "LLM_BASE_URL"}
        models = {**MODELS, "anthropic": None}
        runner = FakeRunner(claude_ok)
        config = config_for(env, models=models, providers=["anthropic", "openai-compat"])
        anthropic, compat = probe_providers(config, env, runner=runner)
        assert anthropic.warnings == [
            "not configured: no model is set (config/llm.yaml or LLM_MODEL_ANTHROPIC)"
        ]
        assert compat.warnings == ["not configured: LLM_BASE_URL is not set"]
        assert (anthropic.status, compat.status) == ("unused", "unused")

    def test_a_working_provider_is_ok_and_a_broken_one_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def working(**options: Any) -> StubProvider:
            return StubProvider("anthropic", {"ok": True}, model=options["model"])

        def broken(**options: Any) -> StubProvider:
            error = LLMAuthError("the credential is missing, invalid or expired", provider="openai")
            return StubProvider("openai", error, model=options["model"])

        monkeypatch.setattr(resolve, "AnthropicProvider", working)
        monkeypatch.setattr(resolve, "OpenAIProvider", broken)
        runner = FakeRunner(claude_ok, version="2.2.0")
        env = {key: value for key, value in ALL_CREDENTIALS.items() if key != "LLM_API_KEY"}
        results = probe_providers(config_for(env), env, runner=runner)
        assert results == [
            LLMProviderResult(
                provider="claude-cli",
                model="haiku",
                status="ok",
                calls=1,
                warnings=["claude CLI version 2.2.0 differs from the verified version 2.1.322"],
            ),
            LLMProviderResult(provider="anthropic", model="claude-test", status="ok", calls=1),
            LLMProviderResult(
                provider="openai",
                model="gpt-test",
                status="failed",
                calls=1,
                failures=1,
                failure_kind="auth",
                error="openai: the credential is missing, invalid or expired",
            ),
            LLMProviderResult(
                provider="openai-compat",
                model="local-test",
                status="unused",
                warnings=["not configured: LLM_API_KEY is not set"],
            ),
        ]

    def test_every_provider_is_probed_even_after_one_failed(self) -> None:
        """A probe is not the chain: it reports each provider on its own."""
        runner = FakeRunner(CliStartError("FileNotFoundError"))
        env = {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN, "LLM_PROVIDERS": "claude-cli,codex-cli"}
        runner.login = CliResult(1, "", "Not logged in")
        claude, codex = probe_providers(config_for(env), env, runner=runner)
        assert (claude.status, claude.failure_kind) == ("failed", "network")
        assert (codex.status, codex.failure_kind) == ("failed", "auth")
        assert codex.error is not None
        assert "codex login" in codex.error

    def test_the_probe_is_one_small_closed_request(self) -> None:
        runner = FakeRunner(claude_ok)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN}
        probe_providers(config_for(env), env, runner=runner)
        (call,) = runner.calls
        assert len(call.stdin) < 200
        schema = json.loads(call.argv[call.argv.index("--json-schema") + 1])
        assert schema["properties"] == {"ok": {"type": "boolean"}}
        assert PROBE_MAX_TOKENS <= 1024

    def test_an_unexpected_exception_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def exploding(**options: Any) -> StubProvider:
            return StubProvider("openai", RuntimeError(f"boom {FAKE_TOKEN}"), model="gpt-test")

        monkeypatch.setattr(resolve, "OpenAIProvider", exploding)
        env = {"OPENAI_API_KEY": FAKE_TOKEN}
        (result,) = probe_providers(config_for(env, providers=["openai"]), env)
        assert (result.status, result.failure_kind) == ("failed", "other")
        assert result.error == "openai: unexpected RuntimeError"

    def test_a_probe_reports_never_show_a_credential(self) -> None:
        runner = FakeRunner(CliResult(1, "", f"401 Unauthorized {FAKE_TOKEN}"))
        env = {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN}
        results = probe_providers(config_for(env), env, runner=runner)
        assert results[0].failure_kind == "auth"
        assert FAKE_TOKEN not in "".join(result.model_dump_json() for result in results)
