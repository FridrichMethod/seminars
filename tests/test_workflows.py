"""Checks of the GitHub Actions workflows and of the user documentation.

A workflow only runs on GitHub, after a push, so this file checks
everything about them that can be checked on one machine:

* the security rules, by parsing the YAML: triggers, permissions, where
  secrets and the token go, which job may push and what it runs, actions
  pinned to commits, no expression inside a script;
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

import base64
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
ACTION_PINS: Final = {
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"),
    "actions/setup-node": ("949feb2413d6458794dcd2491c4babbbce0c15c1", "v7.1.0"),
    "actions/upload-artifact": ("cf430e030ddbb5b0abf93d22962f4752f3646cd9", "v7.0.2"),
    "actions/download-artifact": ("9000827ccba6bdab643e8b6fd33ac0654aef8333", "v8.0.2"),
    "actions/configure-pages": ("45bfe0192ca1faeb007ade9deae92b16b8254a0d", "v6.0.0"),
    "actions/upload-pages-artifact": ("fc324d3547104276b827a68afc52ff2a11cc49c9", "v5.0.0"),
    "actions/deploy-pages": ("368f82528645a54fb793d4d04e342629a3f51346", "v5.0.1"),
    SETUP_UV: ("68559e99cc807c1eb7e9238fd43a15bba460eec2", "v10.3.3"),
}
"""Every action, the commit it is pinned to and the release of that commit.

