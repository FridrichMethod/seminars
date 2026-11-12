"""What the two command line providers see of the environment, checked where a child starts.

``subprocess.Popen`` is replaced by a stand-in that records how each child
would be started and answers the way the client would. No real CLI runs:
the parent environment points PATH at a directory that does not exist, so
even a stand-in that failed to apply could not start one. The providers are
built the way a run builds them (``build_llm_client`` and ``probe_providers``
over the real ``run_cli``), from an environment that holds every variable
that must not reach a child, as the update workflow's environment does.
Every credential-shaped value is put together at run time.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

import pytest

from seminars.config import LLMConfig, apply_llm_env
from seminars.llm import claude_cli, cli, codex_cli
from seminars.llm.resolve import build_llm_client, probe_providers
from seminars.models import Model

TOKEN: Final = "-".join(["sk", "ant", "oat01", "env", "check" * 8])


def credential(prefix: str) -> str:
    return prefix + "_" + "Z9y8X7w6V5u4" * 4


GENERAL: Final[dict[str, str]] = {
    "PATH": "/nonexistent/seminars-test/bin",
    "HOME": "/home/runner",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TMPDIR": "/tmp/seminars-test-runner",
}
"""The variables of the parent that every child gets."""

FORBIDDEN: Final[dict[str, str]] = {
    "GITHUB_TOKEN": credential("ghs"),
    "GH_TOKEN": credential("gho"),
    "GITHUB_REPOSITORY": "owner/seminars",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": credential("oidc"),
    "ANTHROPIC_API_KEY": "sk-" + "ant-" + "api03-" + "Q1w2E3r4T5y6" * 4,
    "ANTHROPIC_AUTH_TOKEN": credential("auth"),
    "ANTHROPIC_BASE_URL": "https://llm-relay.example.org",
    "OPENAI_API_KEY": "sk-" + "proj-" + "P0o9I8u7Y6t5" * 4,
    "OPENAI_BASE_URL": "https://llm-relay.example.org/v1",
    "LLM_API_KEY": credential("compat"),
    "LLM_BASE_URL": "https://llm.example.org/v1",
    "HTTPS_PROXY": "http://someone:" + credential("pw") + "@proxy.example.org:3128",
    "HTTP_PROXY": "http://proxy.example.org:3128",
    "ALL_PROXY": "socks5://proxy.example.org:1080",
    "SSL_CERT_FILE": "/home/runner/other-ca.pem",
    "NODE_EXTRA_CA_CERTS": "/home/runner/other-ca.pem",
    "NODE_OPTIONS": "--require /home/runner/hook.js",
    "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1001/bus",
    "AWS_SECRET_ACCESS_KEY": credential("aws"),
    "CLAUDECODE": "1",
    "USER": "runner",
    "SHELL": "/bin/bash",
}
"""Variables of the parent that no child may get."""

SECRET_VALUES: Final = {
    name: value
    for name, value in FORBIDDEN.items()
    if name.endswith(("TOKEN", "KEY")) or name == "HTTPS_PROXY"
}
"""The forbidden values that are credentials: they must not show up anywhere in a child."""

PARENT: Final[dict[str, str]] = {
    **GENERAL,
    "CLAUDE_CODE_OAUTH_TOKEN": f"  {TOKEN}\n",
    "CLAUDE_CONFIG_DIR": "/home/runner/.claude-of-the-owner",
    "CODEX_HOME": "/home/runner/.codex-of-the-owner",
    **FORBIDDEN,
}

QUIET: Final[dict[str, str]] = {
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}


class Tags(Model):
    fields: list[Literal["math", "bio"]]
    is_talk: bool


@dataclass
class Started:
    """How one child was started, and what it saw while it ran."""

    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: Path
    options: dict[str, Any]
    stdin: str = ""
    seen: dict[str, bool] = field(default_factory=dict[str, bool])


def _answer_for(schema: Mapping[str, Any]) -> dict[str, Any]:
    if "ok" in schema.get("properties", {}):
        return {"ok": True}
    return {"fields": ["bio"], "is_talk": True}


def _claude(started: Started) -> tuple[int, str, str]:
    if started.argv[1:] == ("--version",):
        return 0, "2.1.322 (Claude Code)\n", ""
    config_dir = started.env.get("CLAUDE_CONFIG_DIR")
    started.seen["config_dir_exists"] = config_dir is not None and Path(config_dir).is_dir()
    schema = json.loads(started.argv[started.argv.index("--json-schema") + 1])
    document = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "structured_output": _answer_for(schema),
        "usage": {"input_tokens": 5, "output_tokens": 2},
        "total_cost_usd": 0.0001,
    }
    return 0, json.dumps(document) + "\n", ""


def _codex(started: Started) -> tuple[int, str, str]:
    if started.argv[1:] == ("--version",):
        return 0, "codex-cli 0.167.0\n", ""
    if started.argv[1:] == ("login", "status"):
        return 0, "", "Logged in using ChatGPT\n"
    schema = json.loads(Path(started.argv[started.argv.index("--output-schema") + 1]).read_text())
    answer_file = Path(started.argv[started.argv.index("-o") + 1])
    answer_file.write_text(json.dumps(_answer_for(schema)), encoding="utf-8")
    usage = {"type": "turn.completed", "usage": {"input_tokens": 23000, "output_tokens": 9}}
    return 0, json.dumps(usage) + "\n", ""


class FakeProcess:
    """The part of ``subprocess.Popen`` that ``seminars.llm.cli.run_cli`` uses."""

    pid: Final = 2**22 + 12345

    def __init__(self, started: Started) -> None:
        self._started = started
        self.returncode: int | None = None

    def communicate(
        self, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes, bytes]:
        self._started.stdin = (input or b"").decode("utf-8")
        reply = _claude if self._started.argv[0] == "claude" else _codex
        self.returncode, stdout, stderr = reply(self._started)
        return stdout.encode("utf-8"), stderr.encode("utf-8")

    def kill(self) -> None:
        """Nothing to end: no process was started."""


class FakeChildren:
    """Stands in for ``subprocess.Popen`` and records every child."""

    def __init__(self) -> None:
        self.started: list[Started] = []

    def __call__(self, args: list[str], **options: Any) -> FakeProcess:
        env = options.get("env")
        assert isinstance(env, dict), "a child must never inherit the environment"
        started = Started(tuple(args), dict(env), Path(options["cwd"]), dict(options))
        self.started.append(started)
        return FakeProcess(started)

    def by_argv(self, *tail: str) -> list[Started]:
        return [started for started in self.started if started.argv[1:] == tail]

    def requests(self) -> list[Started]:
        local = (("--version",), ("login", "status"))
        return [started for started in self.started if started.argv[1:] not in local]


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> FakeChildren:
    fake = FakeChildren()
    monkeypatch.setattr(subprocess, "Popen", fake)
    return fake


def config_for(env: Mapping[str, str]) -> LLMConfig:
    base = LLMConfig.model_validate(
        {
            "models": {"claude-cli": "haiku", "codex-cli": "gpt-6-luna"},
            "cli_versions": {"claude-cli": "2.1.322", "codex-cli": "0.167.0"},
        }
    )
    return apply_llm_env(base, env)


def run_chain(env: Mapping[str, str]) -> Tags:
    chain = build_llm_client(config_for(env), env)
    assert chain is not None
    return chain.complete_json("Tag the events.", "the events", Tags, max_tokens=64)


class TestClaudeInTheUpdateWorkflow:
    ENV: Final = {**PARENT, "GITHUB_ACTIONS": "true", "LLM_PROVIDERS": "claude-cli"}

    def test_a_request_gets_exactly_the_listed_variables(self, children: FakeChildren) -> None:
        assert run_chain(self.ENV) == Tags(fields=["bio"], is_talk=True)
        [request] = children.requests()
        config_dir = request.env["CLAUDE_CONFIG_DIR"]
        assert request.env == {
            **GENERAL,
            "CLAUDE_CODE_OAUTH_TOKEN": TOKEN,
            "CLAUDE_CONFIG_DIR": config_dir,
            **QUIET,
        }

    def test_the_configuration_directory_is_a_private_temporary_one(
        self, children: FakeChildren
    ) -> None:
        run_chain(self.ENV)
        [request] = children.requests()
        config_dir = Path(request.env["CLAUDE_CONFIG_DIR"])
        assert request.seen["config_dir_exists"]
        assert config_dir.parent == request.cwd.parent
        assert str(config_dir) != PARENT["CLAUDE_CONFIG_DIR"]
        assert not config_dir.exists()

    def test_the_version_check_gets_the_general_variables_and_no_login(
        self, children: FakeChildren
    ) -> None:
        run_chain(self.ENV)
        [version] = children.by_argv("--version")
        assert version.env == {**GENERAL, **QUIET}

    def test_llm_smoke_starts_it_the_same_way(self, children: FakeChildren) -> None:
        [result] = probe_providers(config_for(self.ENV), self.ENV)
        assert result.status == "ok"
        [request] = children.requests()
        assert request.env == {
            **GENERAL,
            "CLAUDE_CODE_OAUTH_TOKEN": TOKEN,
            "CLAUDE_CONFIG_DIR": request.env["CLAUDE_CONFIG_DIR"],
            **QUIET,
        }


class TestCodexOnTheMachineOfTheOwner:
    ENV: Final = {**PARENT, "LLM_PROVIDERS": "codex-cli"}

    def test_every_child_gets_exactly_the_listed_variables(self, children: FakeChildren) -> None:
        assert run_chain(self.ENV) == Tags(fields=["bio"], is_talk=True)
        kinds = [started.argv[1:2] for started in children.started]
        assert kinds == [("login",), ("--version",), ("exec",)]
        for started in children.started:
            assert started.env == {**GENERAL, "CODEX_HOME": PARENT["CODEX_HOME"]}

    def test_without_codex_home_only_the_general_variables_are_left(
        self, children: FakeChildren
    ) -> None:
        env = {name: value for name, value in self.ENV.items() if name != "CODEX_HOME"}
        run_chain(env)
        assert [started.env for started in children.started] == [GENERAL] * 3

    def test_not_even_the_claude_token_reaches_it(self, children: FakeChildren) -> None:
        run_chain(self.ENV)
        for started in children.started:
            assert TOKEN not in " ".join([*started.env.values(), *started.argv, started.stdin])


class TestNothingElseLeaks:
    ENV: Final = {**PARENT, "LLM_PROVIDERS": "claude-cli,codex-cli"}

    def test_no_forbidden_name_or_value_reaches_any_child(self, children: FakeChildren) -> None:
        probe_providers(config_for(self.ENV), self.ENV)
        assert {started.argv[0] for started in children.started} == {"claude", "codex"}
        for started in children.started:
            seen = " ".join([*started.env.values(), *started.argv, started.stdin])
            for name in FORBIDDEN:
                assert name not in started.env
            for name, value in SECRET_VALUES.items():
                assert value not in seen, name

    def test_every_child_stays_within_the_list_of_its_provider(
        self, children: FakeChildren
    ) -> None:
        probe_providers(config_for(self.ENV), self.ENV)
        for started in children.started:
            allowed = (
                claude_cli.CHILD_ENV_NAMES
                if started.argv[0] == "claude"
                else codex_cli.CHILD_ENV_NAMES
            )
            assert set(started.env) <= allowed

    def test_a_child_is_started_without_a_shell_in_a_session_of_its_own(
        self, children: FakeChildren
    ) -> None:
        probe_providers(config_for(self.ENV), self.ENV)
        for started in children.started:
            assert started.options["shell"] is False
            assert started.options["start_new_session"] is True


class TestTheLists:
    def test_the_general_variables(self) -> None:
        assert set(cli.PASSED_THROUGH) == set(GENERAL)

    def test_the_variables_of_claude(self) -> None:
        listed = set(claude_cli.CHILD_ENV_NAMES)
        assert listed == {*GENERAL, "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", *QUIET}

    def test_the_variables_of_codex(self) -> None:
        assert set(codex_cli.CHILD_ENV_NAMES) == {*GENERAL, "CODEX_HOME"}

    def test_no_listed_variable_is_a_credential_of_another_provider(self) -> None:
        listed = claude_cli.CHILD_ENV_NAMES | codex_cli.CHILD_ENV_NAMES
        assert not listed & set(FORBIDDEN)


DOCS: Final = Path(__file__).resolve().parents[1] / "docs"


class TestTheListsAreWrittenDown:
    NEVER: Final = (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "LLM_API_KEY",
        "ANTHROPIC_BASE_URL",
        "OPENAI_BASE_URL",
        "HTTPS_PROXY",
        "SSL_CERT_FILE",
        "NODE_EXTRA_CA_CERTS",
        "NODE_OPTIONS",
        "SSH_AUTH_SOCK",
        "DBUS_SESSION_BUS_ADDRESS",
    )

    @pytest.mark.parametrize("name", ["LLM_CREDENTIALS.md", "LLM_CREDENTIALS.zh-CN.md"])
    def test_the_documents_name_every_variable(self, name: str) -> None:
        text = (DOCS / name).read_text(encoding="utf-8")
        listed = claude_cli.CHILD_ENV_NAMES | codex_cli.CHILD_ENV_NAMES
        for variable in (*sorted(listed), *self.NEVER):
            assert f"`{variable}`" in text, variable

    def test_the_module_docstrings_name_every_variable(self) -> None:
        for module in (claude_cli, codex_cli):
            assert module.__doc__ is not None
            for variable in module.CHILD_ENV_NAMES:
                assert variable in module.__doc__, (module.__name__, variable)
        assert cli.__doc__ is not None
        for variable in (*cli.PASSED_THROUGH, *self.NEVER):
            assert variable in cli.__doc__, variable
