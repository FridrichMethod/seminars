"""Checks of the GitHub Actions workflows and of the user documentation.

A workflow only runs on GitHub, after a push, so this file checks
everything about them that can be checked on one machine:

* the security rules, by parsing the YAML: triggers, permissions, where
  secrets and the token go, pinned actions, no expression inside a script;
* the schedules, the concurrency groups, the Pages deployment and the
  commands each job runs;
* the shell scripts of the steps that decide what is committed and
  deployed, by running them with bash against stub commands and throwaway
  git repositories (nothing leaves the machine).

actionlint (run by ci.yml) checks the syntax and the scripts on top.

The README and the contributor guide exist in English and Chinese; the
last part of this file keeps the two versions in step.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import pytest
import yaml

from seminars.config import (
    LOCAL_ONLY_PROVIDERS,
    PROVIDER_CREDENTIAL_ENV,
    PROVIDER_IDS,
    load_settings,
    model_env_var,
)
from seminars.models import FEED_IDS
from seminars.models.source import SOURCE_GROUP_PREFIXES
from seminars.sources.base import GENERIC_ADAPTERS

ROOT: Final = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR: Final = ROOT / ".github" / "workflows"
WORKFLOW_NAMES: Final = ("ci.yml", "update.yml", "digest.yml")

SETUP_UV: Final = "astral-sh/setup-uv"
MAJOR_PINS: Final = {
    "actions/checkout": "v7",
    "actions/setup-node": "v7",
    "actions/configure-pages": "v6",
    "actions/upload-pages-artifact": "v5",
    "actions/deploy-pages": "v5",
}
"""First-party actions and the major version tag each one is pinned to."""

RUNNER_IMAGE: Final = "ubuntu-24.04"
FORBIDDEN_SECRET_NAMES: Final = ("CODEX_AUTH_JSON", "SECRETS_WRITER_PAT")
LLM_SECRET_NAMES: Final = sorted(name for name in PROVIDER_CREDENTIAL_ENV.values() if name)
LLM_VARIABLE_NAMES: Final = sorted(
    {"LLM_PROVIDERS", "LLM_BASE_URL"}
    | {model_env_var(p) for p in PROVIDER_IDS if p not in LOCAL_ONLY_PROVIDERS}
)

EXPECTED_JOB_PERMISSIONS: Final[dict[tuple[str, str], dict[str, str] | None]] = {
    ("ci.yml", "checks"): None,
    ("ci.yml", "browser"): None,
    ("update.yml", "update"): {"contents": "write", "issues": "write"},
    ("update.yml", "deploy"): {"pages": "write", "id-token": "write"},
    ("update.yml", "llm-smoke"): {"contents": "read"},
    ("digest.yml", "digest"): {"contents": "read", "issues": "write"},
}
"""Every job of every workflow and the token permissions it raises (None: inherits read)."""

LLM_SECRET_STEPS: Final = {("update.yml", "update", "run"), ("update.yml", "llm-smoke", "smoke")}
TOKEN_STEPS: Final = {("update.yml", "update", "run"), ("digest.yml", "digest", "digest")}
PUSHING_CHECKOUTS: Final = {("update.yml", "update")}

EXPRESSION: Final = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
CONTEXT_REFERENCE: Final = re.compile(
    r"\b(?:github|inputs|secrets|vars|steps|needs|env|matrix|runner|job|jobs|strategy)"
    r"(?:\.[A-Za-z0-9_-]+)+"
)
ALLOWED_REFERENCE: Final = re.compile(
    r"github\.(?:ref|event_name|token)"
    r"|inputs\.[a-z_]+|secrets\.[A-Z0-9_]+|vars\.[A-Z0-9_]+|env\.[A-Z0-9_]+"
    r"|steps\.[a-z-]+\.outputs\.[a-z_]+|needs\.[a-z-]+\.outputs\.[a-z_]+"
)
"""Context values the workflows may use. Nothing an outsider controls (github.event.*,
branch names, titles) is on the list."""


# --- loading -------------------------------------------------------------------------------


def workflow_text(name: str) -> str:
    return (WORKFLOWS_DIR / name).read_text(encoding="utf-8")


def load_workflow(name: str) -> dict[str, Any]:
    data: dict[Any, Any] = yaml.safe_load(workflow_text(name))
    # PyYAML follows YAML 1.1, where the bare key "on" is the boolean true.
    if True in data:
        data = {("on" if key is True else key): value for key, value in data.items()}
    return data


def jobs_of(name: str) -> dict[str, dict[str, Any]]:
    return load_workflow(name)["jobs"]


def steps_of(name: str, job_id: str) -> list[dict[str, Any]]:
    return jobs_of(name)[job_id]["steps"]


def step(name: str, job_id: str, step_id: str) -> dict[str, Any]:
    matches = [s for s in steps_of(name, job_id) if s.get("id") == step_id]
    assert len(matches) == 1, f"{name} {job_id}: expected one step with id {step_id!r}"
    return matches[0]


def all_steps() -> Iterator[tuple[str, str, dict[str, Any]]]:
    for name in WORKFLOW_NAMES:
        for job_id, job in jobs_of(name).items():
            for entry in job["steps"]:
                yield name, job_id, entry


def run_scripts() -> Iterator[tuple[str, str, str]]:
    for name, job_id, entry in all_steps():
        if "run" in entry:
            yield name, job_id, entry["run"]


def triggers(name: str) -> dict[str, Any]:
    on = load_workflow(name)["on"]
    return dict.fromkeys(on) if isinstance(on, list) else dict(on)


def uses_of(name: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for job in jobs_of(name).values():
        for entry in job["steps"]:
            if "uses" in entry:
                action, _, ref = str(entry["uses"]).partition("@")
                found.append((action, ref))
    return found


def walk_strings(node: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[tuple[Any, ...], str]]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from walk_strings(value, (*path, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk_strings(value, (*path, index))
    elif isinstance(node, str):
        yield path, node


def conditions(name: str) -> Iterator[str]:
    for job in jobs_of(name).values():
        if "if" in job:
            yield str(job["if"])
        for entry in job["steps"]:
            if "if" in entry:
                yield str(entry["if"])


def header_comment(name: str) -> str:
    lines: list[str] = []
    for line in workflow_text(name).splitlines():
        if not line.startswith("#"):
            break
        lines.append(line)
    return "\n".join(lines)


def cron_fields(entry: Mapping[str, str]) -> list[str]:
    fields = entry["cron"].split()
    assert len(fields) == 5, entry
    return fields


# --- triggers ------------------------------------------------------------------------------


def test_the_three_workflows_exist_and_are_the_only_ones() -> None:
    present = sorted(path.name for path in WORKFLOWS_DIR.iterdir())
    assert present == sorted(WORKFLOW_NAMES)


def test_ci_runs_on_pushes_to_main_and_on_pull_requests() -> None:
    on = triggers("ci.yml")
    assert set(on) == {"push", "pull_request", "workflow_dispatch"}
    assert on["push"] == {"branches": ["main"]}


@pytest.mark.parametrize("name", ["update.yml", "digest.yml"])
def test_privileged_workflows_run_only_on_schedule_and_by_hand(name: str) -> None:
    assert set(triggers(name)) == {"schedule", "workflow_dispatch"}


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_a_workflow_that_sees_a_credential_has_no_pull_request_trigger(name: str) -> None:
    text = workflow_text(name)
    on = triggers(name)
    assert "pull_request_target" not in on
    assert "workflow_run" not in on
    if "secrets." in text or "github.token" in text:
        assert not [key for key in on if key.startswith("pull_request")], name


def test_only_the_scheduled_workflows_touch_credentials() -> None:
    assert "secrets" not in workflow_text("ci.yml")
    assert "github.token" not in workflow_text("ci.yml")
    assert "secrets." not in workflow_text("digest.yml")


# --- schedules -----------------------------------------------------------------------------


def schedule_of(name: str) -> list[dict[str, str]]:
    return triggers(name)["schedule"]


@pytest.mark.parametrize("name", ["update.yml", "digest.yml"])
def test_schedules_use_the_project_time_zone(name: str) -> None:
    zone = load_settings(ROOT / "config" / "settings.yaml").timezone
    assert zone == "America/Los_Angeles"
    for entry in schedule_of(name):
        assert set(entry) == {"cron", "timezone"}
        assert entry["timezone"] == zone
        ZoneInfo(entry["timezone"])


@pytest.mark.parametrize("name", ["update.yml", "digest.yml"])
def test_no_schedule_fires_at_the_top_of_the_hour(name: str) -> None:
    for entry in schedule_of(name):
        minute = cron_fields(entry)[0]
        assert minute.isdigit(), entry
        assert int(minute) != 0, entry


def test_daily_update_runs_at_0423_and_catches_up_six_hours_later() -> None:
    entries = [cron_fields(entry) for entry in schedule_of("update.yml")]
    assert entries == [["23", "4", "*", "*", "*"], ["23", "10", "*", "*", "*"]]


def test_digest_runs_monday_morning_and_catches_up_on_tuesday() -> None:
    entries = [cron_fields(entry) for entry in schedule_of("digest.yml")]
    assert entries == [["7", "7", "*", "*", "1"], ["7", "7", "*", "*", "2"]]


@pytest.mark.parametrize(
    ("name", "fallbacks"),
    [
        ("update.yml", ["'23 12 * * *'", "'23 18 * * *'"]),
        ("digest.yml", ["'7 15 * * 1'", "'7 15 * * 2'"]),
    ],
)
def test_the_utc_fallback_of_each_schedule_is_written_down(name: str, fallbacks: list[str]) -> None:
    text = workflow_text(name)
    for fallback in fallbacks:
        assert fallback in text


# --- permissions ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_top_level_permissions_are_read_only(name: str) -> None:
    assert load_workflow(name)["permissions"] == {"contents": "read"}


def test_every_job_raises_exactly_the_permissions_it_needs() -> None:
    found = {
        (name, job_id): job.get("permissions")
        for name in WORKFLOW_NAMES
        for job_id, job in jobs_of(name).items()
    }
    assert found == EXPECTED_JOB_PERMISSIONS


def test_no_permission_shorthand_is_used() -> None:
    for name in WORKFLOW_NAMES:
        assert "read-all" not in workflow_text(name)
        assert "write-all" not in workflow_text(name)


def checkouts() -> Iterator[tuple[str, str, dict[str, Any]]]:
    for name, job_id, entry in all_steps():
        if str(entry.get("uses", "")).startswith("actions/checkout@"):
            yield name, job_id, entry


def test_every_job_that_checks_out_does_it_once() -> None:
    found = Counter((name, job_id) for name, job_id, _ in checkouts())
    expected = {key for key in EXPECTED_JOB_PERMISSIONS if key != ("update.yml", "deploy")}
    assert set(found) == expected
    assert set(found.values()) == {1}


def test_only_the_pushing_job_keeps_the_token_in_its_checkout() -> None:
    for name, job_id, entry in checkouts():
        keep = entry.get("with", {}).get("persist-credentials")
        assert keep is ((name, job_id) in PUSHING_CHECKOUTS), (name, job_id)


def test_scheduled_workflows_check_out_the_tip_of_main() -> None:
    # The default ref of a scheduled or manual run is the commit the run was created
    # for. A run that waited in the concurrency group must instead start from the data
    # commit of the run before it.
    for name, job_id, entry in checkouts():
        ref = entry.get("with", {}).get("ref")
        assert ref == (None if name == "ci.yml" else "main"), (name, job_id)
        assert set(entry["with"]) <= {"ref", "persist-credentials"}, (name, job_id)


# --- secrets and expressions ---------------------------------------------------------------


def secret_locations() -> Iterator[tuple[str, tuple[Any, ...], str]]:
    for name in WORKFLOW_NAMES:
        for path, value in walk_strings(load_workflow(name)):
            if re.search(r"\bsecrets\b|github\.token", value):
                yield name, path, value


def test_secrets_enter_only_through_the_env_of_a_step() -> None:
    for name, path, value in secret_locations():
        if len(path) == 6 and path[0] == "jobs" and path[2] == "steps" and path[4] == "env":
            key = path[5]
            source = "github.token" if key == "GITHUB_TOKEN" else f"secrets.{key}"
            assert value == "${{ " + source + " }}", (name, path)
        else:
            # The one exception: a job-level flag that only says whether a secret is set,
            # because an "if:" cannot read the secrets context.
            assert len(path) == 4, (name, path)
            assert path[0] == "jobs", (name, path)
            assert path[2] == "env", (name, path)
            assert str(path[3]).startswith("HAS_"), (name, path)
            assert re.fullmatch(r"\$\{\{ secrets\.[A-Z0-9_]+ != '' \}\}", value), (name, path)


def steps_with_env_value(pattern: str) -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for name, job_id, entry in all_steps():
        if any(re.search(pattern, str(v)) for v in entry.get("env", {}).values()):
            found.add((name, job_id, str(entry.get("id"))))
    return found


@pytest.mark.parametrize("secret", LLM_SECRET_NAMES)
def test_each_llm_credential_reaches_only_the_run_and_smoke_steps(secret: str) -> None:
    assert steps_with_env_value(rf"secrets\.{secret}\b") == LLM_SECRET_STEPS


def test_the_token_reaches_only_the_steps_that_open_issues() -> None:
    assert steps_with_env_value(r"github\.token") == TOKEN_STEPS


@pytest.mark.parametrize(("job_id", "step_id"), [("update", "run"), ("llm-smoke", "smoke")])
def test_llm_steps_get_every_credential_and_variable_of_the_contract(
    job_id: str, step_id: str
) -> None:
    env = step("update.yml", job_id, step_id)["env"]
    for secret in LLM_SECRET_NAMES:
        assert env[secret] == "${{ secrets." + secret + " }}"
    for variable in LLM_VARIABLE_NAMES:
        assert env[variable] == "${{ vars." + variable + " }}"
    named = {key for key in env if key.startswith("LLM_") or key in LLM_SECRET_NAMES}
    assert named == set(LLM_SECRET_NAMES) | set(LLM_VARIABLE_NAMES)


def test_the_claude_flag_is_computed_from_the_claude_secret_only() -> None:
    for job_id in ("update", "llm-smoke"):
        env = jobs_of("update.yml")[job_id]["env"]
        assert env == {"HAS_CLAUDE_TOKEN": "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN != '' }}"}


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_nothing_about_codex_is_in_any_workflow(name: str) -> None:
    assert "codex" not in workflow_text(name).lower()
    for secret in FORBIDDEN_SECRET_NAMES:
        assert secret not in workflow_text(name)


def test_no_run_script_contains_an_expression() -> None:
    for name, job_id, script in run_scripts():
        assert "${{" not in script, (name, job_id, script)


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_expressions_use_only_trusted_context_values(name: str) -> None:
    sources = [match.group(1) for match in EXPRESSION.finditer(workflow_text(name))]
    for text in [*sources, *conditions(name)]:
        for reference in CONTEXT_REFERENCE.findall(text):
            assert ALLOWED_REFERENCE.fullmatch(reference), (name, reference)


# --- actions -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_every_action_is_pinned(name: str) -> None:
    for action, ref in uses_of(name):
        if action == SETUP_UV:
            assert re.fullmatch(r"[0-9a-f]{40}", ref), ref
        else:
            assert MAJOR_PINS.get(action) == ref, (action, ref)


def test_setup_uv_is_pinned_to_one_commit_with_its_release_in_a_comment() -> None:
    pins: set[str] = set()
    for name in WORKFLOW_NAMES:
        for line in workflow_text(name).splitlines():
            if f"{SETUP_UV}@" in line:
                match = re.fullmatch(
                    rf"\s+uses: {SETUP_UV}@([0-9a-f]{{40}}) # v\d+\.\d+\.\d+", line
                )
                assert match, line
                pins.add(line.strip())
    assert len(pins) == 1


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_the_header_says_why_each_action_is_used(name: str) -> None:
    header = header_comment(name)
    for action, _ in uses_of(name):
        assert f"{action} " in header, action


@pytest.mark.parametrize("name", ["update.yml", "digest.yml"])
def test_privileged_workflows_do_not_restore_a_dependency_cache(name: str) -> None:
    for job in jobs_of(name).values():
        for entry in job["steps"]:
            if str(entry.get("uses", "")).startswith(f"{SETUP_UV}@"):
                assert entry["with"] == {"enable-cache": False}


# --- jobs, steps and commands --------------------------------------------------------------


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_common_settings(name: str) -> None:
    workflow = load_workflow(name)
    assert workflow["env"] == {"PYTHONUTF8": "1"}
    assert workflow["defaults"] == {"run": {"shell": "bash"}}
    for job_id, job in workflow["jobs"].items():
        assert job["runs-on"] == RUNNER_IMAGE, job_id
        assert isinstance(job["timeout-minutes"], int), job_id
        assert "concurrency" not in job, job_id


@pytest.mark.parametrize(
    ("name", "group"), [("update.yml", "daily-update"), ("digest.yml", "weekly-digest")]
)
def test_privileged_runs_share_one_group_and_are_never_cancelled(name: str, group: str) -> None:
    assert load_workflow(name)["concurrency"] == {"group": group, "cancel-in-progress": False}


def test_ci_cancels_only_superseded_pull_request_runs() -> None:
    assert load_workflow("ci.yml")["concurrency"] == {
        "group": "ci-${{ github.ref }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }


def test_every_uv_call_keeps_the_lock_file() -> None:
    for name, job_id, script in run_scripts():
        for line in script.splitlines():
            if re.search(r"\buv (sync|run)\b", line):
                assert "--locked" in line, (name, job_id, line)


def test_no_step_runs_regardless_of_failures() -> None:
    for name, job_id, entry in all_steps():
        assert "continue-on-error" not in entry, (name, job_id)
    for name in WORKFLOW_NAMES:
        for job_id, job in jobs_of(name).items():
            assert "continue-on-error" not in job, (name, job_id)
        for condition in conditions(name):
            assert not re.search(r"always\(|failure\(|cancelled\(", condition), condition


def commands_of(name: str, job_id: str) -> list[str]:
    return [str(entry["run"]).strip() for entry in steps_of(name, job_id) if "run" in entry]


def test_ci_runs_every_check_of_the_project() -> None:
    assert commands_of("ci.yml", "checks") == [
        "uv sync --locked",
        "uv run --locked python scripts/check_ascii.py",
        "uv run --locked ruff check",
        "uv run --locked ruff format --check",
        "uv run --locked pyright",
        "uv run --locked pytest --cov",
        "uvx --from actionlint-py==1.7.12.25 actionlint",
    ]


def test_ci_runs_the_browser_tests_in_their_own_job() -> None:
    assert commands_of("ci.yml", "browser") == [
        "uv sync --locked --extra browser",
        "uv run --locked --extra browser playwright install --with-deps chromium",
        "uv run --locked --extra browser pytest -m browser",
    ]


def test_dispatch_offers_the_full_and_the_llm_smoke_mode() -> None:
    mode = triggers("update.yml")["workflow_dispatch"]["inputs"]["mode"]
    assert mode["type"] == "choice"
    assert mode["options"] == ["full", "llm-smoke"]
    assert mode["default"] == "full"


def test_jobs_run_on_main_only_and_by_mode() -> None:
    on_main = "github.ref == 'refs/heads/main'"
    jobs = jobs_of("update.yml")
    assert jobs["update"]["if"] == (
        f"{on_main} && (github.event_name == 'schedule' || inputs.mode == 'full')"
    )
    assert jobs["llm-smoke"]["if"] == (
        f"{on_main} && github.event_name == 'workflow_dispatch' && inputs.mode == 'llm-smoke'"
    )
    assert jobs_of("digest.yml")["digest"]["if"] == on_main


def test_the_smoke_job_only_probes_the_providers() -> None:
    assert step("update.yml", "llm-smoke", "smoke")["run"].strip() == (
        "uv run --locked seminars llm-smoke"
    )
    commands = " ".join(commands_of("update.yml", "llm-smoke"))
    assert "seminars run" not in commands
    assert "git " not in commands


def test_update_steps_after_the_run_wait_for_a_real_run() -> None:
    entries = steps_of("update.yml", "update")
    index = next(i for i, entry in enumerate(entries) if entry.get("id") == "run")
    later = entries[index + 1 :]
    assert [entry.get("id") for entry in later] == ["validate", "commit", "build", "upload"]
    for entry in later:
        assert entry["if"] == "steps.run.outputs.ran == 'true'"
    assert step("update.yml", "update", "validate")["run"].strip() == (
        "uv run --locked seminars validate"
    )
    assert step("update.yml", "update", "build")["run"].strip() == (
        "uv run --locked seminars build-site"
    )
    upload = step("update.yml", "update", "upload")
    assert upload["uses"] == "actions/upload-pages-artifact@v5"
    assert upload["with"] == {"path": "public"}


def test_pages_are_deployed_by_their_own_job_after_a_real_run() -> None:
    jobs = jobs_of("update.yml")
    assert jobs["update"]["outputs"] == {"ran": "${{ steps.run.outputs.ran }}"}
    deploy = jobs["deploy"]
    assert deploy["needs"] == "update"
    assert deploy["if"] == "needs.update.outputs.ran == 'true'"
    assert deploy["environment"] == {
        "name": "github-pages",
        "url": "${{ steps.deployment.outputs.page_url }}",
    }
    assert [entry["uses"] for entry in deploy["steps"]] == [
        "actions/configure-pages@v6",
        "actions/deploy-pages@v5",
    ]
    assert deploy["steps"][-1]["id"] == "deployment"


@pytest.mark.parametrize("job_id", ["update", "llm-smoke"])
def test_claude_code_is_installed_only_when_its_token_is_set(job_id: str) -> None:
    flagged = "env.HAS_CLAUDE_TOKEN == 'true'"
    node = [e for e in steps_of("update.yml", job_id) if "setup-node" in str(e.get("uses"))]
    assert len(node) == 1
    assert node[0]["if"] == flagged
    assert node[0]["with"] == {"node-version": 24}
    assert step("update.yml", job_id, "claude")["if"] == flagged


def test_both_jobs_install_claude_code_the_same_way() -> None:
    first = step("update.yml", "update", "claude")
    second = step("update.yml", "llm-smoke", "claude")
    assert first == second


# --- the scripts, run with bash --------------------------------------------------------------

needs_shell = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="needs bash and git",
)

TEST_IDENTITY: Final = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

STUB_UV: Final = """#!/bin/sh
printf 'uv %s\\n' "$*" >> "$STUB_LOG"
if [ "$1 $2 $3" = "run --locked python" ]; then
  shift 3
  exec "$STUB_PYTHON" "$@"