Each SHA was read from the tag of the release with the GitHub API
(``gh api repos/<owner>/<repo>/git/ref/tags/<release>``, following an
annotated tag to its commit). A new release is a change of this table and of
the workflows together.
"""

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
    ("update.yml", "fetch"): {"contents": "read", "issues": "write"},
    ("update.yml", "commit"): {"contents": "write"},
    ("update.yml", "build"): None,
    ("update.yml", "deploy"): {"pages": "write", "id-token": "write"},
    ("update.yml", "outage"): None,
    ("update.yml", "llm-smoke"): {"contents": "read"},
    ("digest.yml", "digest"): {"contents": "read", "issues": "write"},
}
"""Every job of every workflow and the token permissions it raises (None: inherits read)."""

LLM_SECRET_STEPS: Final = {("update.yml", "fetch", "run"), ("update.yml", "llm-smoke", "smoke")}
TOKEN_STEPS: Final = {
    ("update.yml", "fetch", "run"),
    ("update.yml", "commit", "commit"),
    ("digest.yml", "digest", "digest"),
}
WITHOUT_CHECKOUT: Final = {("update.yml", "deploy"), ("update.yml", "outage")}
"""Jobs that need nothing of the repository."""

EXPRESSION: Final = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
CONTEXT_REFERENCE: Final = re.compile(
    r"\b(?:github|inputs|secrets|vars|steps|needs|env|matrix|runner|job|jobs|strategy)"
    r"(?:\.[A-Za-z0-9_-]+)+"
)
ALLOWED_REFERENCE: Final = re.compile(
    r"github\.(?:ref|event_name|token)"
    r"|inputs\.[a-z_]+|secrets\.[A-Z0-9_]+|vars\.[A-Z0-9_]+|env\.[A-Z0-9_]+"
    r"|steps\.[a-z-]+\.outputs\.[a-z_]+|needs\.[a-z-]+\.outputs\.[a-z_]+"
    r"|runner\.temp"
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
    expected = set(EXPECTED_JOB_PERMISSIONS) - WITHOUT_CHECKOUT
    assert set(found) == expected
    assert set(found.values()) == {1}


def test_no_checkout_keeps_the_token() -> None:
    # Otherwise every later step could read it from .git/config. The commit step hands
    # the token to git itself.
    for name, job_id, entry in checkouts():
        assert entry["with"]["persist-credentials"] is False, (name, job_id)


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


@pytest.mark.parametrize(("job_id", "step_id"), [("fetch", "run"), ("llm-smoke", "smoke")])
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
    for job_id in ("fetch", "llm-smoke"):
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
def test_every_action_is_pinned_to_the_commit_of_its_release(name: str) -> None:
    for action, ref in uses_of(name):
        assert action in ACTION_PINS, action
        assert ref == ACTION_PINS[action][0], (action, ref)


USES_LINE: Final = re.compile(r"\s+uses: ([\w.-]+/[\w.-]+)@([0-9a-f]{40}) # (v\d+\.\d+\.\d+)")


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_each_pin_names_its_release_in_a_comment(name: str) -> None:
    lines = [line for line in workflow_text(name).splitlines() if "uses:" in line]
    assert lines
    for line in lines:
        match = USES_LINE.fullmatch(line)
        assert match, line
        action, sha, release = match.groups()
        assert ACTION_PINS[action] == (sha, release), line


def test_no_action_is_left_on_a_tag_or_a_branch() -> None:
    # Before the pins, the actions of GitHub stood on major version tags such as v7.
    for name in WORKFLOW_NAMES:
        assert not re.search(r"uses: [^#\n]*@v\d", workflow_text(name)), name
        assert "@main" not in workflow_text(name), name


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
def test_the_header_says_why_each_action_is_used(name: str) -> None:
    header = header_comment(name)
    for action, _ in uses_of(name):
        assert f"{action} ({ACTION_PINS[action][1]}" in header, action


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
    assert jobs["fetch"]["if"] == (
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


def holds_a_credential(entry: Mapping[str, Any]) -> bool:
    return any(
        re.search(r"\bsecrets\.|github\.token", str(value))
        for value in entry.get("env", {}).values()
    )


SYNC: Final = "uv sync --locked"


def test_the_project_is_built_before_any_step_that_holds_a_credential() -> None:
    # The build of the project and every package install run in a step of their own,
    # without a credential in its environment; a step that holds one only runs.
    checked = 0
    for name in WORKFLOW_NAMES:
        for job_id, job in jobs_of(name).items():
            entries = job["steps"]
            for index, entry in enumerate(entries):
                if not holds_a_credential(entry) or "uv " not in str(entry.get("run", "")):
                    continue
                syncs = [e for e in entries[:index] if str(e.get("run", "")).strip() == SYNC]
                assert syncs, (name, job_id, entry.get("id"))
                assert all(not holds_a_credential(e) for e in syncs), (name, job_id)
                checked += 1
    assert checked == 3  # the pipeline, the smoke step and the digest


def test_the_sync_comes_right_after_uv_is_installed() -> None:
    for name, job_id in [
        ("update.yml", "fetch"),
        ("update.yml", "build"),
        ("update.yml", "llm-smoke"),
        ("digest.yml", "digest"),
    ]:
        entries = steps_of(name, job_id)
        index = next(i for i, e in enumerate(entries) if SETUP_UV in str(e.get("uses", "")))
        assert entries[index + 1] == {
            "name": "Install the locked dependencies",
            "id": "sync",
            "run": SYNC,
        }, (name, job_id)


def test_the_job_that_can_push_runs_nothing_but_git() -> None:
    commit = jobs_of("update.yml")["commit"]
    actions = [str(entry["uses"]).partition("@")[0] for entry in commit["steps"] if "uses" in entry]
    assert actions == ["actions/checkout", "actions/download-artifact"]
    (script,) = commands_of("update.yml", "commit")
    for word in ("uv", "uvx", "npm", "node", "python", "pip", "seminars", "make", "sudo"):
        assert not re.search(rf"(^|[\s;|&(]){word}\b", script, re.MULTILINE), word
    assert "needs.fetch.outputs" not in str(commit["steps"][0])


def test_only_the_commit_job_may_write_the_repository() -> None:
    writers = {
        key
        for key, permissions in EXPECTED_JOB_PERMISSIONS.items()
        if permissions and permissions.get("contents") == "write"
    }
    assert writers == {("update.yml", "commit")}


def test_the_fetch_job_runs_validates_and_hands_on_its_data() -> None:
    entries = steps_of("update.yml", "fetch")
    index = next(i for i, entry in enumerate(entries) if entry.get("id") == "run")
    later = entries[index + 1 :]
    assert [entry.get("id") for entry in later] == ["validate", "upload"]
    assert later[0]["if"] == (
        "steps.run.outputs.ran == 'true' && steps.run.outputs.outcome == 'ok'"
    )
    assert later[0]["run"].strip() == "uv run --locked seminars validate"
    assert later[1]["if"] == "steps.run.outputs.ran == 'true'"
    assert later[1]["uses"].partition("@")[0] == "actions/upload-artifact"
    assert later[1]["with"] == {
        "name": "run-data",
        "path": "data",
        "if-no-files-found": "error",
        "retention-days": 1,
    }
    assert jobs_of("update.yml")["fetch"]["outputs"] == {
        "ran": "${{ steps.run.outputs.ran }}",
        "outcome": "${{ steps.run.outputs.outcome }}",
        "base": "${{ steps.run.outputs.base }}",
    }


def test_the_commit_job_takes_the_data_of_a_real_run() -> None:
    commit = jobs_of("update.yml")["commit"]
    assert commit["needs"] == "fetch"
    assert commit["if"] == "needs.fetch.outputs.ran == 'true'"
    download = step("update.yml", "commit", "download")
    assert download["uses"].partition("@")[0] == "actions/download-artifact"
    assert download["with"] == {"name": "run-data", "path": "${{ runner.temp }}/run-data"}
    env = step("update.yml", "commit", "commit")["env"]
    assert env["RUN_OUTCOME"] == "${{ needs.fetch.outputs.outcome }}"
    assert env["RUN_BASE"] == "${{ needs.fetch.outputs.base }}"


def test_the_site_is_built_from_the_new_main_and_then_deployed() -> None:
    jobs = jobs_of("update.yml")
    assert jobs["build"]["needs"] == "commit"
    assert "if" not in jobs["build"]
    assert commands_of("update.yml", "build") == [
        SYNC,
        "uv run --locked seminars build-site",
        "uv run --locked seminars validate",
    ]
    upload = step("update.yml", "build", "upload")
    assert upload["uses"].partition("@")[0] == "actions/upload-pages-artifact"
    assert upload["with"] == {"path": "public"}
    deploy = jobs["deploy"]
    assert deploy["needs"] == "build"
    assert "if" not in deploy
    assert deploy["environment"] == {
        "name": "github-pages",
        "url": "${{ steps.deployment.outputs.page_url }}",
    }
    assert [entry["uses"].partition("@")[0] for entry in deploy["steps"]] == [
        "actions/configure-pages",
        "actions/deploy-pages",
    ]
    assert deploy["steps"][-1]["id"] == "deployment"


def test_a_total_outage_fails_the_run_after_it_is_recorded_and_deployed() -> None:
    outage = jobs_of("update.yml")["outage"]
    assert outage["needs"] == ["fetch", "deploy"]
    assert outage["if"] == "needs.fetch.outputs.outcome == 'outage'"
    (script,) = commands_of("update.yml", "outage")
    assert script.startswith('echo "::error::')
    assert script.endswith("exit 1")
    # Exit status 1 after the run recorded itself has two causes; the message names both.
    assert "every source failed" in script
    assert "the feeds could not be written" in script


@pytest.mark.parametrize("job_id", ["fetch", "llm-smoke"])
def test_claude_code_is_installed_only_when_its_token_is_set(job_id: str) -> None:
    flagged = "env.HAS_CLAUDE_TOKEN == 'true'"
    node = [e for e in steps_of("update.yml", job_id) if "setup-node" in str(e.get("uses"))]
    assert len(node) == 1
    assert node[0]["if"] == flagged
    assert node[0]["with"] == {"node-version": 24}
    assert step("update.yml", job_id, "claude")["if"] == flagged


def test_both_jobs_install_claude_code_the_same_way() -> None:
    first = step("update.yml", "fetch", "claude")
    second = step("update.yml", "llm-smoke", "claude")
    assert first == second


def test_npm_runs_no_install_script() -> None:
    installs = [
        line.strip()
        for _, _, script in run_scripts()
        for line in script.splitlines()
        if re.search(r"\bnpm (install|i|ci)\b", line)
    ]
    assert len(installs) == 2
    for line in installs:
        assert "--ignore-scripts" in line, line


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
  printf '%s\\n' "${STUB_UV_RECORD:-changed}" > "$STUB_UV_TOUCH"
fi
exit "${STUB_UV_STATUS:-0}"
"""

