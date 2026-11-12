"""The provider chain: failover, the caps of a run, restricted views and the usage report."""

from __future__ import annotations

import threading
from typing import Literal

import pytest
from pydantic import BaseModel

from seminars.contracts import (
    LLMAuthError,
    LLMBudgetExceededError,
    LLMChainClient,
    LLMClient,
    LLMInvalidOutputError,
    LLMNetworkError,
    LLMProviderError,
    LLMQuotaError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from seminars.llm.base import Provider
from seminars.llm.chain import ProviderChain
from seminars.models import LLMProviderResult, LLMUsage, Model
from test_llm_fakes import FAKE_TOKEN, Echo, StubProvider

OK = {"ok": True}


class Tags(Model):
    fields: list[Literal["math", "bio"]]


class FreeText(Model):
    title: str


def chain(*providers: Provider, max_calls: int = 60, max_input_chars: int = 12000) -> ProviderChain:
    return ProviderChain(providers, max_calls=max_calls, max_input_chars=max_input_chars)


def ask(client: LLMClient, user: str = "question") -> Echo:
    return client.complete_json("system", user, Echo, max_tokens=50)


def result_of(usage: LLMUsage, provider: str) -> LLMProviderResult:
    (found,) = [result for result in usage.providers if result.provider == provider]
    return found


class TestProtocol:
    def test_it_is_a_chain_client(self) -> None:
        client: LLMChainClient = chain(StubProvider("claude-cli"))
        assert isinstance(client, LLMChainClient)
        assert isinstance(client.restricted([]), LLMChainClient)

    def test_the_name_is_the_provider_that_answers_next(self) -> None:
        first = StubProvider("claude-cli", LLMAuthError("expired", provider="claude-cli"))
        second = StubProvider("anthropic", OK)
        client = chain(first, second)
        assert client.name == "claude-cli"
        ask(client)
        assert client.name == "anthropic"
        assert client.restricted(["anthropic"]).name == "none"
        assert chain().name == "none"


class TestFailover:
    def test_auth_failure_then_rate_limit_then_the_third_provider_answers(self) -> None:
        first = StubProvider("claude-cli", LLMAuthError("expired", provider="claude-cli"))
        second = StubProvider("anthropic", LLMRateLimitError("slow down", provider="anthropic"))
        third = StubProvider("openai").always(OK)
        client = chain(first, second, third)
        assert ask(client) == Echo(ok=True)
        assert [len(stub.calls) for stub in (first, second, third)] == [1, 1, 1]
        usage = client.usage()
        assert usage.enabled
        assert usage.provider == "openai"
        assert usage.calls == 3
        assert [(r.provider, r.status, r.failure_kind) for r in usage.providers] == [
            ("claude-cli", "failed", "auth"),
            ("anthropic", "failed", "rate_limit"),
            ("openai", "ok", None),
        ]

    def test_a_failed_provider_is_not_asked_again_in_the_same_run(self) -> None:
        first = StubProvider("claude-cli", LLMAuthError("expired", provider="claude-cli"))
        second = StubProvider("openai").always(OK)
        client = chain(first, second)
        for _ in range(4):
            ask(client)
        assert len(first.calls) == 1
        assert len(second.calls) == 4
        assert client.usage().calls == 5

    @pytest.mark.parametrize(
        "error",
        [
            LLMAuthError("a"),
            LLMQuotaError("q"),
            LLMRateLimitError("r"),
            LLMTimeoutError("t"),
            LLMNetworkError("n"),
        ],
    )
    def test_every_provider_failure_moves_on(self, error: LLMProviderError) -> None:
        client = chain(StubProvider("claude-cli", error), StubProvider("openai", OK))
        assert ask(client) == Echo(ok=True)
        assert result_of(client.usage(), "claude-cli").failure_kind == error.failure_kind

    def test_when_every_provider_fails_the_chain_ends_in_none(self) -> None:
        first = StubProvider("claude-cli", LLMAuthError("expired", provider="claude-cli"))
        second = StubProvider("openai", LLMQuotaError("empty", provider="openai"))
        client = chain(first, second)
        with pytest.raises(LLMUnavailableError):
            ask(client)
        with pytest.raises(LLMUnavailableError):
            ask(client)
        assert [len(first.calls), len(second.calls)] == [1, 1]
        usage = client.usage()
        assert usage.provider is None
        assert usage.calls == 2
        assert [result.status for result in usage.providers] == ["failed", "failed"]
        assert client.name == "none"

    def test_a_chain_without_providers_is_unavailable(self) -> None:
        client = chain()
        with pytest.raises(LLMUnavailableError):
            ask(client)
        assert client.usage() == LLMUsage(enabled=True)

    def test_an_unexpected_exception_of_a_backend_is_a_failure_of_that_backend(self) -> None:
        first = StubProvider("anthropic", RuntimeError(f"boom {FAKE_TOKEN}"))
        client = chain(first, StubProvider("openai", OK))
        assert ask(client) == Echo(ok=True)
        failed = result_of(client.usage(), "anthropic")
        assert (failed.status, failed.failure_kind) == ("failed", "other")
        assert failed.error == "anthropic: unexpected RuntimeError"

    def test_a_schema_no_backend_can_take_is_an_error_of_the_caller(self) -> None:
        class Loose(BaseModel):
            extra: dict[str, int]

        stub = StubProvider("openai").always(OK)
        client = chain(stub)
        with pytest.raises(ValueError, match="additionalProperties"):
            client.complete_json("s", "u", Loose, max_tokens=5)
        assert stub.calls == []
        assert client.usage().calls == 0


class TestInvalidOutput:
    def test_invalid_output_is_raised_and_does_not_switch_providers(self) -> None:
        first = StubProvider("claude-cli", {"ok": "perhaps"}, OK)
        second = StubProvider("openai").always(OK)
        client = chain(first, second)
        with pytest.raises(LLMInvalidOutputError):
            ask(client)
        assert ask(client) == Echo(ok=True)
        assert len(first.calls) == 2
        assert second.calls == []
        result = result_of(client.usage(), "claude-cli")
        assert (result.status, result.calls, result.failures) == ("ok", 2, 1)
        assert result.failure_kind is None

    def test_a_provider_that_only_gave_invalid_output_is_reported_as_failed(self) -> None:
        client = chain(StubProvider("openai-compat").always({"ok": "perhaps"}))
        for _ in range(2):
            with pytest.raises(LLMInvalidOutputError):
                ask(client)
        result = result_of(client.usage(), "openai-compat")
        assert (result.status, result.calls, result.failures) == ("failed", 2, 2)
        assert result.failure_kind == "invalid_output"
        assert client.usage().provider is None
        assert client.name == "openai-compat", "it stays in the chain"


class TestCaps:
    def test_the_call_cap_counts_every_request_sent_to_a_provider(self) -> None:
        first = StubProvider("claude-cli", LLMAuthError("expired", provider="claude-cli"))
        second = StubProvider("openai").always(OK)
        client = chain(first, second, max_calls=3)
        ask(client)  # two requests: the failed one and the answer
        ask(client)
        for _ in range(2):
            with pytest.raises(LLMBudgetExceededError, match="call cap"):
                ask(client)
        usage = client.usage()
        assert usage.calls == 3
        assert usage.skipped_call_cap == 2
        assert len(second.calls) == 2

    def test_a_cap_of_zero_sends_nothing(self) -> None:
        stub = StubProvider("openai").always(OK)
        client = chain(stub, max_calls=0)
        with pytest.raises(LLMBudgetExceededError):
            ask(client)
        assert stub.calls == []
        assert client.usage().skipped_call_cap == 1

    def test_the_cap_is_not_spent_when_no_provider_is_left(self) -> None:
        client = chain(StubProvider("openai", LLMAuthError("x", provider="openai")), max_calls=1)
        with pytest.raises(LLMUnavailableError):
            ask(client)
        with pytest.raises(LLMUnavailableError):
            ask(client)
        assert client.usage().skipped_call_cap == 0

    def test_an_input_that_is_too_long_is_skipped_and_counted(self) -> None:
        stub = StubProvider("openai").always(OK)
        client = chain(stub, max_input_chars=10)
        assert ask(client, "x" * 10) == Echo(ok=True)
        with pytest.raises(LLMBudgetExceededError, match="max_input_chars"):
            ask(client, "x" * 11)
        usage = client.usage()
        assert usage.skipped_too_long == 1
        assert usage.calls == 1
        assert len(stub.calls) == 1


class TestRestrictedViews:
    def test_a_view_never_asks_an_excluded_provider(self) -> None:
        codex = StubProvider("codex-cli").always(OK)
        claude = StubProvider("claude-cli").always(OK)
        client = chain(codex, claude)
        view = client.restricted(["codex-cli"])
        assert ask(view) == Echo(ok=True)
        assert codex.calls == []
        assert len(claude.calls) == 1
        assert ask(client) == Echo(ok=True)
        assert len(codex.calls) == 1

    def test_a_view_without_any_provider_left_is_unavailable(self) -> None:
        codex = StubProvider("codex-cli").always(OK)
        client = chain(codex)
        with pytest.raises(LLMUnavailableError):
            ask(client.restricted(["codex-cli"]))
        assert codex.calls == []
        assert client.usage().calls == 0

    def test_views_share_the_cap_the_failures_and_the_usage(self) -> None:
        claude = StubProvider("claude-cli", OK, LLMAuthError("expired", provider="claude-cli"))
        openai = StubProvider("openai").always(OK)
        client = chain(claude, openai, max_calls=4)
        view = client.restricted(["codex-cli"])
        ask(view)
        ask(view)  # claude fails here, openai answers
        ask(client)  # the chain knows that claude failed
        assert len(claude.calls) == 2
        assert len(openai.calls) == 2
        with pytest.raises(LLMBudgetExceededError):
            ask(client)
        with pytest.raises(LLMBudgetExceededError):
            ask(view)
        assert client.usage() == view.usage()
        assert client.usage().calls == 4
        assert client.usage().skipped_call_cap == 2

    def test_restrictions_add_up(self) -> None:
        stubs = [StubProvider(name).always(OK) for name in ("claude-cli", "anthropic", "openai")]
        view = chain(*stubs).restricted(["claude-cli"]).restricted(("anthropic",))
        ask(view)
        assert [len(stub.calls) for stub in stubs] == [0, 0, 1]

    def test_a_provider_that_does_not_serve_the_schema_is_passed_over_not_failed(self) -> None:
        codex = StubProvider("codex-cli", closed_only=True).always({"fields": ["bio"]})
        claude = StubProvider("claude-cli").always({"title": "A talk"})
        client = chain(codex, claude)
        assert client.complete_json("s", "u", FreeText, max_tokens=5) == FreeText(title="A talk")
        assert codex.calls == []
        assert client.complete_json("s", "u", Tags, max_tokens=5) == Tags(fields=["bio"])
        assert len(codex.calls) == 1
        assert result_of(client.usage(), "codex-cli").status == "ok"

    def test_free_text_with_only_a_closed_vocabulary_provider_is_unavailable(self) -> None:
        codex = StubProvider("codex-cli", closed_only=True).always({"fields": []})
        client = chain(codex)
        with pytest.raises(LLMUnavailableError):
            client.complete_json("s", "u", FreeText, max_tokens=5)
        assert codex.calls == []


class TestUsage:
    def test_tokens_cost_and_warnings_add_up_per_run(self) -> None:
        claude = StubProvider(
            "claude-cli",
            model="haiku",
            usage=(100, 20, 0.002),
            warnings=("claude CLI version 2.2.0 differs from the verified version 2.1.322",),
        ).always(OK)
        client = chain(claude, StubProvider("openai", model="gpt-test"))
        for _ in range(3):
            ask(client)
        usage = client.usage()
        assert (usage.input_tokens, usage.output_tokens) == (300, 60)
        assert usage.cost_usd == pytest.approx(0.006)
        assert usage.cache_hits == 0
        assert usage.providers == [
            LLMProviderResult(
                provider="claude-cli",
                model="haiku",
                status="ok",
                calls=3,
                warnings=["claude CLI version 2.2.0 differs from the verified version 2.1.322"],
            ),
            LLMProviderResult(provider="openai", model="gpt-test", status="unused"),
        ]

    def test_before_any_call_everything_is_unused(self) -> None:
        usage = chain(StubProvider("claude-cli"), StubProvider("openai")).usage()
        assert usage == LLMUsage(
            enabled=True,
            providers=[
                LLMProviderResult(provider="claude-cli", model="stub-model", status="unused"),
                LLMProviderResult(provider="openai", model="stub-model", status="unused"),
            ],
        )

    def test_the_error_of_a_failed_provider_is_its_own_fixed_message(self) -> None:
        error = LLMAuthError("the credential is missing, invalid or expired", provider="openai")
        client = chain(StubProvider("openai", error))
        with pytest.raises(LLMUnavailableError):
            ask(client)
        failed = result_of(client.usage(), "openai")
        assert failed.error == "openai: the credential is missing, invalid or expired"
        assert (failed.calls, failed.failures) == (1, 1)

    def test_the_report_passes_the_charset_rule_of_data(self) -> None:
        client = chain(StubProvider("openai", RuntimeError("caf" + chr(0xE9))))
        with pytest.raises(LLMUnavailableError):
            ask(client)
        assert client.usage().model_dump_json().isascii()


class TestThreads:
    def test_the_cap_holds_when_several_threads_ask_at_once(self) -> None:
        stub = StubProvider("openai").always(OK)
        client = chain(stub, max_calls=25)
        outcomes: list[str] = []
        lock = threading.Lock()

        def work() -> None:
            for _ in range(10):
                try:
                    ask(client)
                    outcome = "answered"
                except LLMBudgetExceededError:
                    outcome = "skipped"
                with lock:
                    outcomes.append(outcome)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert outcomes.count("answered") == 25
        assert outcomes.count("skipped") == 55
        usage = client.usage()
        assert (usage.calls, usage.skipped_call_cap) == (25, 55)
