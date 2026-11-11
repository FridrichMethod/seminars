"""Stand-ins shared by the tests of the LLM layer, and the checks that they behave.

``FakeRunner`` plays a command line client: it records every request it is
asked to run and answers from a script. ``StubProvider`` is a scripted
backend for the provider chain. Import them from this module
(``from test_llm_fakes import FakeRunner``).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import BaseModel

from seminars.contracts import LLMAuthError, LLMClient, LLMError
from seminars.llm.base import Completion, Provider, ProviderBase
from seminars.llm.cli import CliRequest, CliResult, CliRunner, CliTimeoutError
from seminars.llm.schema import is_closed_vocabulary, parse_answer, strict_json_schema
from seminars.models import Model

FAKE_TOKEN: Final = "-".join(["sk", "ant", "oat01", "fake", "token" * 6])
"""Shaped like a credential and put together at run time, so that no file holds one."""

CliReply = CliResult | Exception | Callable[[CliRequest], CliResult]


class FakeRunner:
    """A ``CliRunner`` that answers from a script and records what it was asked to run.

    ``--version`` and ``login status`` are answered from ``version`` and
    ``login`` without using up the script; set either to an exception to
    make that command fail. The default login answer is what the codex
    client really prints: one line on its standard error output. Every
    other request takes the next reply: a result, an exception to raise, or
    a function of the request (which can look at the files and the
    directory the request points to while they still exist).
    """

    def __init__(self, *replies: CliReply, version: str = "9.9.9") -> None:
        self.requests: list[CliRequest] = []
        self.version: str | Exception = version
        self.login: CliResult | Exception = CliResult(0, "", "Logged in using ChatGPT\n")
        self._script: deque[CliReply] = deque(replies)

    @property
    def calls(self) -> list[CliRequest]:
        """The requests that were model calls: not ``--version``, not ``login status``."""
        return [request for request in self.requests if not self._is_local(request)]

    @staticmethod
    def _is_local(request: CliRequest) -> bool:
        return request.argv[1:] in (("--version",), ("login", "status"))

    def __call__(self, request: CliRequest) -> CliResult:
        self.requests.append(request)
        if request.argv[1:] == ("--version",):
            if isinstance(self.version, Exception):
                raise self.version
            return CliResult(0, f"{self.version} (fake client)\n", "")
        if request.argv[1:] == ("login", "status"):
            if isinstance(self.login, Exception):
                raise self.login
            return self.login
        if not self._script:
            raise AssertionError(f"FakeRunner: no reply scripted for {request.argv[:3]}")
        reply = self._script.popleft()
        if isinstance(reply, Exception):
            raise reply
        return reply(request) if callable(reply) else reply


Scripted = Mapping[str, Any] | BaseModel | Exception


class StubProvider(ProviderBase):
    """A scripted ``Provider``: each call takes the next reply; ``always`` repeats one.

    A mapping or a model is validated against the requested schema like the
    answer of a real backend. ``calls`` records ``(system, user, schema)``.
    """

    def __init__(
        self,
        name: str,
        *replies: Scripted,
        model: str | None = "stub-model",
        closed_only: bool = False,
        usage: tuple[int, int, float] = (10, 5, 0.001),
        warnings: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.model = model
        self.calls: list[tuple[str, str, type[BaseModel]]] = []
        self._script: deque[Scripted] = deque(replies)
        self._always: Scripted | None = None
        self._closed_only = closed_only
        self._usage = usage
        self._warnings = warnings

    def always(self, reply: Scripted) -> StubProvider:
        self._always = reply
        return self

    def serves(self, schema: type[BaseModel]) -> bool:
        if not self._closed_only:
            return True
        return is_closed_vocabulary(strict_json_schema(schema))

    def complete[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> Completion[T]:
        self.calls.append((system, user, schema))
        if self._script:
            reply = self._script.popleft()
        elif self._always is not None:
            reply = self._always
        else:
            raise AssertionError(f"StubProvider {self.name}: call {len(self.calls)} has no reply")
        if isinstance(reply, Exception):
            raise reply
        data = reply.model_dump() if isinstance(reply, BaseModel) else dict(reply)
        tokens_in, tokens_out, cost = self._usage
        return Completion(
            value=parse_answer(data, schema, provider=self.name),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_usd=cost,
            warnings=self._warnings,
        )


class Echo(Model):
    """A minimal answer schema for tests."""

    ok: bool


class TestFakeRunner:
    def request(self, *argv: str) -> CliRequest:
        return CliRequest(argv=argv, stdin="", env={}, cwd=Path("."), timeout=1.0)

    def test_it_is_a_runner_and_plays_its_script_in_order(self) -> None:
        runner: CliRunner = FakeRunner(CliResult(0, "one", ""), CliTimeoutError())
        assert runner(self.request("client", "-p")).stdout == "one"
        with pytest.raises(CliTimeoutError):
            runner(self.request("client", "-p"))
        with pytest.raises(AssertionError, match="no reply scripted"):
            runner(self.request("client", "-p"))

    def test_local_commands_do_not_use_up_the_script(self) -> None:
        runner = FakeRunner(CliResult(0, "answer", ""), version="1.2.3")
        assert runner(self.request("client", "--version")).stdout.startswith("1.2.3")
        assert "ChatGPT" in runner(self.request("client", "login", "status")).stderr
        assert runner(self.request("client", "exec")).stdout == "answer"
        assert [request.argv[1] for request in runner.calls] == ["exec"]
        assert len(runner.requests) == 3

    def test_a_reply_can_be_computed_from_the_request(self) -> None:
        runner = FakeRunner(lambda request: CliResult(0, request.stdin.upper(), ""))
        request = CliRequest(argv=("c",), stdin="abc", env={}, cwd=Path("."), timeout=1.0)
        assert runner(request).stdout == "ABC"


class TestStubProvider:
    def test_it_is_a_provider_and_a_client(self) -> None:
        stub = StubProvider("stub", {"ok": True})
        provider: Provider = stub
        assert isinstance(stub, LLMClient)
        assert provider.complete_json("s", "u", Echo, max_tokens=5) == Echo(ok=True)
        assert stub.calls == [("s", "u", Echo)]

    def test_errors_and_invalid_answers_behave_like_a_real_backend(self) -> None:
        stub = StubProvider("stub", LLMAuthError("no", provider="stub"), {"ok": "maybe?"})
        with pytest.raises(LLMAuthError):
            stub.complete("s", "u", Echo, max_tokens=5)
        with pytest.raises(LLMError, match="does not match"):
            stub.complete("s", "u", Echo, max_tokens=5)
        with pytest.raises(AssertionError, match="has no reply"):
            stub.complete("s", "u", Echo, max_tokens=5)

    def test_always_and_usage(self) -> None:
        stub = StubProvider("stub", usage=(7, 3, 0.5), warnings=("note",)).always({"ok": False})
        completions = [stub.complete("s", "u", Echo, max_tokens=5) for _ in range(3)]
        assert completions == [Completion(Echo(ok=False), 7, 3, 0.5, ("note",))] * 3
