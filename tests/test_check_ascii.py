"""Tests for scripts/check_ascii.py, the charset gate of the repository."""

from __future__ import annotations

import ast
import importlib.util
import os
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
            ("README.zh-CN.md", "clean"),
            ("docs/ARCHITECTURE.zh-CN.md", "clean"),
            ("locales/zh-CN.json", "clean"),
            ("data/events.json", "clean"),
            ("data/archive/2026.jsonl", "clean"),
            ("data/llm_cache/ab/cdef.json", "clean"),
            ("tests/fixtures/sites-chemistry/events.json", "free"),
            ("tests/fixtures/_site/events.json", "free"),
            ("tests/data/sample.json", "strict"),
            ("tests/golden/all.ics", "strict"),
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


CLEAN_PATHS = ["README.zh-CN.md", "locales/zh-CN.json", "data/events.json"]
INVISIBLE = ["\u202f", "\u2009", "\u2028", "\u205f", "\u061c", "\u180e", "\ufe0e"]
"""Narrow no-break, thin and mathematical space, line separator, marks and selectors."""
UNPRINTABLE = ["\u115f", "\u1160", "\u3164", "\uffa0", "\ufff9", "\ufffb", "\ufffc"]
UNPRINTABLE += ["\ue000", "\uf0b7", "\uf8ff"]
UNPRINTABLE += [chr(0xF0000), chr(0xF0045), chr(0xFFFFD), chr(0x100000), chr(0x10FFFD)]
"""Hangul fillers, annotation marks, the object replacement character and private use."""


class TestCleanFiles:
    """Files that may hold non-ASCII text but nothing decorative: zh-CN texts and data/."""

    def test_chinese_and_cjk_punctuation_pass(self) -> None:
        text = "# " + CHINESE + "\n\n" + CURLY_QUOTE + CHINESE + "\u201d\uff0c" + EM_DASH + "\n"
        assert check("README.zh-CN.md", text) == []
        assert check("locales/zh-CN.json", '{"a": "' + CHINESE + '"}\n') == []

    def test_scraped_names_and_symbols_pass_in_data(self) -> None:
        line = '{"speaker": "Poincar' + E_ACUTE + '", "title": "Maps \u211d\u00b2 \u2192 \u211d"}\n'
        assert check("data/events.json", line) == []
        assert check("data/archive/2026.jsonl", line) == []

    @pytest.mark.parametrize("path", CLEAN_PATHS)
    @pytest.mark.parametrize("char", [ROCKET, "\u2705", "\u2b50", "\ufe0f"])
    def test_emoji_is_rejected(self, path: str, char: str) -> None:
        messages = check(path, CHINESE + char + "\n")
        assert len(messages) == 1
        assert messages[0].startswith(f"{path}:1:3 U+{ord(char):04X}")

    def test_scientific_signs_pass_in_data(self) -> None:
        """Sun, planet, female, male, flat and sharp signs sit in the emoji blocks."""
        line = '{"title": "A 10 M\u2609 star, 2 M\u2643, \u2640 and \u2642 mice, the \u266d map"}\n'
        assert check("data/events.json", line) == []

    @pytest.mark.parametrize("path", CLEAN_PATHS)
    @pytest.mark.parametrize(
        "char", [NBSP, LIGATURE_FI, ZERO_WIDTH_SPACE, "\t", *INVISIBLE, *UNPRINTABLE]
    )
    def test_decorative_and_invisible_characters_are_rejected(self, path: str, char: str) -> None:
        messages = check(path, '{"a": "' + char + '"}\n')
        assert len(messages) == 1
        assert f"U+{ord(char):04X}" in messages[0]

    @pytest.mark.parametrize("char", ["\ud55c", "\u1112\u1161", "\uf900", "\ufffd"])
    def test_text_next_to_the_unprintable_ranges_passes(self, char: str) -> None:
        """Hangul, a CJK compatibility ideograph and the replacement character are text."""
        assert check("data/events.json", '{"a": "' + char + '"}\n') == []


class TestFreeFiles:
    """Recorded fixtures are verbatim page snapshots: any valid UTF-8 is accepted."""

    def test_any_unicode_is_allowed(self) -> None:
        text = "Poincar" + E_ACUTE + " " + ROCKET + NBSP + LIGATURE_FI + "\tend\n"
        assert check("tests/fixtures/x/page.html", text) == []

    def test_line_ending_and_newline_rules_still_apply(self) -> None:
        assert check("tests/fixtures/x/a.json", "{}") == [
            "tests/fixtures/x/a.json: missing final newline"
        ]
        assert check("tests/fixtures/x/a.ics", "BEGIN\r\nEND\r\n") == [
            "tests/fixtures/x/a.ics:1: CRLF line ending (2 line(s) in total)"
        ]