STUB_LOGGER: Final = """#!/bin/sh
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$STUB_LOG"
"""

PACKAGE: Final = "lib/node_modules/@anthropic-ai/claude-code"
"""The CLI package of a global install, below the npm prefix."""

NESTED_NATIVE: Final = f"{PACKAGE}/node_modules/@anthropic-ai/claude-code-linux-x64/claude"
"""Where a global install puts the binary of the platform package: npm does not hoist the
optional dependency of a global package, so it stays below the package."""

HOISTED_NATIVE: Final = "lib/node_modules/@anthropic-ai/claude-code-linux-x64/claude"
"""Where the binary is when the platform package is hoisted to the top of the modules."""

PLACEHOLDER: Final = f"{PACKAGE}/bin/claude.exe"
"""The command that the skipped install script of the package would replace."""

STUB_NPM: Final = f"""#!/bin/sh
printf 'npm %s\\n' "$*" >> "$STUB_LOG"
case "$1" in
  root)
    echo "$STUB_NPM_PREFIX/lib/node_modules"
    exit "${{STUB_NPM_ROOT_STATUS:-0}}"
    ;;
  prefix) echo "$STUB_NPM_PREFIX" ;;
  install)
    placeholder="$STUB_NPM_PREFIX/{PLACEHOLDER}"
    mkdir -p "$(dirname "$placeholder")" "$STUB_NPM_PREFIX/bin"
    cp "$STUB_PLACEHOLDER" "$placeholder"
    chmod 755 "$placeholder"
    ln -sf "../{PLACEHOLDER}" "$STUB_NPM_PREFIX/bin/claude"
    case "${{STUB_NPM_LAYOUT:-nested}}" in
      nested) natives="{NESTED_NATIVE}" ;;
      hoisted) natives="{HOISTED_NATIVE}" ;;
      both) natives="{NESTED_NATIVE} {HOISTED_NATIVE}" ;;
      *) natives="" ;;
    esac
    for relative in $natives; do
      native="$STUB_NPM_PREFIX/$relative"
      mkdir -p "$(dirname "$native")"
      cp "$STUB_NATIVE" "$native"
      chmod 755 "$native"
    done
    exit "${{STUB_NPM_STATUS:-0}}"
    ;;
esac
"""
"""npm as far as the install step uses it: a global prefix, and an install that runs no
script. Like the real global install it links ``claude`` to the placeholder of the package
and puts the binary of the platform package below the package (STUB_NPM_LAYOUT=nested, the
default); ``hoisted`` puts it at the top of the global modules instead, ``both`` in both
places, and ``none`` nowhere. STUB_NPM_ROOT_STATUS and STUB_NPM_STATUS make ``npm root`` and
the install fail."""

STUB_NATIVE: Final = """#!/bin/sh
printf 'native %s\\n' "$*" >> "$STUB_LOG"
exit "${STUB_NATIVE_STATUS:-0}"
"""
"""The binary of the platform package, as the stub npm installs it."""

