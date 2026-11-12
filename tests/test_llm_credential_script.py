"""scripts/set-llm-credential.sh, run against a fake gh and a fake codex.

The fakes sit first on PATH and record every call (arguments and standard
input) as JSON lines. The real gh is never reached: PATH holds only the
fake directory and the system directories after it, gh gets an empty
configuration directory and no token, and every test checks first that
"gh" resolves to the fake. The secret files are throwaway files in the
temporary directory of the test with a value that is put together at run
time.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

from test_llm_fakes import FAKE_TOKEN

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
SCRIPT: Final = REPO_ROOT / "scripts" / "set-llm-credential.sh"
SYSTEM_PATH: Final = "/usr/bin:/bin"
ACCOUNT: Final = "owner@example.com"

FAKE_GH: Final = f"""#!{sys.executable} -I
import json, os, sys
argv = sys.argv[1:]
record = {{"argv": argv, "stdin": None}}
if argv[:2] == ["secret", "set"] and not sys.stdin.isatty():
    record["stdin"] = sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"tool": "gh", **record}}) + "\\n")
if argv[:2] == ["auth", "status"]:
    sys.exit(int(os.environ.get("FAKE_GH_AUTH_STATUS", "0")))
sys.exit(int(os.environ.get("FAKE_GH_STATUS", "0")))
"""

FAKE_CODEX: Final = f"""#!{sys.executable} -I
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"tool": "codex", "argv": argv, "stdin": None}}) + "\\n")
if argv == ["--version"]:
    print("codex-cli " + os.environ.get("FAKE_CODEX_VERSION", "0.167.0"))
elif argv == ["login", "status"]:
    sys.stderr.write(os.environ.get("FAKE_CODEX_LOGIN", "Logged in using ChatGPT") + "\\n")
"""


@dataclass(frozen=True)
class Outcome:
    returncode: int
    stdout: str
    stderr: str
    calls: list[dict[str, Any]]

    @property
    def gh_calls(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls if call["tool"] == "gh"]

    def stdin_of(self, *argv: str) -> str | None:
        for call in self.calls:
            if call["tool"] == "gh" and call["argv"] == list(argv):
                return call["stdin"]
        raise AssertionError(f"gh was not called with {list(argv)}")


class Sandbox:
    """A temporary bin directory with the fakes, and a way to run the script with it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "calls.jsonl"
        self.env: dict[str, str] = {
            "PATH": f"{self.bin}:{SYSTEM_PATH}",
            "HOME": str(root / "home"),
            "GH_CONFIG_DIR": str(root / "gh-config"),
            "FAKE_LOG": str(self.log),
            "LANG": "C",
        }
        self.install("gh", FAKE_GH)

    def install(self, name: str, text: str) -> None:
        path = self.bin / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def secret_file(self, value: str = FAKE_TOKEN + "\n", *, mode: int = 0o600) -> Path:
        path = self.root / "secret"
        path.write_text(value, encoding="utf-8")
        path.chmod(mode)
        return path

    def run(self, *args: str | Path, stdin: str | None = None, **env: str) -> Outcome:
        environment = {**self.env, **env}
        found = shutil.which("gh", path=environment["PATH"])
        assert found == str(self.bin / "gh"), "the fake gh must come first on PATH"
        result = subprocess.run(
            ["bash", str(SCRIPT), *map(str, args)],
            input=stdin if stdin is not None else "",
            capture_output=True,
            text=True,
            env=environment,
            cwd=self.root,
            timeout=60,
            check=False,
        )
        calls = []
        if self.log.exists():
            lines = self.log.read_text(encoding="utf-8").splitlines()
            calls = [json.loads(line) for line in lines]
            self.log.unlink()
        return Outcome(result.returncode, result.stdout, result.stderr, calls)


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def assert_value_stays_hidden(outcome: Outcome) -> None:
    assert FAKE_TOKEN not in outcome.stdout
    assert FAKE_TOKEN not in outcome.stderr
    for call in outcome.calls:
        assert all(FAKE_TOKEN not in argument for argument in call["argv"])