fi
if [ -n "${STUB_UV_TOUCH:-}" ]; then
  mkdir -p "$(dirname "$STUB_UV_TOUCH")"
  printf '{"changed": true}\\n' > "$STUB_UV_TOUCH"
fi
exit "${STUB_UV_STATUS:-0}"
"""

STUB_LOGGER: Final = """#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$STUB_LOG"
"""

STUB_DAY: Final = "2026-11-02"
STUB_DATE: Final = f"""#!/bin/sh
if [ "${{TZ:-}}" = "America/Los_Angeles" ] && [ "$*" = "+%F" ]; then
  echo {STUB_DAY}
  exit 0
fi
echo "unexpected call: TZ=${{TZ:-}} date $*" >&2
exit 1
"""
"""A clock for the scripts: answers only the Pacific calendar day, and fails otherwise."""


@dataclass(frozen=True)
class ScriptResult:
    status: int
    stdout: str
    outputs: dict[str, str]
    calls: list[str]


class Sandbox:
    """A directory with stub commands and an environment cut off from the user's git setup."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "calls.log"
        self.log.touch()
        self.stub("uv", STUB_UV)
        self.stub("npm", STUB_LOGGER)
        self.stub("claude", STUB_LOGGER)
        self.stub("date", STUB_DATE)

    def stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def env(self, extra: Mapping[str, str] = {}) -> dict[str, str]:
        return {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.root),
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "STUB_LOG": str(self.log),
            "STUB_PYTHON": sys.executable,
            **extra,
        }

    def git(self, cwd: Path, *args: str) -> str:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self.env(TEST_IDENTITY),
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def repo(self, files: Mapping[str, str]) -> tuple[Path, Path]:
        """A bare repository holding ``files`` on main, and a clone of it; returns both."""
        remote, seed = self.root / "remote.git", self.root / "seed"
        self.git(self.root, "init", "--quiet", "--bare", "--initial-branch=main", str(remote))
        self.git(self.root, "init", "--quiet", "--initial-branch=main", str(seed))
        for relative, text in files.items():
            write(seed / relative, text)
        self.git(seed, "add", "--all")
        self.git(seed, "commit", "--quiet", "--message", "seed")
        self.git(seed, "push", "--quiet", str(remote), "main")
        return remote, self.clone(remote, "work")

    def clone(self, remote: Path, name: str) -> Path:
        self.git(self.root, "clone", "--quiet", str(remote), name)
        return self.root / name

    def run_step(
        self, workflow: str, job_id: str, step_id: str, cwd: Path, extra: Mapping[str, str] = {}
    ) -> ScriptResult:
        entry = step(workflow, job_id, step_id)
        literal = {k: str(v) for k, v in entry.get("env", {}).items() if "${{" not in str(v)}
        outputs_file = self.root / "github_output"
        outputs_file.write_text("", encoding="utf-8")
        script = self.root / "step.sh"
        script.write_text(entry["run"], encoding="utf-8")
        self.log.write_text("", encoding="utf-8")
        env = self.env({**literal, "GITHUB_OUTPUT": str(outputs_file), **extra})
        # The command GitHub runs for "shell: bash".
        done = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        lines = outputs_file.read_text(encoding="utf-8").splitlines()
        return ScriptResult(
            status=done.returncode,
            stdout=done.stdout + done.stderr,
            outputs={key: value for key, _, value in (line.partition("=") for line in lines)},
            calls=self.log.read_text(encoding="utf-8").splitlines(),
        )


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


