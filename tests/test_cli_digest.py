"""``seminars digest --dry-run``: the bilingual markdown, printed whatever stdout is.

The dry run is the one command whose output is not ASCII: it prints the digest as
it would be posted, with its Chinese headings. It writes that text as UTF-8 also
when stdout is a pipe or a file in another encoding (cp1252 for a redirected stdout
on Windows, for example), and it names the issue title in a line of its own, so the
document keeps a single top-level heading.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from seminars import cli
from seminars.models import DigestDocument, DigestOutcome
from seminars.pipeline import services as wiring

REPO_ROOT = Path(__file__).resolve().parents[1]
CHINESE_HEADING = "# Seminars - week of 2026-11-16 / \u5b66\u672f\u62a5\u544a"
BODY = f"{CHINESE_HEADING}\n\nZo\u00e9 M\u00fcller \u0141ukasz\n"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch project with the repository's settings and locales and no data."""
    root = tmp_path / "project"
    (root / "config" / "sources.d").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "config" / "settings.yaml", root / "config" / "settings.yaml")
    shutil.copytree(REPO_ROOT / "locales", root / "locales")
    for name in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_REPOSITORY_OWNER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def fake_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """The digest publisher answers with a fixed dry-run document."""
    week = date(2026, 11, 16)
    document = DigestDocument(week_start=week, title="Seminars - week of X", body=BODY)
    real_load = wiring.load

    def load(key: str) -> object:
        if key == "digest_publish":
            return lambda *args, **kwargs: DigestOutcome(action="dry_run", document=document)
        return real_load(key)

    monkeypatch.setattr(wiring, "load", load)


@pytest.mark.usefixtures("project", "fake_digest")
class TestDryRunOutput:
    @pytest.mark.parametrize("charset", ["utf-8", "cp1252", "ascii", "latin-1"])
    def test_the_markdown_is_utf8_whatever_stdout_is(self, charset: str) -> None:
        result = CliRunner(charset=charset).invoke(cli.app, ["digest", "--dry-run"])
        assert result.exit_code == 0, result.stderr
        assert result.stdout_bytes.decode("utf-8") == f"Issue title: Seminars - week of X\n\n{BODY}"

    def test_the_title_is_no_second_heading(self) -> None:
        result = CliRunner().invoke(cli.app, ["digest", "--dry-run"])
        lines = result.stdout_bytes.decode("utf-8").splitlines()
        assert lines[0] == "Issue title: Seminars - week of X"
        assert [line for line in lines if line.startswith("# ")] == [CHINESE_HEADING]

    def test_the_help_names_the_exception(self) -> None:
        result = CliRunner().invoke(cli.app, ["digest", "--help"])
        assert result.exit_code == 0
        assert "UTF-8" in result.output


@pytest.mark.usefixtures("project")
def test_a_real_dry_run_into_a_cp1252_pipe(project: Path) -> None:
    """The command as a user runs it, with the stdout of a redirected Windows shell."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(project),
        "PYTHONIOENCODING": "cp1252",
        "SEMINARS_HTTP_CACHE_DIR": str(project / "http-cache"),
    }
    command = [sys.executable, "-c", "from seminars.cli import app; app()", "digest", "--dry-run"]
    done = subprocess.run(command, cwd=project, env=env, capture_output=True, timeout=120)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    text = done.stdout.decode("utf-8")
    assert text.startswith("Issue title: Seminars - week of ")
    assert "\n# Seminars - week of " in text
    assert "\u7cbe\u9009" in text  # the Chinese of the picks heading