class TestGoldenFiles:
    """Expected output is held to the strict rule.

    The docstring of ``seminars.contracts.IcsFeedWriter`` tells the writer
    of the feeds how its tests live with that.
    """

    FEED = "BEGIN:VCALENDAR\r\nSUMMARY:Seminar\r\nEND:VCALENDAR\r\n"

    def test_a_feed_with_its_real_line_endings_cannot_be_a_golden_file(self) -> None:
        assert check("tests/golden/all.ics", self.FEED) == [
            "tests/golden/all.ics:1: CRLF line ending (3 line(s) in total)"
        ]

    def test_the_same_feed_with_lf_is_accepted(self) -> None:
        assert check("tests/golden/all.ics", self.FEED.replace("\r\n", "\n")) == []

    def test_non_ascii_text_cannot_be_stored_in_a_golden_file(self) -> None:
        line = "SUMMARY:Poincar" + E_ACUTE + " seminar\n"
        (message,) = check("tests/golden/all.ics", line)
        assert message.startswith("tests/golden/all.ics:1:16 U+00E9")


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

    def test_emoji_in_data_fails_but_not_in_fixtures(self, repo: Path) -> None:
        for directory in ("data", "tests/fixtures/sites-x"):
            (repo / directory).mkdir(parents=True)
            (repo / directory / "events.json").write_text(
                '{"title": "Launch ' + ROCKET + '"}\n', encoding="utf-8"
            )
        _git(repo, "add", "-A")
        result = _run_script(repo)
        assert result.returncode == 1
        assert "data/events.json:1:19 U+1F680 ROCKET" in result.stdout
        assert "tests/fixtures" not in result.stdout
        assert "1 violation(s) in 1 of 2 file(s)" in result.stderr

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


class TestEncodingsOtherThanUtf8:
    """Text in UTF-16 or UTF-32 is reported, not skipped as binary."""

    @pytest.mark.parametrize(
        ("encoding", "name"),
        [
            ("utf-16", "UTF-16 LE"),
            ("utf-32", "UTF-32 LE"),
        ],
    )
    def test_a_byte_order_mark_names_the_encoding(self, encoding: str, name: str) -> None:
        data = ("Talk " + ROCKET + " " + CURLY_QUOTE + "quoted\n").encode(encoding)
        assert check_ascii.check_bytes("README.md", data) == [
            f"README.md: not UTF-8 ({name} byte order mark)"
        ]

    @pytest.mark.parametrize(
        ("bom", "name"),
        [(b"\xfe\xff", "UTF-16 BE"), (b"\x00\x00\xfe\xff", "UTF-32 BE")],
    )
    def test_big_endian_byte_order_marks(self, bom: bytes, name: str) -> None:
        codec = "utf-16-be" if name == "UTF-16 BE" else "utf-32-be"
        data = bom + "x = 1\n".encode(codec)
        assert check_ascii.check_bytes("a.py", data) == [
            f"a.py: not UTF-8 ({name} byte order mark)"
        ]

    @pytest.mark.parametrize("path", ["src/seminars/x.py", "tests/fixtures/x/page.html", "notes"])
    def test_utf16_without_a_byte_order_mark_is_reported(self, path: str) -> None:
        data = "print('hi')\n".encode("utf-16-le")
        assert check_ascii.check_bytes(path, data) == [
            f"{path}: NUL byte at byte offset 1: not UTF-8 text"
        ]

    @pytest.mark.parametrize("path", ["site/icon.png", "site/fonts/a.woff2", "docs/b.PDF"])
    def test_files_with_a_binary_extension_are_skipped(self, path: str) -> None:
        assert check_ascii.check_bytes(path, b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfe") == []

    @pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"])
    def test_wide_text_in_a_fixture_is_reported(self, codec: str) -> None:
        data = ("<p>Kn" + E_ACUTE + "ts</p>\n").encode(codec)
        (message,) = check_ascii.check_bytes("tests/fixtures/x/page.html", data)
        assert message.startswith("tests/fixtures/x/page.html: NUL byte at byte offset ")

    def test_a_stray_nul_byte_in_a_fixture_is_part_of_the_snapshot(self) -> None:
        data = b"<html><body><p>Knots\x00 and links</p></body></html>\n"
        assert check_ascii.check_bytes("tests/fixtures/x/page.html", data) == []

    def test_a_stray_nul_byte_elsewhere_is_reported(self) -> None:
        data = b"<html><body><p>Knots\x00 and links</p></body></html>\n"
        assert check_ascii.check_bytes("site/page.html", data) == [
            "site/page.html: NUL byte at byte offset 20: not UTF-8 text"
        ]

    def test_a_binary_extension_is_not_a_name_that_ends_in_its_letters(self) -> None:
        assert check_ascii.check_bytes("notpng", b"a\x00\n") == [
            "notpng: NUL byte at byte offset 1: not UTF-8 text"
        ]


def _run_on_message(path: Path, git_editor: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "GIT_EDITOR"}
    if git_editor is not None:
        env["GIT_EDITOR"] = git_editor
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--commit-msg", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=env,
    )


