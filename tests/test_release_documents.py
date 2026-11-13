"""The release documents: the architecture in two languages, the changelog, the README values.

docs/ARCHITECTURE.md and its Chinese twin say the same: every name, path and value written
as code, and every table, is in both. The modules and files they name exist, and the
numbers and permissions they quote are those of the configuration, the code and the
workflows, and every direct import between the functional packages is named.
CHANGELOG.md records the version of pyproject.toml. Both READMEs link every feed at the
address the site is deployed to, give the webcal form of each, and link the site, its
status page, the architecture and the changelog; their tables of sources give each source
the same extraction method.
"""

from __future__ import annotations

import ast
import itertools
import re
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any, Final, get_args

import pytest
import yaml

from seminars.config import load_config
from seminars.contracts import llm_cache_refresh_days
from seminars.models import FEED_IDS, ExtractionMethod
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
FUNCTIONAL_PACKAGES: Final = frozenset({"classify", "llm", "outputs", "pipeline", "sources"})


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


def imported_modules(path: Path) -> list[str]:
    """The absolute imports of a module, by module name."""
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names


def module_path(name: str) -> str:
    """``seminars.llm.base`` as ``llm/base.py``, a package as ``pipeline/``."""
    relative = "/".join(name.split(".")[1:])
    return f"{relative}/" if (SRC / relative).is_dir() else f"{relative}.py"


def imports_between_functional_packages() -> set[tuple[str, str]]:
    """Each (importing module, imported module) whose functional packages differ."""
    pairs: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        importer = path.relative_to(SRC).as_posix()
        package = importer.split("/")[0]
        if package not in FUNCTIONAL_PACKAGES:
            continue
        for name in imported_modules(path):
            parts = name.split(".")
            target = parts[1] if parts[0] == "seminars" and len(parts) > 1 else None
            if target in FUNCTIONAL_PACKAGES and target != package:
                pairs.add((importer, module_path(name)))
    return pairs


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

    def test_every_direct_import_between_functional_packages_is_named(self, path: str) -> None:
        pairs = imports_between_functional_packages()
        named = inline_code(path)
        assert pairs
        assert [pair for pair in sorted(pairs) if not set(pair) <= set(named)] == []

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

    def test_it_states_the_concurrency_rule_of_the_update_workflow(self, path: str) -> None:
        # GitHub never cancels a run in progress of a group without cancel-in-progress, but
        # keeps only one waiting run per group: a newer one replaces it.
        assert workflow("update.yml")["concurrency"]["cancel-in-progress"] is False
        rule = {
            ENGLISH: (
                "A run in progress is never cancelled; GitHub keeps one waiting run per group, "
                "so a newer run replaces one that is still waiting."
            ),
            CHINESE: (
                "\u6b63\u5728\u8fdb\u884c\u7684\u8fd0\u884c\u4ece\u4e0d\u53d6\u6d88\uff1b"
                "GitHub \u5728\u6bcf\u4e2a group \u91cc\u53ea\u4fdd\u7559\u4e00\u4e2a"
                "\u7b49\u5f85\u4e2d\u7684\u8fd0\u884c\uff0c\u6240\u4ee5\u8f83\u65b0\u7684"
                "\u8fd0\u884c\u4f1a\u53d6\u4ee3\u4ecd\u5728\u7b49\u5f85\u7684\u90a3\u4e00\u4e2a\u3002"
            ),
        }
        # A line of the Chinese text may wrap anywhere, so white space does not count.
        assert "".join(rule[path].split()) in "".join(prose(path).split())

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
            f"removed once it has missed {settings.events.delete_after_missing_runs} such runs",
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
            f"\u7f3a\u5e2d\u7d2f\u8ba1 {settings.events.delete_after_missing_runs} "
            "\u6b21\u540e\u5220\u9664",
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


def extraction_by_source(path: str, heading: str) -> dict[str, str]:
    """The last column (``heading``) of a README's table of sources, by source id."""
    lines = text(path).splitlines()
    start = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("|") and line.endswith(f"| {heading} |")
    )
    methods: dict[str, str] = {}
    for line in itertools.takewhile(lambda row: row.startswith("|"), lines[start + 2 :]):
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        source = re.fullmatch(r".* \(`([a-z0-9-]+)`\)", cells[0])
        assert source, line
        methods[source.group(1)] = cells[-1]
    return methods


def test_both_readmes_give_each_source_the_same_extraction_method() -> None:
    english = extraction_by_source("README.md", "Extraction")
    chinese = extraction_by_source("README.zh-CN.md", "\u62bd\u53d6\u65b9\u5f0f")
    assert english
    assert english == chinese
    assert set(english) <= {source.id for source in CONFIG.sources}
    assert set(english.values()) <= set(get_args(ExtractionMethod))