BASE_FILES: Final = {"data/last_run.json": "{}\n", "data/events.json": "[]\n", "README.md": "x\n"}


@needs_shell
@pytest.mark.parametrize("status", [1, 3])
def test_a_failed_run_stops_the_job_before_anything_is_committed(
    sandbox: Sandbox, status: int
) -> None:
    _, work = sandbox.repo(BASE_FILES)
    result = sandbox.run_step(
        "update.yml",
        "update",
        "run",
        work,
        {"GITHUB_EVENT_NAME": "schedule", "STUB_UV_STATUS": str(status)},
    )
    assert result.status == status
    assert "ran" not in result.outputs
    assert "::error" in result.stdout
    if status == 3:
        assert "Output check failed" in result.stdout


@needs_shell
def test_a_scheduled_run_may_skip_and_then_nothing_is_published(sandbox: Sandbox) -> None:
    _, work = sandbox.repo(BASE_FILES)
    scheduled = {"GITHUB_EVENT_NAME": "schedule"}
    result = sandbox.run_step("update.yml", "update", "run", work, scheduled)
    assert result.status == 0
    assert result.calls == ["uv run --locked seminars run --skip-if-done-today"]
    assert result.outputs == {"ran": "false"}


@needs_shell
@pytest.mark.parametrize(
    ("event", "command"),
    [
        ("schedule", "uv run --locked seminars run --skip-if-done-today"),
        ("workflow_dispatch", "uv run --locked seminars run"),
    ],
)
def test_a_run_that_wrote_its_record_is_published(
    sandbox: Sandbox, event: str, command: str
) -> None:
    _, work = sandbox.repo(BASE_FILES)
    touch = str(work / "data" / "last_run.json")
    result = sandbox.run_step(
        "update.yml", "update", "run", work, {"GITHUB_EVENT_NAME": event, "STUB_UV_TOUCH": touch}
    )
    assert result.status == 0
    assert result.calls == [command]
    assert result.outputs == {"ran": "true"}


