"""The release documents: the architecture in two languages, the changelog, the README values.

docs/ARCHITECTURE.md and its Chinese twin say the same: every name, path and value written
as code, and every table, is in both. The modules and files they name exist, and the
numbers and permissions they quote are those of the configuration, the code and the
workflows. CHANGELOG.md records the version of pyproject.toml. Both READMEs link every
feed at the address the site is deployed to, give the webcal form of each, and link the
site, its status page, the architecture and the changelog.
"""

from __future__ import annotations

import re
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from seminars.config import load_config
from seminars.contracts import llm_cache_refresh_days
from seminars.models import FEED_IDS
from seminars.models.health import HEALTH_HISTORY_LIMIT
from seminars.pipeline.collect import MAX_FETCH_WORKERS, SOURCE_FETCH_LIMIT_SECONDS

ROOT: Final = Path(__file__).resolve().parents[1]
SRC: Final = ROOT / "src" / "seminars"
CONFIG: Final = load_config(ROOT, env={})
ENGLISH: Final = "docs/ARCHITECTURE.md"
CHINESE: Final = "docs/ARCHITECTURE.zh-CN.md"
READMES: Final = ("README.md", "README.zh-CN.md")
FENCE: Final = re.compile(r"^```[^\n]*\n.*?^```$", re.DOTALL | re.MULTILINE)
INLINE_CODE: Final = re.compile(r"`([^`\n]+)`")
MODULE: Final = re.compile(r"(?:[a-z_]+/)*[a-z_]+\.py")
REPOSITORY_PATH: Final = re.compile(r"(?:config|docs|locales|scripts|site|tests)/[A-Za-z0-9_./-]*")
CONFIG_FILE: Final = re.compile(r"[a-z]+\.yaml")
PERMISSION: Final = re.compile(r"[a-z-]+: (?:read|write)")
READ_ONLY: Final = {ENGLISH: "read only", CHINESE: "\u53ea\u8bfb"}
UPDATE_SECTION: Final = (r"\*\*`update\.yml`\*\*", r"\*\*`digest\.yml`\*\*")


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def prose(path: str) -> str:
    """The document without its code blocks, every run of white space one space."""
    return " ".join(FENCE.sub("", text(path)).split())


def inline_code(path: str) -> Counter[str]:
    return Counter(INLINE_CODE.findall(FENCE.sub("", text(path))))


def table_rows(document: str) -> list[list[str]]:
    rows = [line for line in document.splitlines() if line.startswith("|")]
    return [[cell.strip() for cell in row.strip("|").split("|")] for row in rows]


def workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load(text(f".github/workflows/{name}"))


class TestTheTwoLanguagesSayTheSame:
    def test_the_same_names_paths_and_values_in_code(self) -> None:
        assert inline_code(ENGLISH) == inline_code(CHINESE)

    def test_the_same_tables(self) -> None:
        def shape(path: str) -> list[int]:
            return [len(row) for row in table_rows(text(path))]

        assert shape(ENGLISH) == shape(CHINESE)


