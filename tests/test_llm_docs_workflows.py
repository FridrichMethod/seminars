"""docs/LLM_CREDENTIALS (both languages) say what the workflows and the tagger really do."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

ROOT: Final = Path(__file__).resolve().parents[1]
ENGLISH: Final = ROOT / "docs" / "LLM_CREDENTIALS.md"
CHINESE: Final = ROOT / "docs" / "LLM_CREDENTIALS.zh-CN.md"
ACTIONS_HEADING: Final = {
    ENGLISH: "## How GitHub Actions passes credentials",
    CHINESE: "## GitHub Actions \u600e\u4e48\u4f20\u51ed\u8bc1",
}
PAGE_TEXT_HEADING: Final = {
    ENGLISH: "## Page text is data",
    CHINESE: "## \u9875\u9762\u6587\u5b57\u662f\u6570\u636e",
}
RESIDUAL_RISK: Final = {
    ENGLISH: "**Residual risk of tagging.**",
    CHINESE: "**\u6253\u6807\u7b7e\u7684\u6b8b\u4f59\u98ce\u9669\u3002**",
}
BOTH: Final = [ENGLISH, CHINESE]


def section(path: Path, heading: str) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(heading + "\n")
    rest = text[start + len(heading) :]
    end = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def jobs_with_llm_secrets() -> set[str]:
    text = (ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8")
    jobs: dict[str, Any] = yaml.safe_load(text)["jobs"]
    return {
        job_id
        for job_id, job in jobs.items()
        for entry in job["steps"]
        if any(re.search(r"\bsecrets\.", str(value)) for value in entry.get("env", {}).values())
    }


@pytest.mark.parametrize("path", BOTH)
def test_every_job_that_receives_llm_secrets_is_named(path: Path) -> None:
    jobs = jobs_with_llm_secrets()
    assert jobs == {"fetch", "llm-smoke"}
    text = section(path, ACTIONS_HEADING[path])
    for job_id in jobs:
        assert f"`{job_id}`" in text, job_id
    assert "`seminars run`" in text
    assert "`seminars llm-smoke`" in text


@pytest.mark.parametrize("path", BOTH)
def test_the_job_that_pushes_and_the_install_rules_are_described(path: Path) -> None:
    text = section(path, ACTIONS_HEADING[path])
    for needle in ("`commit`", "`uv sync --locked`", "`--ignore-scripts`", "`pyproject.toml`"):
        assert needle in text, needle


@pytest.mark.parametrize("path", BOTH)
def test_the_residual_risk_of_batched_tagging_is_stated(path: Path) -> None:
    text = section(path, PAGE_TEXT_HEADING[path])
    assert RESIDUAL_RISK[path] in text
    assert "`batch_size`" in text


@pytest.mark.parametrize("path", BOTH)
def test_the_https_rule_of_the_endpoint_is_stated(path: Path) -> None:
    text = section(path, "### openai-compat")
    for needle in ("`https://`", "`http://`", "`localhost`", "`LLM_API_KEY`"):
        assert needle in text, needle


@pytest.mark.parametrize("path", BOTH)
def test_switching_off_says_that_stored_answers_still_apply(path: Path) -> None:
    heading = "## Switching the LLM off" if path == ENGLISH else "## \u5173\u6389 LLM"
    text = section(path, heading)
    assert "`data/llm_cache/`" in text
    assert "`seminars run --no-llm`" in text