@needs_shell
def test_the_first_record_of_a_new_repository_counts_as_a_run(sandbox: Sandbox) -> None:
    _, work = sandbox.repo({"README.md": "x\n"})
    touch = str(work / "data" / "last_run.json")
    extra = {"GITHUB_EVENT_NAME": "schedule", "STUB_UV_TOUCH": touch}
    result = sandbox.run_step("update.yml", "update", "run", work, extra)
    assert result.outputs == {"ran": "true"}


@needs_shell
def test_the_commit_holds_only_data_and_is_pushed_by_the_bot(sandbox: Sandbox) -> None:
    remote, work = sandbox.repo(BASE_FILES)
    write(work / "data" / "events.json", "[1]\n")
    write(work / "data" / "archive" / "2026.jsonl", "{}\n")
    (work / "data" / "last_run.json").unlink()
    write(work / "README.md", "changed by a bug\n")
    write(work / "stray.txt", "not data\n")
    result = sandbox.run_step("update.yml", "update", "commit", work)
    assert result.status == 0, result.stdout
    head = sandbox.git(remote, "log", "-1", "--format=%an <%ae>|%cn|%s", "main")
    author, committer, subject = head.split("|")
    assert author == "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>"
    assert committer == "github-actions[bot]"
    assert subject == f"chore(data): daily update {STUB_DAY}"
    changed = sandbox.git(
        remote, "show", "--no-renames", "--name-status", "--format=", "main"
    ).splitlines()
    assert sorted(changed) == [
        "A\tdata/archive/2026.jsonl",
        "D\tdata/last_run.json",
        "M\tdata/events.json",
    ]