STUB_PLACEHOLDER: Final = """#!/bin/sh
printf 'placeholder %s\\n' "$*" >> "$STUB_LOG"
exit 1
"""
"""The placeholder command of the package, which fails without its install script."""

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
        self.npm_prefix = root / "npm-global"
        self.runner_temp = root / "runner-temp"
        self.runner_temp.mkdir()
        self.log = root / "calls.log"
        self.log.touch()
        self.stub("uv", STUB_UV)
        self.stub("npm", STUB_NPM)
        self.native = root / "native-claude"
        self.native.write_text(STUB_NATIVE, encoding="utf-8")
        self.placeholder = root / "placeholder-claude"
        self.placeholder.write_text(STUB_PLACEHOLDER, encoding="utf-8")
        self.stub("claude", STUB_LOGGER)
        self.stub("date", STUB_DATE)

    def stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def env(self, extra: Mapping[str, str] = {}) -> dict[str, str]:
        # The global bin of npm comes first, as setup-node puts it on PATH.
        path = os.pathsep.join([str(self.npm_prefix / "bin"), str(self.bin), os.environ["PATH"]])
        return {
            "PATH": path,
            "HOME": str(self.root),
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "STUB_LOG": str(self.log),
            "STUB_PYTHON": sys.executable,
            "STUB_NPM_PREFIX": str(self.npm_prefix),
            "STUB_NATIVE": str(self.native),
            "STUB_PLACEHOLDER": str(self.placeholder),
            "RUNNER_TEMP": str(self.runner_temp),
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

    def push_from_other(self, remote: Path, files: Mapping[str, str], message: str) -> str:
        """Push a commit that changes ``files`` from another clone; returns its SHA."""
        other = self.root / f"other-{len(list(self.root.glob('other-*')))}"
        self.git(self.root, "clone", "--quiet", str(remote), other.name)
        for relative, text in files.items():
            write(other / relative, text)
        self.git(other, "add", "--all")
        self.git(other, "commit", "--quiet", "--message", message)
        self.git(other, "push", "--quiet", "origin", "main")
        return self.git(other, "rev-parse", "HEAD")

    def run_data(self, files: Mapping[str, str]) -> Path:
        """What the commit job downloads: the data/ of the fetch job, as ``files``."""
        target = self.runner_temp / "run-data"
        for relative, text in files.items():
            write(target / relative, text)
        return target

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


BASE_FILES: Final = {
    "data/last_run.json": "{}\n",
    "data/health.json": "{}\n",
    "data/run_report.json": "{}\n",
    "data/events.json": "[]\n",
    "data/uid_map.json": "{}\n",
    "README.md": "x\n",
}
STUB_TOKEN: Final = "stub-workflow-token"
PUSH_ENV: Final = {"GITHUB_TOKEN": STUB_TOKEN, "GITHUB_SERVER_URL": "https://github.com"}


def data_of(files: Mapping[str, str]) -> dict[str, str]:
    """``files`` below data/, as the run data of the commit job holds them."""
    return {
        key.removeprefix("data/"): text for key, text in files.items() if key.startswith("data/")
    }


def commit_env(sandbox: Sandbox, work: Path, outcome: str = "ok", **extra: str) -> dict[str, str]:
    base = sandbox.git(work, "rev-parse", "HEAD")
    return {**PUSH_ENV, "RUN_OUTCOME": outcome, "RUN_BASE": base, **extra}


def changed_files(sandbox: Sandbox, remote: Path) -> list[str]:
    shown = sandbox.git(remote, "show", "--no-renames", "--name-status", "--format=", "main")
    return sorted(shown.splitlines())


# --- the pipeline step


@needs_shell
@pytest.mark.parametrize("status", [2, 3, 4])
def test_a_failed_run_stops_the_job_before_anything_is_committed(
    sandbox: Sandbox, status: int
) -> None:
    _, work = sandbox.repo(BASE_FILES)
    touch = str(work / "data" / "last_run.json")
    result = sandbox.run_step(
        "update.yml",
        "fetch",
        "run",
        work,
        {"GITHUB_EVENT_NAME": "schedule", "STUB_UV_STATUS": str(status), "STUB_UV_TOUCH": touch},
    )
    assert result.status == status
    assert set(result.outputs) == {"base"}
    assert "::error" in result.stdout
    if status == 3:
        assert "Output check failed" in result.stdout


@needs_shell
def test_a_run_that_could_not_start_stops_the_job(sandbox: Sandbox) -> None:
    _, work = sandbox.repo(BASE_FILES)
    extra = {"GITHUB_EVENT_NAME": "schedule", "STUB_UV_STATUS": "1"}
    result = sandbox.run_step("update.yml", "fetch", "run", work, extra)
    assert result.status == 1
    assert set(result.outputs) == {"base"}
    assert "before it recorded the run" in result.stdout


FAILED_RECORD: Final = '{"ok": false, "mode": "live"}'
"""data/last_run.json of a run that failed: what makes exit status 1 an outage."""


def failed_run(work: Path, record: str = FAILED_RECORD) -> dict[str, str]:
    """The environment of a scheduled run that exits 1 after it wrote ``record``."""
    touch = str(work / "data" / "last_run.json")
    return {
        "GITHUB_EVENT_NAME": "schedule",
        "STUB_UV_STATUS": "1",
        "STUB_UV_TOUCH": touch,
        "STUB_UV_RECORD": record,
    }


@needs_shell
def test_a_total_outage_is_handed_on_to_be_recorded(sandbox: Sandbox) -> None:
    _, work = sandbox.repo(BASE_FILES)
    result = sandbox.run_step("update.yml", "fetch", "run", work, failed_run(work))
    assert result.status == 0, result.stdout
    assert result.outputs == {
        "base": sandbox.git(work, "rev-parse", "HEAD"),
        "ran": "true",
        "outcome": "outage",
    }
    assert "::warning::seminars run failed with exit status 1" in result.stdout
    assert "every source failed, or the feeds could not be written" in result.stdout


@needs_shell
@pytest.mark.parametrize(
    "record",
    [
        '{"ok": true, "mode": "live"}',
        '{"mode": "live"}',
        '{"ok": "false"}',
        "[]",
        "not json",
    ],
)
def test_status_1_without_a_record_of_a_failed_run_stops_the_job(
    sandbox: Sandbox, record: str
) -> None:
    # Python exits with 1 for an error that nothing caught, also after the record of a
    # successful run was written: that run could not finish and must not be committed.
    _, work = sandbox.repo(BASE_FILES)
    result = sandbox.run_step("update.yml", "fetch", "run", work, failed_run(work, record))
    assert result.status == 1
    assert set(result.outputs) == {"base"}
    assert "does not record a failed run" in result.stdout
    assert "nothing is committed or deployed" in result.stdout


@needs_shell
def test_a_scheduled_run_may_skip_and_then_nothing_is_published(sandbox: Sandbox) -> None:
    _, work = sandbox.repo(BASE_FILES)
    scheduled = {"GITHUB_EVENT_NAME": "schedule"}
    result = sandbox.run_step("update.yml", "fetch", "run", work, scheduled)
    assert result.status == 0
    assert result.calls == ["uv run --locked seminars run --skip-if-done-today"]
    assert result.outputs == {"base": sandbox.git(work, "rev-parse", "HEAD"), "ran": "false"}


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
        "update.yml", "fetch", "run", work, {"GITHUB_EVENT_NAME": event, "STUB_UV_TOUCH": touch}
    )
    assert result.status == 0
    assert result.calls == [command]
    assert result.outputs == {
        "base": sandbox.git(work, "rev-parse", "HEAD"),
        "ran": "true",
        "outcome": "ok",
    }


