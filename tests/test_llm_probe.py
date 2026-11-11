"""The probe of the provider chain: a run that sends no request still learns its state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from seminars.config import LLMConfig
from seminars.contracts import LLMAuthError, LLMBudgetExceededError, LLMQuotaError
from seminars.llm import resolve
from seminars.llm.chain import ProviderChain
from seminars.llm.probe import PROBE_MAX_TOKENS, PROBE_SYSTEM, PROBE_USER, ProbeAnswer
from test_llm_fakes import FAKE_TOKEN, StubProvider

OK = {"ok": True}


def chain_of(*providers: StubProvider, max_calls: int = 10, idle: bool = True) -> ProviderChain:
    return ProviderChain(
        list(providers), max_calls=max_calls, max_input_chars=12000, probe_when_idle=idle
    )


class TestAnIdleRun:
    def test_reading_the_usage_sends_one_probe_to_the_first_provider(self) -> None:
        first = StubProvider("claude-cli").always(OK)
        second = StubProvider("anthropic").always(OK)
        chain = chain_of(first, second)
        usage = chain.usage()
        assert first.calls == [(PROBE_SYSTEM, PROBE_USER, ProbeAnswer)]
        assert second.calls == []
        assert usage.calls == 1
        assert usage.provider == "claude-cli"
        assert [result.status for result in usage.providers] == ["ok", "unused"]

    def test_the_probe_is_sent_once_however_often_the_usage_is_read(self) -> None:
        first = StubProvider("claude-cli").always(OK)
        chain = chain_of(first)
        chain.usage()
        chain.usage()
        chain.restricted({"codex-cli"}).usage()
        chain.probe()
        assert len(first.calls) == 1

    def test_an_expired_credential_is_found_and_the_next_provider_is_probed(self) -> None:
        expired = StubProvider("claude-cli", LLMAuthError("401 expired", provider="claude-cli"))
        second = StubProvider("anthropic").always(OK)
        usage = chain_of(expired, second).usage()
        first_result, second_result = usage.providers
        assert (first_result.status, first_result.failure_kind) == ("failed", "auth")
        assert first_result.failures == 1
        assert second_result.status == "ok"
        assert usage.calls == 2

    def test_a_probe_that_no_provider_answers_does_not_raise(self) -> None:
        empty = StubProvider("claude-cli", LLMQuotaError("used up", provider="claude-cli"))
        usage = chain_of(empty).usage()
        [result] = usage.providers
        assert (result.status, result.failure_kind) == ("failed", "quota")
        assert usage.provider is None

    def test_a_run_without_room_for_a_request_sends_no_probe(self) -> None:
        first = StubProvider("claude-cli").always(OK)
        usage = chain_of(first, max_calls=0).usage()
        assert first.calls == []
        assert (usage.calls, usage.skipped_call_cap) == (0, 0)

    def test_the_probe_counts_against_the_cap_of_the_run(self) -> None:
        first = StubProvider("claude-cli").always(OK)
        chain = chain_of(first, max_calls=1)
        chain.probe()
        assert chain.usage().calls == 1
        with pytest.raises(LLMBudgetExceededError, match="call cap"):
            chain.complete_json("system", "user", ProbeAnswer, max_tokens=5)


class TestABusyRun:
    def test_a_run_that_sent_a_request_has_had_its_probe(self) -> None:
        first = StubProvider("claude-cli").always(OK)
        chain = chain_of(first)
        chain.complete_json("system", "user", ProbeAnswer, max_tokens=5)
        chain.usage()
        chain.probe()
        assert len(first.calls) == 1

    def test_without_probe_when_idle_reading_the_usage_sends_nothing(self) -> None:
        first = StubProvider("claude-cli").always(OK)
        usage = chain_of(first, idle=False).usage()
        assert first.calls == []
        assert usage.providers[0].status == "unused"

    def test_an_explicit_probe_works_without_probe_when_idle(self) -> None:
        first = StubProvider("claude-cli").always(OK)
        chain = chain_of(first, idle=False)
        chain.probe()
        assert len(first.calls) == 1
        assert chain.usage().providers[0].status == "ok"

    def test_the_probe_request_is_small(self) -> None:
        assert PROBE_MAX_TOKENS <= 1024
        assert len(PROBE_SYSTEM) + len(PROBE_USER) < 200


class TestTheChainOfARun:
    def test_build_llm_client_makes_a_chain_that_probes_when_idle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = StubProvider("claude-cli").always(OK)

        def stubs(config: LLMConfig, env: Mapping[str, str], **_: Any) -> list[StubProvider]:
            return [stub]

        monkeypatch.setattr(resolve, "build_providers", stubs)
        chain = resolve.build_llm_client(LLMConfig(), {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN})
        assert chain is not None
        assert stub.calls == []  # building sends nothing
        assert chain.usage().providers[0].status == "ok"
        assert len(stub.calls) == 1