@needs_shell
def test_nothing_is_committed_when_data_is_unchanged(sandbox: Sandbox) -> None:
    remote, work = sandbox.repo(BASE_FILES)
    before = sandbox.git(remote, "rev-parse", "main")
    write(work / "README.md", "changed outside data\n")
    result = sandbox.run_step("update.yml", "update", "commit", work)
    assert result.status == 0
    assert sandbox.git(remote, "rev-parse", "main") == before
    assert "nothing to commit" in result.stdout


@needs_shell
def test_the_data_commit_is_rebased_when_main_moved_during_the_run(sandbox: Sandbox) -> None:
    remote, work = sandbox.repo(BASE_FILES)
    other = sandbox.clone(remote, "other")
    write(other / "README.md", "pushed meanwhile\n")
    sandbox.git(other, "commit", "--quiet", "--all", "--message", "docs: meanwhile")
    sandbox.git(other, "push", "--quiet", "origin", "main")
    write(work / "data" / "events.json", "[1]\n")
    result = sandbox.run_step("update.yml", "update", "commit", work)
    assert result.status == 0, result.stdout
    subjects = sandbox.git(remote, "log", "--format=%s", "main").splitlines()
    assert subjects == [f"chore(data): daily update {STUB_DAY}", "docs: meanwhile", "seed"]
    assert "::warning::" in result.stdout