@needs_shell
def test_the_first_record_of_a_new_repository_counts_as_a_run(sandbox: Sandbox) -> None:
    _, work = sandbox.repo({"README.md": "x\n"})
    touch = str(work / "data" / "last_run.json")
    extra = {"GITHUB_EVENT_NAME": "schedule", "STUB_UV_TOUCH": touch}
    result = sandbox.run_step("update.yml", "fetch", "run", work, extra)
    assert result.outputs["ran"] == "true"


# --- the commit step


@needs_shell
def test_the_commit_mirrors_the_data_of_the_run_and_is_pushed_by_the_bot(
    sandbox: Sandbox,
) -> None:
    remote, work = sandbox.repo(BASE_FILES)
    run = {**data_of(BASE_FILES), "events.json": "[1]\n", "archive/2026.jsonl": "{}\n"}
    del run["uid_map.json"]
    sandbox.run_data(run)
    write(work / "README.md", "changed by a bug\n")
    write(work / "stray.txt", "not data\n")
    result = sandbox.run_step("update.yml", "commit", "commit", work, commit_env(sandbox, work))
    assert result.status == 0, result.stdout
    head = sandbox.git(remote, "log", "-1", "--format=%an <%ae>|%cn|%s", "main")
    author, committer, subject = head.split("|")
    assert author == "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>"
    assert committer == "github-actions[bot]"
    assert subject == f"chore(data): daily update {STUB_DAY}"
    assert changed_files(sandbox, remote) == [
        "A\tdata/archive/2026.jsonl",
        "D\tdata/uid_map.json",
        "M\tdata/events.json",
    ]


@needs_shell
def test_a_total_outage_commits_only_the_records_of_the_run(sandbox: Sandbox) -> None:
    remote, work = sandbox.repo(BASE_FILES)
    records = dict.fromkeys(("health.json", "last_run.json"), '{"failed": true}\n')
    run = {**data_of(BASE_FILES), **records, "events.json": "[]\n[]\n", "new.json": "{}\n"}
    del run["uid_map.json"]
    sandbox.run_data(run)
    result = sandbox.run_step(
        "update.yml", "commit", "commit", work, commit_env(sandbox, work, "outage")
    )
    assert result.status == 0, result.stdout
    subject = sandbox.git(remote, "log", "-1", "--format=%s", "main")
    assert subject == f"chore(data): record a failed update {STUB_DAY}"
    assert changed_files(sandbox, remote) == ["M\tdata/health.json", "M\tdata/last_run.json"]


