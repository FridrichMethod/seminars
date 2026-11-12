"""Running a command line client as a child process: arguments, input, environment, timeout.

The children here are tiny Python programs. Nothing is sent to a network and
no real client is started.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from seminars.llm.cli import (
    PASSED_THROUGH,
    CliRequest,
    CliResult,
    CliStartError,
    CliTimeoutError,
    child_environment,
    end_process_tree,
    installed_version,
    parse_version,
    run_cli,
    version_warning,
)

FAKE_TOKEN = "-".join(["ghp", "never", "passed" * 5])


def python(code: str, *arguments: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code, *arguments)


def request(
    argv: tuple[str, ...],
    cwd: Path,
    *,
    stdin: str = "",
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> CliRequest:
    return CliRequest(argv=argv, stdin=stdin, env=env or {}, cwd=cwd, timeout=timeout)


class TestRunCli:
    def test_output_and_exit_status_come_back(self, tmp_path: Path) -> None:
        code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
        result = run_cli(request(python(code), tmp_path))
        assert result == CliResult(returncode=3, stdout="out\n", stderr="err\n")

    def test_the_prompt_goes_to_standard_input(self, tmp_path: Path) -> None:
        prompt = "page text with 'quotes', $(commands) and `ticks`; rm -rf /\n" * 3
        result = run_cli(
            request(
                python("import sys; sys.stdout.write(sys.stdin.read())"), tmp_path, stdin=prompt
            )
        )
        assert result.stdout == prompt

    def test_arguments_are_passed_as_they_are_without_a_shell(self, tmp_path: Path) -> None:
        arguments = ("", "a b", "$HOME", "; echo no", "*", "--flag=x y")
        code = "import sys, json; print(json.dumps(sys.argv[1:]))"
        result = run_cli(request(python(code, *arguments), tmp_path))
        assert result.stdout.strip() == ('["", "a b", "$HOME", "; echo no", "*", "--flag=x y"]')

    def test_the_child_sees_only_the_given_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", FAKE_TOKEN)
        monkeypatch.setenv("OPENAI_API_KEY", FAKE_TOKEN)
        code = "import os, json; print(json.dumps(sorted(os.environ)))"
        result = run_cli(request(python(code), tmp_path, env={"ONLY": "this"}))
        names = set(json.loads(result.stdout))
        assert "ONLY" in names
        assert not names & {"GITHUB_TOKEN", "OPENAI_API_KEY", "HOME", "USER"}
        assert FAKE_TOKEN not in result.stdout

    def test_the_child_runs_in_the_given_directory(self, tmp_path: Path) -> None:
        result = run_cli(request(python("import os; print(os.getcwd())"), tmp_path))
        assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()

    def test_non_ascii_text_survives_both_ways(self, tmp_path: Path) -> None:
        text = "Poincar" + chr(0xE9) + " " + chr(0x4E2D) + chr(0x6587)
        code = "import sys; data = sys.stdin.buffer.read(); sys.stdout.buffer.write(data)"
        assert run_cli(request(python(code), tmp_path, stdin=text)).stdout == text

    def test_output_that_is_not_utf8_does_not_fail(self, tmp_path: Path) -> None:
        code = "import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe end')"
        result = run_cli(request(python(code), tmp_path))
        assert result.stdout.startswith("ok ")
        assert result.stdout.endswith(" end")

    def test_a_lone_surrogate_in_the_prompt_does_not_fail(self, tmp_path: Path) -> None:
        code = "import sys; print(len(sys.stdin.buffer.read()))"
        result = run_cli(request(python(code), tmp_path, stdin="a" + chr(0xD83D) + "b"))
        assert result.stdout.strip() == "3"

    def test_a_missing_command_is_a_start_error(self, tmp_path: Path) -> None:
        with pytest.raises(CliStartError) as caught:
            run_cli(request((str(tmp_path / "no-such-client"), "--version"), tmp_path))
        assert str(tmp_path) not in str(caught.value)

    def test_a_file_that_is_not_executable_is_a_start_error(self, tmp_path: Path) -> None:
        script = tmp_path / "client"
        script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        script.chmod(0o600)
        with pytest.raises(CliStartError):
            run_cli(request((str(script),), tmp_path))

    def test_a_child_that_takes_too_long_is_ended(self, tmp_path: Path) -> None:
        started = time.monotonic()
        with pytest.raises(CliTimeoutError):
            run_cli(request(python("import time; time.sleep(60)"), tmp_path, timeout=0.3))
        assert time.monotonic() - started < 10

    @pytest.mark.skipif(not hasattr(os, "killpg"), reason="needs process groups")
    def test_ending_a_child_ends_what_it_started_as_well(self) -> None:
        """Both hold the output pipe; it reaches its end only when both are gone."""
        grandchild = "import time; time.sleep(8)"
        code = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
            "print('started', flush=True); time.sleep(8)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, start_new_session=True
        )
        assert process.stdout is not None
        assert process.stdout.readline() == b"started\n"
        started = time.monotonic()
        end_process_tree(process)
        assert process.stdout.read() == b""
        process.wait()
        process.stdout.close()
        assert time.monotonic() - started < 5

    def test_ending_a_child_that_is_gone_already_is_harmless(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        process.wait()
        end_process_tree(process)


class TestChildEnvironment:
    def test_only_the_general_variables_and_the_extras_are_passed(self) -> None:
        parent = {
            "PATH": "/usr/bin",
            "HOME": "/home/someone",
            "LANG": "C.UTF-8",
            "GITHUB_TOKEN": FAKE_TOKEN,
            "ANTHROPIC_API_KEY": FAKE_TOKEN,
            "CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN,
            "SSH_AUTH_SOCK": "/tmp/agent",
        }
        env = child_environment(parent, {"CLAUDE_CONFIG_DIR": "/tmp/x"})
        assert env == {
            "PATH": "/usr/bin",
            "HOME": "/home/someone",
            "LANG": "C.UTF-8",
            "CLAUDE_CONFIG_DIR": "/tmp/x",
        }

    def test_unset_and_empty_variables_are_left_out(self) -> None:
        assert child_environment({"PATH": "", "HOME": "/h"}, {}) == {"HOME": "/h"}

    def test_an_extra_wins_over_the_parent(self) -> None:
        assert child_environment({"HOME": "/h"}, {"HOME": "/tmp/home"}) == {"HOME": "/tmp/home"}

    def test_the_list_of_general_variables(self) -> None:
        assert PASSED_THROUGH == ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


class TestVersions:
    @pytest.mark.parametrize(
        ("text", "version"),
        [
            ("2.1.322 (Claude Code)\n", "2.1.322"),
            ("codex-cli 0.167.0\n", "0.167.0"),
            ("v10.3.3", "10.3.3"),
            ("no version here", None),
            ("1.2", None),
            ("1.2.3.4", None),
            ("", None),
        ],
    )
    def test_parse_version(self, text: str, version: str | None) -> None:
        assert parse_version(text) == version

    def test_installed_version_asks_the_client(self, tmp_path: Path) -> None:
        seen: list[CliRequest] = []

        def runner(request: CliRequest) -> CliResult:
            seen.append(request)
            return CliResult(0, "codex-cli 0.167.0\n", "")

        assert installed_version(runner, "codex", {"PATH": "/bin"}, tmp_path) == "0.167.0"
        assert seen[0].argv == ("codex", "--version")
        assert seen[0].stdin == ""
        assert seen[0].env == {"PATH": "/bin"}
        assert seen[0].timeout <= 30

    @pytest.mark.parametrize("failure", [CliStartError("x"), CliTimeoutError()])
    def test_a_client_that_does_not_start_has_no_version(
        self, tmp_path: Path, failure: Exception
    ) -> None:
        def runner(request: CliRequest) -> CliResult:
            raise failure

        assert installed_version(runner, "codex", {}, tmp_path) is None

    def test_a_failing_version_command_has_no_version(self, tmp_path: Path) -> None:
        def runner(request: CliRequest) -> CliResult:
            return CliResult(2, "9.9.9", "boom")

        assert installed_version(runner, "codex", {}, tmp_path) is None

    def test_version_warning(self) -> None:
        assert version_warning("codex", "0.167.0", "0.167.0") is None
        assert version_warning("codex", "0.168.1", None) is None
        assert version_warning("codex", "0.168.1", "0.167.0") == (
            "codex CLI version 0.168.1 differs from the verified version 0.167.0"
        )
        assert version_warning("claude", None, "2.1.322") == (
            "the version of the claude CLI could not be determined (verified: 2.1.322)"
        )
