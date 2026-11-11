"""Tests for the names of fixture files: data extensions only, and pytest stays out.

A recorded body is whatever a site sent, so it must never be a file that
something executes. pytest imports every ``conftest.py`` and ``test_*.py``
it finds on its way through tests/. Three things keep fetched content from
being run: the recorder only writes files with a data extension, ``check``
reports every other file in a fixture directory, and pytest does not look
below tests/fixtures at all.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from factories import REFERENCE_NOW, make_document
from fakes import FakeHttpClient
from seminars.fixtures import (
    FIXTURE_EXTENSIONS,
    MANIFEST_NAME,
    FixtureEntry,
    FixtureError,
    FixtureStore,
    check_tree,
    default_file_name,
    is_fixture_file_name,
    main,
    offline_client,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGE = "https://events.example.edu/calendar"

GOOD_NAMES = [
    "page.json",
    "feed.xml",
    "calendar.ics",
    "index.html",
    "plain.txt",
    "notes.md",
    "Page-1_of.2.json",
    "0.txt",
]
BAD_NAMES = [
    "conftest.py",
    "test_events.py",
    "page.PY",
    "page.pyc",
    "page.html.py",
    "page.sh",
    "page.js",
    "page.htm",
    "page.yaml",
    "page.HTML",
    "page.Json",
    "page",
    "page.",
    "json",
    ".json",
    ".hidden.json",
    "-page.json",
    "page.json ",
    "page.json\n",
    "pa ge.json",
    "dir/page.json",
    "..\\page.json",
    "page.json/",
    "caf" + chr(0xE9) + ".json",
    "",
]


@pytest.fixture
def store(tmp_path: Path) -> FixtureStore:
    return FixtureStore(tmp_path / "tests" / "fixtures" / "sites-a")


class TestNames:
    def test_the_extensions_are_those_the_recorder_derives_and_the_one_of_notes(self) -> None:
        assert FIXTURE_EXTENSIONS == (".html", ".ics", ".json", ".md", ".txt", ".xml")
        content_types = ["application/json", "application/rss+xml", "text/calendar", "text/html"]
        content_types += ["text/plain", "application/xhtml+xml", "image/png", "odd/type", None]
        derived = {Path(default_file_name(PAGE, value)).suffix for value in content_types}
        assert derived == set(FIXTURE_EXTENSIONS) - {".md"}

    @pytest.mark.parametrize("name", GOOD_NAMES)
    def test_a_plain_name_with_a_data_extension_is_a_fixture_file_name(self, name: str) -> None:
        assert is_fixture_file_name(name)
        assert FixtureEntry(file=name, fetched_at=REFERENCE_NOW).file == name

    @pytest.mark.parametrize("name", BAD_NAMES)
    def test_no_other_name_is(self, name: str) -> None:
        assert not is_fixture_file_name(name)
        with pytest.raises(ValidationError):
            FixtureEntry(file=name, fetched_at=REFERENCE_NOW)

    @pytest.mark.parametrize("name", GOOD_NAMES)
    def test_the_store_takes_a_body_under_such_a_name(self, store: FixtureStore, name: str) -> None:
        assert store.add(make_document(PAGE, "body"), name=name).file == name
        assert (store.directory / name).read_text(encoding="utf-8") == "body\n"

    @pytest.mark.parametrize("name", [name for name in BAD_NAMES if name])
    def test_the_store_refuses_any_other_name_and_writes_nothing(
        self, store: FixtureStore, name: str
    ) -> None:
        with pytest.raises(FixtureError, match="is not a fixture file name"):
            store.add(make_document(PAGE, "print('run me')"), name=name)
        assert not store.directory.exists()

    def test_a_manifest_that_lists_such_a_file_is_not_loaded(self, store: FixtureStore) -> None:
        store.add(make_document(PAGE, "body"), name="page.html")
        manifest = store.directory / MANIFEST_NAME
        text = manifest.read_text(encoding="utf-8")
        manifest.write_text(text.replace("page.html", "conftest.py"), encoding="utf-8")
        with pytest.raises(FixtureError, match="invalid fixture manifest"):
            FixtureStore(store.directory)
        with pytest.raises(FixtureError, match="invalid fixture manifest"):
            offline_client("sites-a", fixtures_dir=store.directory.parent)


class TestCheck:
    def test_files_with_another_extension_are_reported(self, store: FixtureStore) -> None:
        store.add(make_document(PAGE, "body"), name="page.html")
        (store.directory / "README.md").write_text("notes\n", encoding="utf-8")
        strays = ["conftest.py", "test_x.py", "README", "README.py", "page.JSON", ".hidden"]
        for name in strays:
            (store.directory / name).write_text("print('run me')\n", encoding="utf-8")
        (store.directory / "__pycache__").mkdir()
        (store.directory / "extra.txt").write_text("orphan\n", encoding="utf-8")
        only = "only .html, .ics, .json, .md, .txt and .xml files belong here"
        assert check_tree(store.directory.parent) == [
            f"sites-a: .hidden is not a fixture file: {only}",
            f"sites-a: README is not a fixture file: {only}",
            f"sites-a: README.py is not a fixture file: {only}",
            f"sites-a: __pycache__ is not a fixture file: {only}",
            f"sites-a: conftest.py is not a fixture file: {only}",
            "sites-a: extra.txt is not listed in the manifest",
            f"sites-a: page.JSON is not a fixture file: {only}",
            f"sites-a: test_x.py is not a fixture file: {only}",
        ]

    def test_the_fixtures_of_this_repository_are_data_files_only(self) -> None:
        fixtures = REPO_ROOT / "tests" / "fixtures"
        files = [path for path in fixtures.rglob("*") if path.is_file()]
        assert files
        assert [path for path in files if path.suffix not in FIXTURE_EXTENSIONS] == []


class TestCommandLine:
    @pytest.mark.parametrize("name", ["conftest.py", "test_x.py", "page.PY", "../x.json", "page"])
    def test_record_refuses_such_a_name_before_it_fetches_anything(
        self,
        fake_http: FakeHttpClient,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        name: str,
    ) -> None:
        fake_http.add(PAGE, "print('run me')")
        arguments = ["--root", str(tmp_path), "record", "--source-id", "sites-a", PAGE]
        with pytest.raises(SystemExit) as caught:
            main([*arguments, "--name", name], http=fake_http)
        assert caught.value.code == 2
        assert "is not a fixture file name" in capsys.readouterr().err
        assert fake_http.requests == []
        assert list(tmp_path.iterdir()) == []

    def test_record_takes_a_name_with_a_data_extension(
        self, fake_http: FakeHttpClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_http.add(PAGE, "<p>x</p>")
        arguments = ["--root", str(tmp_path), "record", "--source-id", "sites-a", PAGE]
        assert main([*arguments, "--name", "calendar.html"], http=fake_http) == 0
        assert capsys.readouterr().out == "recorded sites-a/calendar.html (HTTP 200, 9 bytes)\n"


DOCTEST_BODY = """A page that a site sent as text/plain.