@needs_shell
def test_a_total_outage_of_a_new_repository_commits_its_records(sandbox: Sandbox) -> None:
    remote, work = sandbox.repo({"README.md": "x\n"})
    sandbox.run_data({"last_run.json": "{}\n", "run_report.json": "{}\n", "events.json": "[]\n"})
    result = sandbox.run_step(
        "update.yml", "commit", "commit", work, commit_env(sandbox, work, "outage")
    )
    assert result.status == 0, result.stdout
    assert changed_files(sandbox, remote) == ["A\tdata/last_run.json", "A\tdata/run_report.json"]


@needs_shell
@pytest.mark.parametrize("outcome", ["ok", "outage"])
def test_nothing_is_committed_when_data_is_unchanged(sandbox: Sandbox, outcome: str) -> None:
    remote, work = sandbox.repo(BASE_FILES)
    before = sandbox.git(remote, "rev-parse", "main")
    sandbox.run_data(data_of(BASE_FILES))
    write(work / "README.md", "changed outside data\n")
    result = sandbox.run_step(
        "update.yml", "commit", "commit", work, commit_env(sandbox, work, outcome)
    )
    assert result.status == 0
    assert sandbox.git(remote, "rev-parse", "main") == before
    assert "nothing to commit" in result.stdout


@needs_shell
def test_the_data_commit_is_rebased_when_main_moved_during_the_push(sandbox: Sandbox) -> None:
    remote, work = sandbox.repo(BASE_FILES)
    env = commit_env(sandbox, work)
    sandbox.push_from_other(remote, {"README.md": "pushed meanwhile\n"}, "docs: meanwhile")
    sandbox.run_data({**data_of(BASE_FILES), "events.json": "[1]\n"})
    result = sandbox.run_step("update.yml", "commit", "commit", work, env)
    assert result.status == 0, result.stdout
    subjects = sandbox.git(remote, "log", "--format=%s", "main").splitlines()
    assert subjects == [f"chore(data): daily update {STUB_DAY}", "docs: meanwhile", "seed"]
    assert "::warning::" in result.stdout


@needs_shell
def test_a_data_commit_that_conflicts_with_main_fails_cleanly(sandbox: Sandbox) -> None:
    remote, work = sandbox.repo(BASE_FILES)
    env = commit_env(sandbox, work)
    pushed = sandbox.push_from_other(
        remote, {"data/last_run.json": '{"by": "other"}\n'}, "chore(data): other"
    )
    sandbox.run_data({**data_of(BASE_FILES), "last_run.json": '{"by": "work"}\n'})
    result = sandbox.run_step("update.yml", "commit", "commit", work, env)
    assert result.status == 1, result.stdout
    assert "::error::The data commit could not be rebased onto main" in result.stdout
    assert sandbox.git(remote, "rev-parse", "main") == pushed
    # The rebase was undone: the clone holds its own data commit and nothing half-merged.
    assert "rebase" not in sandbox.git(work, "status")
    assert sandbox.git(work, "log", "-1", "--format=%s") == f"chore(data): daily update {STUB_DAY}"
    assert sandbox.git(work, "status", "--porcelain") == ""


@needs_shell
def test_main_that_moved_during_the_run_takes_the_data_when_it_left_data_alone(
    sandbox: Sandbox,
) -> None:
    remote, first = sandbox.repo(BASE_FILES)
    base = sandbox.git(first, "rev-parse", "HEAD")
    sandbox.push_from_other(remote, {"README.md": "pushed meanwhile\n"}, "docs: meanwhile")
    work = sandbox.clone(remote, "late")
    sandbox.run_data({**data_of(BASE_FILES), "events.json": "[1]\n"})
    env = {**PUSH_ENV, "RUN_OUTCOME": "ok", "RUN_BASE": base}
    result = sandbox.run_step("update.yml", "commit", "commit", work, env)
    assert result.status == 0, result.stdout
    subjects = sandbox.git(remote, "log", "--format=%s", "main").splitlines()
    assert subjects == [f"chore(data): daily update {STUB_DAY}", "docs: meanwhile", "seed"]
    assert changed_files(sandbox, remote) == ["M\tdata/events.json"]


@needs_shell
def test_main_that_changed_data_during_the_run_gets_nothing(sandbox: Sandbox) -> None:
    # Copying the data of the run over the new main would undo that change of data/.
    remote, first = sandbox.repo(BASE_FILES)
    base = sandbox.git(first, "rev-parse", "HEAD")
    pushed = sandbox.push_from_other(
        remote, {"data/uid_map.json": '{"fixed": "by hand"}\n'}, "fix(data): by hand"
    )
    work = sandbox.clone(remote, "late")
    sandbox.run_data({**data_of(BASE_FILES), "events.json": "[1]\n"})
    env = {**PUSH_ENV, "RUN_OUTCOME": "ok", "RUN_BASE": base}
    result = sandbox.run_step("update.yml", "commit", "commit", work, env)
    assert result.status == 1, result.stdout
    assert "::error::main changed data/ while the pipeline ran" in result.stdout
    assert sandbox.git(remote, "rev-parse", "main") == pushed