@pytest.mark.parametrize("path", [ENGLISH, CHINESE])
class TestTrueToTheProject:
    def test_every_module_and_file_it_names_exists(self, path: str) -> None:
        missing = []
        for code in inline_code(path):
            if REPOSITORY_PATH.fullmatch(code):
                found = (ROOT / code).exists()
            elif MODULE.fullmatch(code):
                found = (SRC / code).is_file()
            elif CONFIG_FILE.fullmatch(code):
                found = (ROOT / "config" / code).is_file()
            else:
                continue
            if not found:
                missing.append(code)
        assert missing == []

    def test_every_job_of_the_update_workflow_is_listed_with_its_permissions(
        self, path: str
    ) -> None:
        start, end = UPDATE_SECTION
        section = re.split(end, re.split(start, text(path))[1])[0]
        listed: dict[str, set[str]] = {}
        for job, permissions, _what in table_rows(section)[2:]:
            granted = set(re.findall(r"`([^`]+)`", permissions))
            assert granted <= {p for p in granted if PERMISSION.fullmatch(p)}
            assert granted or permissions == READ_ONLY[path], job
            listed[job.strip("`")] = granted
        jobs = workflow("update.yml")["jobs"]
        assert listed == {
            name: {f"{key}: {value}" for key, value in job.get("permissions", {}).items()}
            for name, job in jobs.items()
        }

    def test_the_numbers_it_quotes_are_those_of_the_configuration(self, path: str) -> None:
        settings, llm = CONFIG.settings, CONFIG.llm
        refresh = llm_cache_refresh_days(llm.cache_ttl_days)
        minutes = round(SOURCE_FETCH_LIMIT_SECONDS / 60)
        english = [
            f"the window of {settings.window.days_back} days back and "
            f"{settings.window.days_ahead} ahead",
            f"at most {MAX_FETCH_WORKERS} at a time",
            f"may take {minutes} minutes in all",
            f"at most {settings.http.max_requests_per_host} requests per run",
            f"at most {settings.dedup.max_start_difference_minutes} minutes apart",
            f"a token set ratio of at least {settings.dedup.title_similarity}",
            f"removed after {settings.events.delete_after_missing_runs} of them in a row",
            f"`max_calls_per_run` requests ({llm.max_calls_per_run})",
            f"older than {refresh} days",
            f"`cache_ttl_days` ({llm.cache_ttl_days})",
            f"the last {HEALTH_HISTORY_LIMIT} runs",
            f"failed {settings.health.source_failure_threshold} runs in a row",
            f"failed {settings.health.llm_failure_threshold} runs in a row",
            f"`<uid>@{settings.uid_domain}`",
        ]
        chinese = [
            f"\u5f80\u524d {settings.window.days_back} \u5929\u5f80\u540e "
            f"{settings.window.days_ahead} \u5929",
            f"\u540c\u65f6\u6700\u591a {MAX_FETCH_WORKERS} \u4e2a",
            f"\u6700\u591a\u7528 {minutes} \u5206\u949f",
            f"\u6700\u591a {settings.http.max_requests_per_host} \u4e2a\u8bf7\u6c42",
            f"\u76f8\u5dee\u4e0d\u8d85\u8fc7 {settings.dedup.max_start_difference_minutes} "
            "\u5206\u949f",
            f"token set ratio \u81f3\u5c11 {settings.dedup.title_similarity}",
            f"\u8fde\u7eed {settings.events.delete_after_missing_runs} \u6b21\u540e\u5220\u9664",
            f"`max_calls_per_run` \u4e2a\u8bf7\u6c42\uff08{llm.max_calls_per_run}\uff09",
            f"\u65e9\u4e8e {refresh} \u5929",
            f"`cache_ttl_days`\uff08{llm.cache_ttl_days}\uff09",
            f"\u6700\u8fd1 {HEALTH_HISTORY_LIMIT} \u6b21\u8fd0\u884c",
            f"\u8fde\u7eed {settings.health.source_failure_threshold} \u6b21\u5931\u8d25",
            f"\u8fde\u7eed {settings.health.llm_failure_threshold} \u6b21\u5931\u8d25\u7684 LLM",
            f"`<uid>@{settings.uid_domain}`",
        ]
        document = prose(path)
        phrases = english if path == ENGLISH else chinese
        assert [phrase for phrase in phrases if phrase not in document] == []


def test_the_changelog_records_the_version_of_the_project() -> None:
    changelog = text("CHANGELOG.md")
    version = tomllib.loads(text("pyproject.toml"))["project"]["version"]
    releases = re.findall(r"^## v(\S+)", changelog, re.MULTILINE)
    assert changelog.startswith("# Changelog\n")
    assert releases[0] == version
    newest = changelog.split(f"## v{version}", 1)[1].split("\n## ", 1)[0]
    assert "\n### Added\n" in newest


@pytest.mark.parametrize("path", READMES)
def test_the_readme_links_every_feed_at_its_published_address(path: str) -> None:
    base = str(CONFIG.settings.site.base_url)
    assert base.startswith("https://")
    readme = text(path)
    for feed in FEED_IDS:
        assert f"]({base}feeds/{feed}.ics)" in readme, feed
        assert f"\nwebcal://{base.removeprefix('https://')}feeds/{feed}.ics\n" in readme, feed


@pytest.mark.parametrize(
    ("path", "architecture"),
    [("README.md", "docs/ARCHITECTURE.md"), ("README.zh-CN.md", "docs/ARCHITECTURE.zh-CN.md")],
)
def test_the_readme_links_the_site_the_status_page_and_the_release_documents(
    path: str, architecture: str
) -> None:
    base = str(CONFIG.settings.site.base_url)
    readme = text(path)
    for target in (base, f"{base}status.html", architecture, "CHANGELOG.md"):
        assert f"]({target})" in readme, target