>>> from pathlib import Path
>>> Path({marker!r}).write_text("x")
1
"""


class TestPytestRunsNoRecordedText:
    """A body named ``test*.txt`` is no doctest, however pytest is pointed at it."""

    def test_the_doctest_plugin_is_switched_off(self) -> None:
        options = tomllib.loads(pytest_options())["tool"]["pytest"]["ini_options"]
        addopts = options["addopts"]
        assert addopts[addopts.index("no:doctest") - 1] == "-p"

    @pytest.mark.parametrize(
        "argument",
        [
            "tests/fixtures/some-source",
            "tests/fixtures/some-source/tests-data-1a2b3c4d.txt",
            "tests/fixtures/some-source/test_notes.txt",
        ],
    )
    def test_a_recorded_text_file_is_not_run_as_a_doctest(
        self, tmp_path: Path, argument: str
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(pytest_options(), encoding="utf-8")
        directory = tmp_path / "tests" / "fixtures" / "some-source"
        directory.mkdir(parents=True)
        marker = tmp_path / "doctest.ran"
        body = DOCTEST_BODY.format(marker=str(marker))
        for name in ("tests-data-1a2b3c4d.txt", "test_notes.txt"):
            assert is_fixture_file_name(name)
            (directory / name).write_text(body, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", argument],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        output = result.stdout + result.stderr
        assert not marker.exists(), output
        assert " passed" not in output, output


def pytest_options() -> str:
    """The ``[tool.pytest.ini_options]`` table of this repository, as it is written."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    found = re.search(r"^\[tool\.pytest\.ini_options\]\n.*?(?=^\[)", text, re.DOTALL | re.MULTILINE)
    assert found is not None
    return found.group()


class TestPytestStaysOut:
    def test_the_option_names_the_fixtures_and_keeps_the_defaults_of_pytest(self) -> None:
        options = tomllib.loads(pytest_options())["tool"]["pytest"]["ini_options"]
        patterns = options["norecursedirs"]
        assert {"tests/fixtures", "tests/fixtures/*"} <= set(patterns)
        defaults = {"*.egg", ".*", "_darcs", "build", "CVS", "dist", "node_modules", "venv"}
        assert defaults | {"{arch}"} <= set(patterns)

    @pytest.mark.parametrize("arguments", [[], ["."], ["tests/fixtures"]])
    def test_python_files_below_the_fixtures_are_neither_collected_nor_imported(
        self, tmp_path: Path, arguments: list[str]
    ) -> None:
        """A scratch project with the pytest options of this repository and hostile fixtures."""
        (tmp_path / "pyproject.toml").write_text(pytest_options(), encoding="utf-8")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        marker = "from pathlib import Path\nPath(__file__).with_suffix('.ran').write_text('x')\n"
        for directory in ("fixtures", "fixtures/some-source", "fixtures/some-source/deeper"):
            (tests / directory).mkdir()
            (tests / directory / "conftest.py").write_text(marker, encoding="utf-8")
            (tests / directory / "test_bad.py").write_text(
                marker + "def test_bad():\n    raise AssertionError('collected')\n",
                encoding="utf-8",
            )
        result = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", *arguments],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        ran = sorted(path.relative_to(tests).as_posix() for path in tests.rglob("*.ran"))
        output = result.stdout + result.stderr
        if arguments == ["tests/fixtures"]:
            # A directory that is named on the command line is entered; nothing below it is.
            assert ran == ["fixtures/conftest.ran", "fixtures/test_bad.ran"], output
            assert "1 failed" in output
        else:
            assert ran == [], output
            assert result.returncode == 0, output
            assert "1 passed" in output