@needs_shell
def test_a_data_commit_that_conflicts_with_main_fails_cleanly(sandbox: Sandbox) -> None:
    remote, work = sandbox.repo(BASE_FILES)
    other = sandbox.clone(remote, "other")
    write(other / "data" / "last_run.json", '{"by": "other"}\n')
    sandbox.git(other, "commit", "--quiet", "--all", "--message", "chore(data): other")
    sandbox.git(other, "push", "--quiet", "origin", "main")
    pushed = sandbox.git(remote, "rev-parse", "main")
    write(work / "data" / "last_run.json", '{"by": "work"}\n')
    result = sandbox.run_step("update.yml", "update", "commit", work)
    assert result.status == 1, result.stdout
    assert "::error::The data commit could not be rebased onto main" in result.stdout
    assert sandbox.git(remote, "rev-parse", "main") == pushed
    # The rebase was undone: the clone holds its own data commit and nothing half-merged.
    assert "rebase" not in sandbox.git(work, "status")
    assert sandbox.git(work, "log", "-1", "--format=%s") == f"chore(data): daily update {STUB_DAY}"
    assert sandbox.git(work, "status", "--porcelain") == ""


def claude_config(work: Path, version: str | None) -> None:
    write(work / "config" / "llm.yaml", f"cli_versions:\n  claude-cli: {json.dumps(version)}\n")