class TestSettingASecret:
    @pytest.mark.parametrize(
        ("provider", "secret"),
        [
            ("claude-cli", "CLAUDE_CODE_OAUTH_TOKEN"),
            ("anthropic", "ANTHROPIC_API_KEY"),
            ("openai", "OPENAI_API_KEY"),
            ("openai-compat", "LLM_API_KEY"),
        ],
    )
    def test_the_value_of_a_file_goes_to_gh_on_standard_input(
        self, sandbox: Sandbox, provider: str, secret: str
    ) -> None:
        outcome = sandbox.run(provider, "--from-file", sandbox.secret_file())
        assert outcome.returncode == 0, outcome.stderr
        assert outcome.gh_calls == [["auth", "status"], ["secret", "set", secret]]
        assert outcome.stdin_of("secret", "set", secret) == FAKE_TOKEN
        assert f"Secret {secret} is set." in outcome.stdout
        assert_value_stays_hidden(outcome)

    def test_line_endings_of_the_file_are_removed(self, sandbox: Sandbox) -> None:
        path = sandbox.secret_file(FAKE_TOKEN + "\r\n\n")
        outcome = sandbox.run("openai", "--from-file", path)
        assert outcome.stdin_of("secret", "set", "OPENAI_API_KEY") == FAKE_TOKEN

    def test_the_repository_is_passed_on(self, sandbox: Sandbox) -> None:
        path = sandbox.secret_file()
        outcome = sandbox.run("claude-cli", "--from-file", path, "--repo", "owner/seminars")
        secret = ["secret", "set", "CLAUDE_CODE_OAUTH_TOKEN", "--repo", "owner/seminars"]
        assert outcome.gh_calls[-1] == secret
        assert outcome.stdin_of(*secret) == FAKE_TOKEN

    def test_a_file_readable_only_by_its_owner_is_accepted(self, sandbox: Sandbox) -> None:
        outcome = sandbox.run("openai", "--from-file", sandbox.secret_file(mode=0o400))
        assert outcome.returncode == 0, outcome.stderr

    def test_a_link_to_a_private_file_is_accepted(self, sandbox: Sandbox) -> None:
        link = sandbox.root / "link"
        link.symlink_to(sandbox.secret_file())
        outcome = sandbox.run("openai", "--from-file", link)
        assert outcome.stdin_of("secret", "set", "OPENAI_API_KEY") == FAKE_TOKEN

    def test_a_failing_gh_fails_the_script(self, sandbox: Sandbox) -> None:
        path = sandbox.secret_file()
        outcome = sandbox.run("openai", "--from-file", path, FAKE_GH_STATUS="1")
        assert outcome.returncode != 0
        assert "is set" not in outcome.stdout
        assert_value_stays_hidden(outcome)

    def test_without_a_file_and_without_a_terminal_nothing_is_read(self, sandbox: Sandbox) -> None:
        outcome = sandbox.run("anthropic", stdin=FAKE_TOKEN)
        assert outcome.returncode == 2
        assert "use --from-file" in outcome.stderr
        assert outcome.gh_calls == [["auth", "status"]]
        assert_value_stays_hidden(outcome)

    def test_gh_must_be_logged_in(self, sandbox: Sandbox) -> None:
        path = sandbox.secret_file()
        outcome = sandbox.run("openai", "--from-file", path, FAKE_GH_AUTH_STATUS="1")
        assert outcome.returncode == 2
        assert "gh auth login" in outcome.stderr
        assert outcome.gh_calls == [["auth", "status"]]


class TestRefusedFiles:
    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o620, 0o602, 0o660, 0o666, 0o700])
    def test_a_file_that_others_may_access_is_refused(self, sandbox: Sandbox, mode: int) -> None:
        path = sandbox.secret_file(mode=mode)
        outcome = sandbox.run("claude-cli", "--from-file", path)
        if mode & 0o077:
            assert outcome.returncode == 2
            assert "chmod 600" in outcome.stderr
            assert outcome.gh_calls == []
        else:
            assert outcome.returncode == 0  # 700: only the owner, even if executable
        assert_value_stays_hidden(outcome)

    def test_a_link_to_a_readable_file_is_refused(self, sandbox: Sandbox) -> None:
        link = sandbox.root / "link"
        link.symlink_to(sandbox.secret_file(mode=0o644))
        outcome = sandbox.run("openai", "--from-file", link)
        assert outcome.returncode == 2
        assert outcome.gh_calls == []

    def test_missing_empty_and_odd_files_are_refused(self, sandbox: Sandbox) -> None:
        cases = {
            "no such file": sandbox.root / "missing",
            "not a regular file": sandbox.root,
            "the file is empty": sandbox.secret_file(" \n\n"),
        }
        for message, path in cases.items():
            outcome = sandbox.run("openai", "--from-file", path)
            assert outcome.returncode == 2
            assert message in outcome.stderr
            assert outcome.gh_calls == []


