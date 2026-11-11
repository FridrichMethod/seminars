"""The tooling configuration other parts of the project rely on stays as agreed."""

from __future__ import annotations

import ast
import importlib
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

RUNTIME_DEPENDENCIES = {
    "anthropic",
    "feedparser",
    "httpx",
    "icalendar",
    "openai",
    "pydantic",
    "python-dateutil",
    "python-dotenv",
    "pyyaml",
    "rapidfuzz",
    "selectolax",
    "typer",
    "tzdata",
}
DEV_DEPENDENCIES = {"hypothesis", "pyright", "pytest", "pytest-cov", "respx", "ruff"}
SUBPACKAGES = [
    "seminars.sources",
    "seminars.sources.generic",
    "seminars.sources.custom",
    "seminars.pipeline",
    "seminars.classify",
    "seminars.llm",
    "seminars.outputs",
]


def _names(requirements: list[str]) -> set[str]:
    return {re.split(r"[<>=!~ \[]", requirement, maxsplit=1)[0] for requirement in requirements}


@pytest.fixture(scope="module")
def pyproject() -> dict[str, Any]:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


class TestPyproject:
    def test_project_metadata(self, pyproject: dict[str, Any]) -> None:
        project = pyproject["project"]
        assert project["name"] == "seminars"
        assert project["requires-python"] == ">=3.12"
        assert project["license"] == "MIT"
        assert project["scripts"] == {"seminars": "seminars.cli:app"}
        assert "readme" not in project

    def test_dependencies(self, pyproject: dict[str, Any]) -> None:
        project = pyproject["project"]
        assert _names(project["dependencies"]) == RUNTIME_DEPENDENCIES
        assert _names(project["optional-dependencies"]["browser"]) == {"playwright"}
        assert _names(pyproject["dependency-groups"]["dev"]) == DEV_DEPENDENCIES
        assert not {"beautifulsoup4", "bs4", "lxml"} & _names(project["dependencies"])

    def test_ruff(self, pyproject: dict[str, Any]) -> None:
        ruff = pyproject["tool"]["ruff"]
        assert ruff["line-length"] == 100
        assert ".worktrees" in ruff["extend-exclude"]
        assert {"E", "F", "W", "I", "UP", "B", "SIM", "RUF"} <= set(ruff["lint"]["select"])
        ignored = set(ruff["lint"].get("ignore", []))
        assert not ignored & {"RUF001", "RUF002", "RUF003", "RUF"}

    def test_pyright(self, pyproject: dict[str, Any]) -> None:
        pyright = pyproject["tool"]["pyright"]
        assert pyright["include"] == ["src", "tests", "scripts"]
        assert pyright["typeCheckingMode"] in ("standard", "strict")

    def test_pytest_markers_are_strict_and_deselected_by_default(
        self, pyproject: dict[str, Any]
    ) -> None:
        options = pyproject["tool"]["pytest"]["ini_options"]
        addopts = options["addopts"]
        assert "--strict-markers" in addopts
        assert addopts[addopts.index("-m") + 1] == "not live and not browser"
        assert {marker.split(":")[0] for marker in options["markers"]} == {"live", "browser"}

    def test_coverage_targets_the_package(self, pyproject: dict[str, Any]) -> None:
        assert pyproject["tool"]["coverage"]["run"]["source_pkgs"] == ["seminars"]
        assert pyproject["tool"]["coverage"]["report"]["fail_under"] == 80

    def test_coverage_excludes_only_lines_that_are_a_bare_ellipsis(
        self, pyproject: dict[str, Any]
    ) -> None:
        patterns = pyproject["tool"]["coverage"]["report"]["exclude_also"]

        def excluded(line: str) -> bool:
            return any(re.search(pattern, line, re.MULTILINE) for pattern in patterns)

        for line in ("...", "        ...", "    ...  "):
            assert excluded(line), line
        for line in (
            "    def enabled_sources(self) -> tuple[SourceConfig, ...]:",
            "def retry(run: Callable[..., int]) -> int:",
            "    usage = 'seminars sources check [ID ...]'",
            "    values = [first, ..., last]",
            "    def put(self, key: str, value: str) -> None: ...",
        ):
            assert not excluded(line), line


class TestRepositoryFiles:
    def test_python_version_is_pinned(self) -> None:
        assert (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.13"

    def test_gitignore(self) -> None:
        lines = set((REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
        expected = {".env", ".venv/", ".worktrees/", "public/", ".cache/", "__pycache__/"}
        assert expected <= lines
        assert {".coverage", "htmlcov/", ".pytest_cache/", ".ruff_cache/"} <= lines

    def test_gitattributes_and_editorconfig(self) -> None:
        attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
        assert attributes == "* text=auto eol=lf\n"
        editorconfig = (REPO_ROOT / ".editorconfig").read_text(encoding="utf-8")
        for line in ("charset = utf-8", "end_of_line = lf", "insert_final_newline = true"):
            assert line in editorconfig

    def test_license(self) -> None:
        text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
        assert text.startswith("MIT License\n\nCopyright (c) 2026 FridrichMethod\n")

    def test_pre_commit_config(self) -> None:
        config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
        ruff_repo, local_repo = config["repos"]
        assert ruff_repo["repo"] == "https://github.com/astral-sh/ruff-pre-commit"
        assert [hook["id"] for hook in ruff_repo["hooks"]] == ["ruff-check", "ruff-format"]
        (hook,) = local_repo["hooks"]
        assert hook["entry"] == "python3 scripts/check_ascii.py"
        assert hook["pass_filenames"] is False

    def test_pre_commit_ruff_matches_the_locked_ruff(self) -> None:
        config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
        lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
        locked = next(
            package["version"] for package in lock["package"] if package["name"] == "ruff"
        )
        assert config["repos"][0]["rev"] == f"v{locked}"

    def test_optional_dependencies_are_never_imported_statically(self) -> None:
        """See the playwright_sync_api fixture in conftest.py for the reason and the remedy."""
        optional = {"playwright"}
        offenders: list[str] = []
        files = [*(REPO_ROOT / "src").rglob("*.py"), *(REPO_ROOT / "tests").glob("*.py")]
        for path in sorted(files):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                if any(module.split(".")[0] in optional for module in modules):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        assert offenders == []

    @pytest.mark.parametrize("name", SUBPACKAGES)
    def test_subpackages_exist_with_a_docstring_only(self, name: str) -> None:
        module = importlib.import_module(name)
        assert module.__doc__
        assert module.__file__ is not None
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert source.strip() == f'"""{module.__doc__}"""'
