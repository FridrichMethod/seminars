"""Tests for scripts/check_ascii.py, the charset gate of the repository."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_ascii.py"

EM_DASH = "\u2014"
CURLY_QUOTE = "\u201c"
NBSP = "\u00a0"
ROCKET = "\U0001f680"
CHINESE = "\u4e2d\u6587"
E_ACUTE = "\u00e9"
LIGATURE_FI = "\ufb01"
ZERO_WIDTH_SPACE = "\u200b"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_ascii", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_ascii = _load_script()


def check(path: str, text: str) -> list[str]:
    return check_ascii.check_bytes(path, text.encode("utf-8"))


class TestClassify:
    @pytest.mark.parametrize(
        ("path", "mode"),
        [
            ("src/seminars/models.py", "strict"),
            ("README.md", "strict"),
            ("locales/en.json", "strict"),
            ("docs/notes.zh-CN.txt", "strict"),
            ("README.zh-CN.md", "zh"),
            ("docs/ARCHITECTURE.zh-CN.md", "zh"),
            ("locales/zh-CN.json", "zh"),
            ("data/events.json", "free"),
            ("data/archive/2026.jsonl", "free"),
            ("tests/fixtures/sites-chemistry/events.json", "free"),
            ("tests/data/sample.json", "strict"),
            ("mydata/events.json", "strict"),
        ],
    )
    def test_modes(self, path: str, mode: str) -> None:
        assert check_ascii.classify(path) == mode


class TestStrictFiles:
    def test_plain_ascii_passes(self) -> None:
        assert check("src/a.py", "x = 1\nprint('ok')\n") == []

    def test_reports_path_line_column_and_code_point(self) -> None:
        messages = check("docs/a.md", "first line\nab" + EM_DASH + "cd\n")
        assert messages == ["docs/a.md:2:3 U+2014 EM DASH"]

    @pytest.mark.parametrize(
        "char", [EM_DASH, CURLY_QUOTE, NBSP, ROCKET, CHINESE[0], E_ACUTE, LIGATURE_FI]
    )
    def test_rejects_every_non_ascii_character(self, char: str) -> None:
        messages = check("config/settings.yaml", "key: a" + char + "\n")
        assert len(messages) == 1
        assert messages[0].startswith(f"config/settings.yaml:1:7 U+{ord(char):04X}")

    def test_rejects_tab_outside_makefiles(self) -> None:
        assert check("src/a.py", "if x:\n\tpass\n") == ["src/a.py:2:1 U+0009"]

    @pytest.mark.parametrize("path", ["Makefile", "tools/rules.mk"])
    def test_allows_tab_in_makefiles(self, path: str) -> None:
        assert check(path, "all:\n\techo ok\n") == []

    def test_rejects_control_characters(self) -> None:
        assert check("a.txt", "a\x07b\n") == ["a.txt:1:2 U+0007"]

    def test_reports_every_occurrence(self) -> None:
        messages = check("a.md", EM_DASH + "\n" + EM_DASH + EM_DASH + "\n")
        assert [m.split(" ")[0] for m in messages] == ["a.md:1:1", "a.md:2:1", "a.md:2:2"]


class TestZhFiles:
    def test_chinese_and_cjk_punctuation_pass(self) -> None:
        text = "# " + CHINESE + "\n\n" + CURLY_QUOTE + CHINESE + "\u201d\uff0c" + EM_DASH + "\n"
        assert check("README.zh-CN.md", text) == []
        assert check("locales/zh-CN.json", '{"a": "' + CHINESE + '"}\n') == []

    @pytest.mark.parametrize("char", [ROCKET, "\u2705", "\u2b50", "\ufe0f"])
    def test_emoji_is_rejected(self, char: str) -> None:
        messages = check("README.zh-CN.md", CHINESE + char + "\n")
        assert len(messages) == 1
        assert messages[0].startswith(f"README.zh-CN.md:1:3 U+{ord(char):04X}")

    @pytest.mark.parametrize("char", [NBSP, LIGATURE_FI, ZERO_WIDTH_SPACE, "\t", "\u202f"])
    def test_decorative_and_invisible_characters_are_rejected(self, char: str) -> None:
        messages = check("locales/zh-CN.json", '{"a": "' + char + '"}\n')
        assert len(messages) == 1
        assert f"U+{ord(char):04X}" in messages[0]


class TestFreeFiles:
    @pytest.mark.parametrize("path", ["data/events.json", "tests/fixtures/x/page.html"])
    def test_any_unicode_is_allowed(self, path: str) -> None:
        text = "Poincar" + E_ACUTE + " " + ROCKET + NBSP + "\tend\n"
        assert check(path, text) == []

    def test_line_ending_and_newline_rules_still_apply(self) -> None:
        assert check("data/a.json", "{}") == ["data/a.json: missing final newline"]
        assert check("tests/fixtures/x/a.ics", "BEGIN\r\nEND\r\n") == [
            "tests/fixtures/x/a.ics:1: CRLF line ending (2 line(s) in total)"
        ]


class TestFileLevelRules:
    def test_bom_is_rejected(self) -> None:
        data = b"\xef\xbb\xbf" + b"x = 1\n"
        assert check_ascii.check_bytes("a.py", data) == [
            "a.py:1:1 U+FEFF byte order mark at start of file"
        ]

    def test_crlf_is_rejected_once_per_file(self) -> None:
        assert check("a.py", "x = 1\r\ny = 2\r\n") == [
            "a.py:1: CRLF line ending (2 line(s) in total)"
        ]

    def test_missing_final_newline_is_rejected(self) -> None:
        assert check("a.py", "x = 1") == ["a.py: missing final newline"]

    def test_empty_file_passes(self) -> None:
        assert check_ascii.check_bytes("src/pkg/py.typed", b"") == []

    def test_binary_file_is_skipped(self) -> None:
        assert check_ascii.check_bytes("site/icon.png", b"\x89PNG\r\n\x1a\n\x00\x00\xff") == []

    def test_invalid_utf8_is_rejected(self) -> None:
        assert check_ascii.check_bytes("a.txt", b"caf\xe9\n") == [
            "a.txt: not valid UTF-8 (byte offset 3)"
        ]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _run_script(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(repo)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


class TestCommandLine:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        _git(tmp_path, "init", "--quiet")
        return tmp_path

    def test_clean_tree_exits_zero(self, repo: Path) -> None:
        (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "data").mkdir()
        (repo / "data" / "events.json").write_text('{"n": "' + E_ACUTE + '"}\n', encoding="utf-8")
        _git(repo, "add", "-A")
        result = _run_script(repo)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK (2 files)" in result.stdout

    def test_violation_exits_non_zero_and_prints_location(self, repo: Path) -> None:
        (repo / "bad.md").write_text("a " + EM_DASH + " b\n", encoding="utf-8")
        _git(repo, "add", "-A")
        result = _run_script(repo)
        assert result.returncode == 1
        assert "bad.md:1:3 U+2014 EM DASH" in result.stdout
        assert "1 violation(s) in 1 of 1 file(s)" in result.stderr

    def test_untracked_files_are_ignored(self, repo: Path) -> None:
        (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        (repo / "untracked.md").write_text(EM_DASH + "\n", encoding="utf-8")
        assert _run_script(repo).returncode == 0

    def test_output_is_capped_per_file(self, repo: Path) -> None:
        (repo / "many.md").write_text((EM_DASH * 30) + "\n", encoding="utf-8")
        _git(repo, "add", "-A")
        result = _run_script(repo)
        assert result.returncode == 1
        assert result.stdout.count("U+2014") == check_ascii.MAX_REPORTS_PER_FILE
        assert "... and 10 more in this file" in result.stdout

    def test_outside_a_git_repository_exits_two(self, tmp_path: Path) -> None:
        result = _run_script(tmp_path)
        assert result.returncode == 2
        assert "cannot list git-tracked files" in result.stderr


class TestScriptItself:
    def test_is_pure_ascii(self) -> None:
        assert SCRIPT.read_bytes().isascii()

    def test_parses_as_python_3_8(self) -> None:
        ast.parse(SCRIPT.read_text(encoding="utf-8"), feature_version=(3, 8))

    def test_imports_only_the_standard_library(self) -> None:
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported <= set(sys.stdlib_module_names)


def test_this_repository_is_clean() -> None:
    """The gate itself, so that plain pytest also enforces the charset rules."""
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if inside.returncode != 0:
        pytest.skip("not a git work tree")
    result = _run_script(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