@needs_shell
@pytest.mark.parametrize(
    "base", ["", "main", "HEAD~1", "--upload-pack=touch pwned", "0" * 39, "A" * 40]
)
def test_a_base_that_is_not_a_commit_sha_is_refused(sandbox: Sandbox, base: str) -> None:
    remote, work = sandbox.repo(BASE_FILES)
    before = sandbox.git(remote, "rev-parse", "main")
    sandbox.run_data({**data_of(BASE_FILES), "events.json": "[1]\n"})
    env = {**PUSH_ENV, "RUN_OUTCOME": "ok", "RUN_BASE": base}
    result = sandbox.run_step("update.yml", "commit", "commit", work, env)
    assert result.status == 1
    assert "::error::The fetch job named no commit" in result.stdout
    assert sandbox.git(remote, "rev-parse", "main") == before
    assert not list(sandbox.root.rglob("pwned"))
    assert result.calls == []


@needs_shell
@pytest.mark.parametrize("outcome", ["", "OK", "failed", "ok; touch pwned"])
def test_an_unknown_outcome_is_refused(sandbox: Sandbox, outcome: str) -> None:
    remote, work = sandbox.repo(BASE_FILES)
    before = sandbox.git(remote, "rev-parse", "main")
    sandbox.run_data({**data_of(BASE_FILES), "events.json": "[1]\n"})
    result = sandbox.run_step(
        "update.yml", "commit", "commit", work, commit_env(sandbox, work, outcome)
    )
    assert result.status == 1
    assert "::error::The fetch job reported no known outcome" in result.stdout
    assert sandbox.git(remote, "rev-parse", "main") == before
    assert not list(sandbox.root.rglob("pwned"))


@needs_shell
def test_run_data_without_its_record_is_refused(sandbox: Sandbox) -> None:
    remote, work = sandbox.repo(BASE_FILES)
    before = sandbox.git(remote, "rev-parse", "main")
    sandbox.run_data({"events.json": "[1]\n"})
    result = sandbox.run_step("update.yml", "commit", "commit", work, commit_env(sandbox, work))
    assert result.status == 1
    assert "::error::The data of the run is missing" in result.stdout
    assert sandbox.git(remote, "rev-parse", "main") == before


@needs_shell
def test_run_data_with_a_link_is_refused(sandbox: Sandbox) -> None:
    remote, work = sandbox.repo(BASE_FILES)
    before = sandbox.git(remote, "rev-parse", "main")
    target = sandbox.run_data(data_of(BASE_FILES))
    (target / "events.json").unlink()
    (target / "events.json").symlink_to(work / ".git" / "config")
    result = sandbox.run_step("update.yml", "commit", "commit", work, commit_env(sandbox, work))
    assert result.status == 1
    assert "holds something other than plain files and directories" in result.stdout
    assert sandbox.git(remote, "rev-parse", "main") == before


@needs_shell
@pytest.mark.parametrize("hidden", [".git/config", "llm_cache/.hidden.json", ".gitattributes"])
def test_run_data_with_a_hidden_name_is_refused(sandbox: Sandbox, hidden: str) -> None:
    remote, work = sandbox.repo(BASE_FILES)
    before = sandbox.git(remote, "rev-parse", "main")
    sandbox.run_data({**data_of(BASE_FILES), hidden: "x\n"})
    result = sandbox.run_step("update.yml", "commit", "commit", work, commit_env(sandbox, work))
    assert result.status == 1
    assert "holds something other than plain files and directories" in result.stdout
    assert sandbox.git(remote, "rev-parse", "main") == before


@needs_shell
def test_the_push_gets_the_token_only_through_the_environment(sandbox: Sandbox) -> None:
    real_git = shutil.which("git")
    assert real_git
    remote, work = sandbox.repo(BASE_FILES)
    env = commit_env(sandbox, work)
    headers = sandbox.root / "headers.log"
    # A git in front of the real one: logs every command line and, at the push, the
    # header that git itself sees.
    sandbox.stub(
        "git",
        "#!/bin/sh\n"
        'printf \'git %s\\n\' "$*" >> "$STUB_LOG"\n'
        'if [ "$1" = push ]; then\n'
        '  "$REAL_GIT" config --get "http.https://github.com/.extraheader" >> "$STUB_HEADERS"\n'
        "fi\n"
        'exec "$REAL_GIT" "$@"\n',
    )
    sandbox.run_data({**data_of(BASE_FILES), "events.json": "[1]\n"})
    extra = {**env, "REAL_GIT": real_git, "STUB_HEADERS": str(headers)}
    result = sandbox.run_step("update.yml", "commit", "commit", work, extra)
    assert result.status == 0, result.stdout
    basic = base64.b64encode(f"x-access-token:{STUB_TOKEN}".encode()).decode()
    assert headers.read_text(encoding="utf-8") == f"AUTHORIZATION: basic {basic}\n"
    assert "git push --quiet origin HEAD:main" in result.calls
    for secret in (STUB_TOKEN, basic):
        assert not [call for call in result.calls if secret in call]
        assert secret not in result.stdout
        assert secret not in (work / ".git" / "config").read_text(encoding="utf-8")
    (sandbox.bin / "git").unlink()
    assert sandbox.git(remote, "log", "-1", "--format=%s", "main").startswith("chore(data)")