class TestOtherActions:
    def test_delete_removes_the_secret(self, sandbox: Sandbox) -> None:
        outcome = sandbox.run("anthropic", "--delete")
        assert outcome.gh_calls[-1] == ["secret", "delete", "ANTHROPIC_API_KEY"]

    def test_none_switches_the_llm_off(self, sandbox: Sandbox) -> None:
        outcome = sandbox.run("none")
        assert outcome.returncode == 0
        assert outcome.gh_calls[-1] == ["variable", "set", "LLM_PROVIDERS", "--body", "none"]
        assert not any(call[:1] == ["secret"] for call in outcome.gh_calls)

    def test_variables_for_the_compatible_service(self, sandbox: Sandbox) -> None:
        path = sandbox.secret_file()
        outcome = sandbox.run(
            "openai-compat",
            "--from-file",
            path,
            "--base-url",
            "https://llm.example.org/v1",
            "--model",
            "small-model",
            "--providers",
            "claude-cli,openai-compat",
        )
        assert outcome.returncode == 0, outcome.stderr
        assert outcome.gh_calls[2:] == [
            ["variable", "set", "LLM_BASE_URL", "--body", "https://llm.example.org/v1"],
            ["variable", "set", "LLM_MODEL_OPENAI_COMPAT", "--body", "small-model"],
            ["variable", "set", "LLM_PROVIDERS", "--body", "claude-cli,openai-compat"],
        ]

    def test_smoke_starts_the_probe_run(self, sandbox: Sandbox) -> None:
        outcome = sandbox.run("claude-cli", "--from-file", sandbox.secret_file(), "--smoke")
        assert outcome.gh_calls[-1] == ["workflow", "run", "update.yml", "-f", "mode=llm-smoke"]

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            ((), "Usage:"),
            (("gemini",), "unknown provider: gemini"),
            (("openai", "anthropic"), "only one provider at a time"),
            (("openai", "--frobnicate"), "unknown option"),
            (("openai", "--from-file"), "--from-file needs a path"),
            (("openai", "--providers", "claude-cli,gemini"), "unknown provider in --providers"),
            (("openai", "--base-url", "https://x.example.org"), "--base-url is for openai-compat"),
            (("openai-compat", "--base-url", "http://x.example.org"), "must start with https://"),
            (("openai", "--model", "two words"), "printable ASCII characters without spaces"),
            (("openai", "--repo", "not a repo"), "--repo must be OWNER/NAME"),
            (("openai", "--delete", "--from-file", "x"), "do not go together"),
            (("none", "--from-file", "x"), "none has no secret"),
            (("none", "--delete"), "none has no secret"),
            (("none", "--model", "m"), "none takes no --model"),
            (("codex-cli", "--providers", "codex-cli"), "codex-cli takes no --providers"),
            (("codex-cli", "--smoke"), "codex-cli takes no --smoke"),
        ],
    )
    def test_wrong_use_is_refused_before_gh_is_called(
        self, sandbox: Sandbox, args: tuple[str, ...], message: str
    ) -> None:
        outcome = sandbox.run(*args)
        assert outcome.returncode == 2
        assert message in outcome.stderr
        assert outcome.gh_calls == []

    def test_help(self, sandbox: Sandbox) -> None:
        outcome = sandbox.run("--help")
        assert outcome.returncode == 0
        assert "--from-file PATH" in outcome.stdout
        assert outcome.calls == []


class TestCodexCli:
    def test_only_the_local_client_is_checked_and_no_secret_is_set(self, sandbox: Sandbox) -> None:
        sandbox.install("codex", FAKE_CODEX)
        outcome = sandbox.run("codex-cli", FAKE_CODEX_LOGIN=f"Logged in using ChatGPT ({ACCOUNT})")
        assert outcome.returncode == 0, outcome.stderr
        assert outcome.gh_calls == []
        assert "codex: installed" in outcome.stdout
        assert "login: ChatGPT" in outcome.stdout
        assert "version: 0.167.0" in outcome.stdout
        assert "LLM_PROVIDERS=claude-cli,codex-cli seminars run" in outcome.stdout
        assert ACCOUNT not in outcome.stdout + outcome.stderr

    def test_another_version_is_reported(self, sandbox: Sandbox) -> None:
        sandbox.install("codex", FAKE_CODEX)
        outcome = sandbox.run("codex-cli", FAKE_CODEX_VERSION="0.170.1")
        assert outcome.returncode == 0
        assert "0.170.1 differs from the verified 0.167.0" in outcome.stdout

    def test_a_missing_chatgpt_login_is_reported(self, sandbox: Sandbox) -> None:
        sandbox.install("codex", FAKE_CODEX)
        outcome = sandbox.run("codex-cli", FAKE_CODEX_LOGIN="Logged in using an API key")
        assert outcome.returncode == 1
        assert "run: codex login" in outcome.stdout
        assert outcome.gh_calls == []

    def test_a_missing_client_is_reported(self, sandbox: Sandbox) -> None:
        outcome = sandbox.run("codex-cli")
        assert outcome.returncode == 1
        assert "codex: not installed" in outcome.stdout
        assert outcome.gh_calls == []


class TestTheScriptItself:
    def test_it_knows_only_the_allowed_secret_names(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        for name in ("CODEX" + "_AUTH_JSON", "SECRETS" + "_WRITER_PAT", "auth" + ".json"):
            assert name not in text

    def test_it_is_executable(self) -> None:
        assert SCRIPT.stat().st_mode & stat.S_IXUSR

    @pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
    def test_shellcheck_finds_nothing(self) -> None:
        result = subprocess.run(
            ["shellcheck", str(SCRIPT)], capture_output=True, text=True, timeout=60, check=False
        )
        assert result.returncode == 0, result.stdout

    def test_no_value_is_printed_even_with_tracing_in_the_environment(
        self, sandbox: Sandbox
    ) -> None:
        path = sandbox.secret_file()
        outcome = sandbox.run("openai", "--from-file", path, SHELLOPTS="xtrace")
        assert_value_stays_hidden(outcome)