@needs_shell
@pytest.mark.parametrize("job_id", ["update", "llm-smoke"])
def test_claude_code_is_installed_at_the_recorded_version(sandbox: Sandbox, job_id: str) -> None:
    work = sandbox.root / "work"
    claude_config(work, "2.1.322")
    result = sandbox.run_step("update.yml", job_id, "claude", work)
    assert result.status == 0, result.stdout
    assert result.calls[1:] == [
        "npm install --global --no-audit --no-fund @anthropic-ai/claude-code@2.1.322",
        "claude --version",
    ]


@needs_shell
@pytest.mark.parametrize("version", [None, "", "latest", "2.1", "1.2.3; touch pwned", "^2.1.0"])
def test_claude_code_is_skipped_without_a_plain_recorded_version(
    sandbox: Sandbox, version: str | None
) -> None:
    work = sandbox.root / "work"
    claude_config(work, version)
    result = sandbox.run_step("update.yml", "update", "claude", work)
    assert result.status == 0, result.stdout
    assert [call for call in result.calls if not call.startswith("uv ")] == []
    assert "::warning" in result.stdout
    assert not (work / "pwned").exists()


INSTALL_CALL: Final = "npm install --global --no-audit --no-fund @anthropic-ai/claude-code@2.1.322"


@needs_shell
@pytest.mark.parametrize("job_id", ["update", "llm-smoke"])
@pytest.mark.parametrize(
    ("broken", "calls"),
    [("npm", [INSTALL_CALL]), ("claude", [INSTALL_CALL, "claude --version"])],
)
def test_a_failed_claude_code_install_only_warns(
    sandbox: Sandbox, job_id: str, broken: str, calls: list[str]
) -> None:
    # A version missing on npm, or a registry outage, must not cost the day's update:
    # the LLM is optional and the provider chain copes without the claude binary.
    work = sandbox.root / "work"
    claude_config(work, "2.1.322")
    sandbox.stub(broken, STUB_LOGGER + "exit 1\n")
    result = sandbox.run_step("update.yml", job_id, "claude", work)
    assert result.status == 0, result.stdout
    assert result.calls[1:] == calls
    assert "::warning::Claude Code 2.1.322 could not be installed" in result.stdout


@needs_shell
@pytest.mark.parametrize("job_id", ["update", "llm-smoke"])
def test_an_unreadable_llm_config_only_warns_at_the_install(sandbox: Sandbox, job_id: str) -> None:
    work = sandbox.root / "work"
    write(work / "config" / "llm.yaml", "cli_versions: [unclosed\n")
    result = sandbox.run_step("update.yml", job_id, "claude", work)
    assert result.status == 0, result.stdout
    assert [call for call in result.calls if not call.startswith("uv ")] == []
    assert "::warning::The claude-cli version could not be read" in result.stdout


@needs_shell
@pytest.mark.parametrize(
    ("dry_run", "command"),
    [
        ("true", "uv run --locked seminars digest --dry-run"),
        ("false", "uv run --locked seminars digest"),
        ("", "uv run --locked seminars digest"),
    ],
)
def test_the_digest_step_passes_dry_run_through(
    sandbox: Sandbox, dry_run: str, command: str
) -> None:
    result = sandbox.run_step("digest.yml", "digest", "digest", sandbox.root, {"DRY_RUN": dry_run})
    assert result.status == 0
    assert result.calls == [command]