@needs_shell
def test_the_commit_step_stops_without_the_token(sandbox: Sandbox) -> None:
    remote, work = sandbox.repo(BASE_FILES)
    before = sandbox.git(remote, "rev-parse", "main")
    sandbox.run_data({**data_of(BASE_FILES), "events.json": "[1]\n"})
    env = commit_env(sandbox, work, GITHUB_TOKEN="")
    result = sandbox.run_step("update.yml", "commit", "commit", work, env)
    assert result.status == 1
    assert "::error::The workflow token is missing" in result.stdout
    assert sandbox.git(remote, "rev-parse", "main") == before
    assert sandbox.git(work, "rev-parse", "HEAD") == before


# --- installing Claude Code


def claude_config(work: Path, version: str | None) -> None:
    write(work / "config" / "llm.yaml", f"cli_versions:\n  claude-cli: {json.dumps(version)}\n")


INSTALL_CALL: Final = (
    "npm install --global --ignore-scripts --no-audit --no-fund @anthropic-ai/claude-code@2.1.322"
)
INSTALL_CALLS: Final = [INSTALL_CALL, "npm root --global", "npm prefix --global"]


@needs_shell
@pytest.mark.parametrize("job_id", ["fetch", "llm-smoke"])
@pytest.mark.parametrize(
    ("layout", "native"),
    [
        # What a global install really produces: the platform package below the CLI.
        ("nested", NESTED_NATIVE),
        # The fallback, for an npm that hoists the platform package.
        ("hoisted", HOISTED_NATIVE),
        # The binary of the package just installed wins over one left at the top.
        ("both", NESTED_NATIVE),
    ],
    ids=["nested", "hoisted", "both"],
)
def test_claude_code_is_installed_at_the_recorded_version(
    sandbox: Sandbox, job_id: str, layout: str, native: str
) -> None:
    work = sandbox.root / "work"
    claude_config(work, "2.1.322")
    result = sandbox.run_step("update.yml", job_id, "claude", work, {"STUB_NPM_LAYOUT": layout})
    assert result.status == 0, result.stdout
    assert "::warning" not in result.stdout
    # "claude" on PATH is now the binary of the platform package, not the placeholder.
    assert result.calls[1:] == [*INSTALL_CALLS, "native --version"]
    linked = sandbox.npm_prefix / "bin" / "claude"
    assert linked.resolve() == (sandbox.npm_prefix / native).resolve()


@needs_shell
@pytest.mark.parametrize("version", [None, "", "latest", "2.1", "1.2.3; touch pwned", "^2.1.0"])
def test_claude_code_is_skipped_without_a_plain_recorded_version(
    sandbox: Sandbox, version: str | None
) -> None:
    work = sandbox.root / "work"
    claude_config(work, version)
    result = sandbox.run_step("update.yml", "fetch", "claude", work)
    assert result.status == 0, result.stdout
    assert [call for call in result.calls if not call.startswith("uv ")] == []
    assert "::warning" in result.stdout
    assert not (work / "pwned").exists()


@needs_shell
@pytest.mark.parametrize("job_id", ["fetch", "llm-smoke"])
@pytest.mark.parametrize(
    ("broken", "calls"),
    [
        ({"STUB_NPM_STATUS": "1"}, [INSTALL_CALL]),
        ({"STUB_NPM_LAYOUT": "none"}, INSTALL_CALLS[:2]),
        ({"STUB_NPM_ROOT_STATUS": "1"}, INSTALL_CALLS[:2]),
        ({"STUB_NATIVE_STATUS": "1"}, [*INSTALL_CALLS, "native --version"]),
        (
            {"STUB_NPM_LAYOUT": "hoisted", "STUB_NATIVE_STATUS": "1"},
            [*INSTALL_CALLS, "native --version"],
        ),
    ],
    ids=["install", "no-binary", "root", "version", "hoisted-version"],
)
def test_a_failed_claude_code_install_only_warns(
    sandbox: Sandbox, job_id: str, broken: dict[str, str], calls: list[str]
) -> None:
    # A version missing on npm, a package without the binary, or a registry outage must
    # not cost the day's update: the LLM is optional and the provider chain copes
    # without the claude binary.
    work = sandbox.root / "work"
    claude_config(work, "2.1.322")
    result = sandbox.run_step("update.yml", job_id, "claude", work, broken)
    assert result.status == 0, result.stdout
    assert result.calls[1:] == calls
    assert "::warning::Claude Code 2.1.322 could not be installed" in result.stdout


@needs_shell
@pytest.mark.parametrize("job_id", ["fetch", "llm-smoke"])
def test_an_unreadable_llm_config_only_warns_at_the_install(sandbox: Sandbox, job_id: str) -> None:
    work = sandbox.root / "work"
    write(work / "config" / "llm.yaml", "cli_versions: [unclosed\n")
    result = sandbox.run_step("update.yml", job_id, "claude", work)
    assert result.status == 0, result.stdout
    assert [call for call in result.calls if not call.startswith("uv ")] == []
    assert "::warning::The claude-cli version could not be read" in result.stdout


# --- the digest


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
    ("docs/ARCHITECTURE.md", "docs/ARCHITECTURE.zh-CN.md"),
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


def test_no_value_is_left_to_be_filled_in_at_release() -> None:
    assert PLACEHOLDER_EN not in doc("README.md")
    assert PLACEHOLDER_ZH not in doc("README.zh-CN.md")


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