def _commit(repo: Path, *messages: str) -> subprocess.CompletedProcess[str]:
    """``git commit -m ...`` in ``repo`` with the commit-msg hook that runs the script."""
    hooks = repo / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "commit-msg"
    hook.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{SCRIPT}" --commit-msg "$1"\n')
    hook.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    options = ["-c", f"core.hooksPath={hooks}", "-c", "user.name=Test", "-c"]
    options += ["user.email=test@example.invalid", "-c", "commit.gpgsign=false"]
    message_options = [part for message in messages for part in ("-m", message)]
    return subprocess.run(
        ["git", *options, "commit", "--allow-empty", "--quiet", *message_options],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=env,
    )


class TestCommitMessages:
    def test_an_ascii_message_passes(self) -> None:
        message = "feat(ics): write all-day events as DATE values\n\nBody text.\n"
        assert check_ascii.check_commit_message("COMMIT_EDITMSG", message.encode()) == []

    def test_characters_outside_printable_ascii_are_reported(self) -> None:
        message = "fix: a " + EM_DASH + " b\n\nTab\there " + NBSP + "\n"
        assert check_ascii.check_commit_message("MSG", message.encode("utf-8")) == [
            "MSG:1:8 U+2014 EM DASH",
            "MSG:3:4 U+0009",
            "MSG:3:10 U+00A0 NO-BREAK SPACE",
        ]

    def test_comment_lines_and_the_diff_below_the_scissors_are_not_checked(self) -> None:
        message = (
            "fix: ascii subject\n\n"
            "# Bitte geben Sie eine Commit-Beschreibung " + E_ACUTE + " ein\n"
            "# ------------------------ >8 ------------------------\n"
            "+ a line of the diff with " + ROCKET + "\n"
        )
        assert check_ascii.check_commit_message("MSG", message.encode("utf-8")) == []

    def test_comment_lines_are_checked_when_no_editor_is_used(self) -> None:
        message = (
            "fix: ascii subject\n\n#12 caf" + E_ACUTE + "\n"
            "# ------------------------ >8 ------------------------\n"
            "+ " + ROCKET + "\n"
        )
        found = check_ascii.check_commit_message("MSG", message.encode("utf-8"), editor_used=False)
        assert found == ["MSG:3:8 U+00E9 LATIN SMALL LETTER E WITH ACUTE", "MSG:5:3 U+1F680 ROCKET"]

    def test_invalid_utf8_is_reported(self) -> None:
        assert check_ascii.check_commit_message("MSG", b"caf\xe9\n") == [
            "MSG: not valid UTF-8 (byte offset 3)"
        ]

    def test_the_command_line_mode_exits_one_on_a_violation(self, tmp_path: Path) -> None:
        good = tmp_path / "good"
        good.write_text("docs: plain ascii\n", encoding="utf-8")
        bad = tmp_path / "bad"
        bad.write_text("docs: curly " + CURLY_QUOTE + "quote\n", encoding="utf-8")
        assert _run_on_message(good).returncode == 0
        result = _run_on_message(bad)
        assert result.returncode == 1
        assert f"{bad}:1:13 U+201C LEFT DOUBLE QUOTATION MARK" in result.stdout
        assert "1 violation(s) in the commit message" in result.stderr

    def test_the_command_line_mode_checks_comment_lines_without_an_editor(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "COMMIT_EDITMSG"
        path.write_text("fix: x\n\n#12 caf" + E_ACUTE + "\n", encoding="utf-8")
        assert _run_on_message(path, git_editor="vim").returncode == 0
        assert _run_on_message(path).returncode == 0
        result = _run_on_message(path, git_editor=":")
        assert result.returncode == 1
        assert f"{path}:3:8 U+00E9" in result.stdout

    def test_git_commit_with_a_message_option_runs_every_line_through_the_check(
        self, tmp_path: Path
    ) -> None:
        _git(tmp_path, "init", "--quiet")
        refused = _commit(tmp_path, "fix: x", "#12 caf" + E_ACUTE)
        assert refused.returncode != 0
        assert "U+00E9" in refused.stdout + refused.stderr
        assert _commit(tmp_path, "fix: x", "#12 cafe").returncode == 0

    def test_a_missing_message_file_exits_two(self, tmp_path: Path) -> None:
        result = _run_on_message(tmp_path / "missing")
        assert result.returncode == 2
        assert "cannot read the commit message" in result.stderr

    def test_the_commit_msg_hook_runs_this_mode(self) -> None:
        config = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        assert "entry: python3 scripts/check_ascii.py --commit-msg" in config
        assert "stages: [commit-msg]" in config