def test_the_digest_step_gets_the_dispatch_input_through_env() -> None:
    assert step("digest.yml", "digest", "digest")["env"] == {
        "GITHUB_TOKEN": "${{ github.token }}",
        "DRY_RUN": "${{ inputs.dry_run }}",
    }
    dry_run = triggers("digest.yml")["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry_run["type"] == "boolean"
    assert dry_run["default"] is False


# --- documentation -------------------------------------------------------------------------

DOC_PAIRS: Final = (
    ("README.md", "README.zh-CN.md"),
    ("docs/ADDING_A_SOURCE.md", "docs/ADDING_A_SOURCE.zh-CN.md"),
)
OTHER_NODES_DOCS: Final = {"docs/LLM_CREDENTIALS.md", "docs/LLM_CREDENTIALS.zh-CN.md"}
"""Documents written elsewhere in the project that the README links to."""

PLACEHOLDER_EN: Final = "(filled in at release)"
PLACEHOLDER_ZH: Final = "\uff08\u53d1\u5e03\u65f6\u586b\u5199\uff09"
"""The Chinese for "(filled in at release)", in full-width parentheses."""
ZH_LABEL: Final = "\u4e2d\u6587"
LINK: Final = re.compile(r"\]\(([^)\s]+)\)")
FENCE: Final = re.compile(r"^```[^\n]*\n.*?^```$", re.DOTALL | re.MULTILINE)


def doc(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def headings(text: str) -> list[int]:
    prose = FENCE.sub("", text)
    return [len(m.group(1)) for m in re.finditer(r"^(#+) ", prose, re.MULTILINE)]


def links(text: str) -> Counter[str]:
    return Counter(target.replace(".zh-CN.md", ".md") for target in LINK.findall(text))


def first_line(text: str) -> str:
    return next(line for line in text.splitlines() if line.strip())


@pytest.mark.parametrize(("english", "chinese"), DOC_PAIRS)
def test_each_document_links_to_its_translation_on_top(english: str, chinese: str) -> None:
    english_name, chinese_name = Path(english).name, Path(chinese).name
    # The English file is ASCII only, so it writes the Chinese label as character references.
    assert first_line(doc(english)) == f"English | [&#20013;&#25991;]({chinese_name})"
    assert first_line(doc(chinese)) == f"[English]({english_name}) | {ZH_LABEL}"


@pytest.mark.parametrize(("english", "chinese"), DOC_PAIRS)
def test_translations_have_the_same_structure(english: str, chinese: str) -> None:
    assert headings(doc(english)) == headings(doc(chinese))
    assert FENCE.findall(doc(english)) == FENCE.findall(doc(chinese))
    assert links(doc(english)) == links(doc(chinese))


@pytest.mark.parametrize("path", [path for pair in DOC_PAIRS for path in pair])
def test_relative_links_point_to_files_of_the_repository(path: str) -> None:
    base = (ROOT / path).parent
    for target in LINK.findall(doc(path)):
        if re.match(r"[a-z]+:", target) or target.startswith("#"):
            continue
        resolved = (base / target.split("#")[0]).resolve()
        relative = resolved.relative_to(ROOT).as_posix()
        assert resolved.exists() or relative in OTHER_NODES_DOCS, (path, target)


def test_numbers_known_only_at_release_are_marked_in_both_readmes() -> None:
    count = doc("README.md").count(PLACEHOLDER_EN)
    assert count > 0
    assert doc("README.zh-CN.md").count(PLACEHOLDER_ZH) == count


@pytest.mark.parametrize("path", ["README.md", "README.zh-CN.md"])
def test_the_readme_lists_every_feed(path: str) -> None:
    for feed in FEED_IDS:
        assert f"`{feed}`" in doc(path), feed


@pytest.mark.parametrize("path", ["docs/ADDING_A_SOURCE.md", "docs/ADDING_A_SOURCE.zh-CN.md"])
def test_the_contributor_guide_covers_the_adapters_and_groups(path: str) -> None:
    text = doc(path)
    for adapter in GENERIC_ADAPTERS:
        assert f"`{adapter}`" in text, adapter
    for prefix in SOURCE_GROUP_PREFIXES.values():
        assert f"`{prefix}`" in text, prefix
    for needle in ("register_adapter", "seminars.fixtures record", "scripts/check_ascii.py"):
        assert needle in text, needle


@pytest.mark.parametrize("path", [path for pair in DOC_PAIRS for path in pair])
def test_documents_never_name_the_removed_secrets(path: str) -> None:
    for secret in FORBIDDEN_SECRET_NAMES:
        assert secret not in doc(path)


COMMANDS: Final = frozenset(
    {"run", "llm-smoke", "sources", "add-source", "validate", "digest", "build-site", "--help"}
)
"""Subcommands of the command line that the documents may show (seminars.contracts)."""

INLINE_CODE: Final = re.compile(r"`([^`\n]+)`")


def code_of(text: str) -> list[str]:
    fences = FENCE.findall(text)
    return [*fences, *INLINE_CODE.findall(FENCE.sub("", text))]


@pytest.mark.parametrize("path", [path for pair in DOC_PAIRS for path in pair])
def test_documents_show_only_commands_of_the_command_line(path: str) -> None:
    used = {
        match.group(1)
        for code in code_of(doc(path))
        for match in re.finditer(r"\bseminars ([a-z-]+)", code)
    }
    assert used
    assert used <= COMMANDS, used - COMMANDS


@pytest.mark.parametrize(
    ("path", "days"), [("README.md", "60 days"), ("README.zh-CN.md", "60 \u5929")]
)
def test_the_readme_says_what_to_do_when_github_disables_the_schedule(path: str, days: str) -> None:
    text = doc(path)
    assert days in text
    assert "Enable workflow" in text


@pytest.mark.parametrize("path", ["README.md", "README.zh-CN.md"])
def test_the_readme_names_the_credentials_and_variables_of_the_workflows(path: str) -> None:
    text = doc(path)
    for name in [*LLM_SECRET_NAMES, "LLM_PROVIDERS", "LLM_MODEL_<PROVIDER>", "LLM_BASE_URL"]:
        assert f"`{name}`" in text, name
