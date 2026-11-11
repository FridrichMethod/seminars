"""Tests for the "fetch" command: polite fetching for exploration, nothing is recorded."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import respx

from fakes import FakeClock, StubResolver
from seminars.config import ENV_HTTP_CACHE_DIR, HttpSettings
from seminars.fixtures import main, scratch_file_name
from seminars.http import PoliteClient

REPO_ROOT = Path(__file__).resolve().parents[1]
HOST = "https://events.example.edu"
ROBOTS = f"{HOST}/robots.txt"
PAGE = f"{HOST}/calendar"
E_ACUTE = chr(0xE9)
SAFE_NAME = r"[a-z0-9][a-z0-9.-]*"


@pytest.fixture
def live(
    http_settings: HttpSettings, fake_clock: FakeClock, resolver: StubResolver
) -> PoliteClient:
    return PoliteClient(http_settings, clock=fake_clock, sleep=fake_clock.sleep, resolver=resolver)


def fetch(root: Path, live: PoliteClient, *arguments: str) -> int:
    return main(["--root", str(root), "fetch", *arguments], http=live)


class TestFetch:
    def test_prints_status_final_url_content_type_and_size(
        self,
        live: PoliteClient,
        tmp_path: Path,
        respx_router: respx.MockRouter,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        respx_router.get(ROBOTS).respond(404)
        body = f"<p>caf{E_ACUTE}</p>".encode("latin-1")
        respx_router.get(PAGE).respond(
            200, content=body, headers={"Content-Type": "text/html; charset=ISO-8859-1"}
        )
        assert fetch(tmp_path, live, PAGE) == 0
        captured = capsys.readouterr()
        assert captured.out == (
            f"fetched {PAGE}\n"
            "  status: 200\n"
            f"  final URL: {PAGE}\n"
            "  content type: text/html\n"
            "  bytes: 13\n"
        )
        assert captured.err == ""
        assert len(f"<p>caf{E_ACUTE}</p>\n".encode()) == 13

    def test_nothing_is_written_without_a_save_directory(
        self, live: PoliteClient, tmp_path: Path, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(ROBOTS).respond(404)
        respx_router.get(PAGE).respond(200, text="ok")
        assert fetch(tmp_path, live, PAGE) == 0
        assert list(tmp_path.iterdir()) == []

    def test_a_redirect_shows_where_the_answer_came_from(
        self,
        live: PoliteClient,
        tmp_path: Path,
        respx_router: respx.MockRouter,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        respx_router.get(ROBOTS).respond(404)
        respx_router.get(PAGE).respond(301, headers={"Location": "/calendar/upcoming"})
        respx_router.get(f"{PAGE}/upcoming").respond(200, text="{}", headers={"Content-Type": ""})
        assert fetch(tmp_path, live, PAGE) == 0
        out = capsys.readouterr().out
        assert f"fetched {PAGE}\n" in out
        assert f"  final URL: {PAGE}/upcoming\n" in out
        assert "  content type: unknown\n" in out

    def test_urls_are_fetched_one_after_another_under_the_rules_of_the_client(
        self,
        live: PoliteClient,
        tmp_path: Path,
        respx_router: respx.MockRouter,
        fake_clock: FakeClock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        robots = respx_router.get(ROBOTS).respond(
            200, text="User-agent: *\nCrawl-delay: 30\nDisallow: /private/\n"
        )
        private = respx_router.get(f"{HOST}/private/list").respond(200, text="secret")
        pages = respx_router.get(url__startswith=PAGE).respond(200, text="ok")
        urls = [f"{PAGE}?page=1", f"{HOST}/private/list", f"{PAGE}?page=2"]
        assert fetch(tmp_path, live, *urls) == 1
        captured = capsys.readouterr()
        assert captured.out.count("fetched ") == 2
        assert "disallowed by robots.txt" in captured.err
        assert (robots.call_count, private.call_count, pages.call_count) == (1, 0, 2)
        assert fake_clock.sleeps == [30.0, 30.0]
        expected = "seminars-bot/1.0 (+https://example.org/seminars/)"
        assert pages.calls.last.request.headers["user-agent"] == expected

    def test_a_url_that_fails_does_not_stop_the_others(
        self,
        live: PoliteClient,
        tmp_path: Path,
        respx_router: respx.MockRouter,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        respx_router.get(ROBOTS).respond(404)
        respx_router.get(f"{HOST}/gone").respond(404)
        respx_router.get(f"{HOST}/logo.png").respond(
            200, content=b"\x89PNG", headers={"Content-Type": "image/png"}
        )
        respx_router.get(PAGE).respond(200, text="ok")
        assert fetch(tmp_path, live, f"{HOST}/gone", f"{HOST}/logo.png", "ftp://x/y", PAGE) == 1
        captured = capsys.readouterr()
        assert captured.err.count("error: ") == 3
        assert "error: HTTP 404" in captured.err
        assert "refusing binary content type image/png" in captured.err
        assert captured.out.startswith(f"fetched {PAGE}\n")

    def test_allowed_statuses_count_as_fetched(
        self,
        live: PoliteClient,
        tmp_path: Path,
        respx_router: respx.MockRouter,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        respx_router.get(ROBOTS).respond(404)
        respx_router.get(f"{HOST}/gone").respond(404, text="not here")
        respx_router.get(f"{HOST}/teapot").respond(418)
        arguments = ["--allow-status", "404", "--allow-status", "418"]
        assert fetch(tmp_path, live, f"{HOST}/gone", f"{HOST}/teapot", *arguments) == 0
        out = capsys.readouterr().out
        assert "  status: 404\n" in out
        assert "  status: 418\n" in out

    def test_non_ascii_urls_are_printed_as_ascii(
        self,
        live: PoliteClient,
        tmp_path: Path,
        respx_router: respx.MockRouter,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        respx_router.get(ROBOTS).respond(404)
        respx_router.get(url__startswith=f"{HOST}/caf").respond(200, text="ok")
        assert fetch(tmp_path, live, f"{HOST}/caf{E_ACUTE}") == 0
        out = capsys.readouterr().out
        assert out.isascii()
        assert f"fetched {HOST}/caf\\xe9\n" in out


NOT_SCRATCH = ["data", "data/llm_cache", "public", "public/feeds", "config", "config/sources.d"]
NOT_SCRATCH += [".git/hooks", ".github/workflows", "src/seminars", "tests", "scratch", ".", ""]
NOT_SCRATCH += [".scratch/../data", ".scratchy", "locales", ".cache/http"]
"""Places inside a project that are not its scratch directory."""


class TestSaveDirectory:
    def test_the_decoded_body_is_written_and_the_path_is_printed(
        self,
        live: PoliteClient,
        tmp_path: Path,
        respx_router: respx.MockRouter,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        elsewhere = tmp_path / "some" / "where"
        elsewhere.mkdir(parents=True)
        monkeypatch.chdir(elsewhere)
        respx_router.get(ROBOTS).respond(404)
        respx_router.get(PAGE).respond(
            200,
            content=f"<p>caf{E_ACUTE}</p>\r\n".encode("latin-1"),
            headers={"Content-Type": "text/html; charset=ISO-8859-1"},
        )
        assert fetch(tmp_path, live, PAGE, "--save-dir", ".scratch/chemistry") == 0
        directory = tmp_path / ".scratch" / "chemistry"
        (saved,) = directory.iterdir()
        assert saved.read_bytes() == f"<p>caf{E_ACUTE}</p>\n".encode()
        assert re.fullmatch(r"events-example-edu-calendar-[0-9a-f]{8}\.html", saved.name)
        assert f"  saved: {saved}\n" in capsys.readouterr().out
        assert not (elsewhere / ".scratch").exists()

    def test_no_fixture_and_no_manifest_is_written(
        self, live: PoliteClient, tmp_path: Path, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(ROBOTS).respond(404)
        respx_router.get(PAGE).respond(200, text="ok")
        assert fetch(tmp_path, live, PAGE, "--save-dir", ".scratch/x") == 0
        assert sorted(path.name for path in tmp_path.iterdir()) == [".scratch"]
        assert list(tmp_path.rglob("manifest.json")) == []

    def test_an_absolute_directory_is_used_as_it_is(
        self, live: PoliteClient, tmp_path: Path, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(ROBOTS).respond(404)
        respx_router.get(PAGE).respond(200, text="ok")
        target = tmp_path / "outside" / "notes"
        assert fetch(tmp_path / "project", live, PAGE, "--save-dir", str(target)) == 0
        assert len(list(target.iterdir())) == 1
        assert not (tmp_path / "project").exists()

    def test_fetching_a_url_again_replaces_its_file(
        self, live: PoliteClient, tmp_path: Path, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(ROBOTS).respond(404)
        respx_router.get(PAGE).mock(
            side_effect=[respx.MockResponse(200, text="old"), respx.MockResponse(200, text="new")]
        )
        for _ in range(2):
            assert fetch(tmp_path, live, PAGE, "--save-dir", ".scratch") == 0
        (saved,) = (tmp_path / ".scratch").iterdir()
        assert saved.read_text(encoding="utf-8") == "new\n"

    @pytest.mark.parametrize(
        "url",
        [
            f"{HOST}/../../../etc/passwd",
            f"{HOST}/a/%2e%2e/%2e%2e/escape",
            f"{HOST}/..%2f..%2fescape.html",
            f"{HOST}/list?file=../../secret&x=/abs/path",
            f"{HOST}/.hidden/.git/config",
            f"{HOST}/C:%5Cwindows%5Cwin.ini",
            f"{HOST}/" + "long-segment/" * 40,
            f"{HOST}/",
            "http://[2606:4700:4700::1111]/status",
            "https://---/x",
            "https:///no-host",
        ],
    )
    def test_file_names_are_safe_whatever_the_url_looks_like(self, url: str) -> None:
        for content_type in ("text/html", "application/json", None, "x/../y"):
            name = scratch_file_name(url, content_type)
            assert re.fullmatch(SAFE_NAME, name), name
            assert ".." not in name
            assert len(name) <= 100
            assert name == scratch_file_name(url, content_type)

    def test_file_names_tell_hosts_and_urls_apart(self) -> None:
        names = {
            scratch_file_name(url, "text/html")
            for url in (
                f"{HOST}/events",
                f"{HOST}/events?page=2",
                f"{HOST}/Events",
                "https://www.example.edu/events",
                "http://events.example.edu/events",
            )
        }
        assert len(names) == 5
        assert scratch_file_name("https://Chemistry.Stanford.EDU/events/", "text/calendar") == (
            scratch_file_name("https://Chemistry.Stanford.EDU/events/", "text/calendar")
        )
        assert scratch_file_name(f"{HOST}/a/b?x=1", "application/json").startswith(
            "events-example-edu-a-b-"
        )
        assert scratch_file_name(f"{HOST}/a/b?x=1", "application/json").endswith(".json")

    def test_every_file_lands_directly_in_the_directory(
        self, live: PoliteClient, tmp_path: Path, respx_router: respx.MockRouter
    ) -> None:
        respx_router.get(ROBOTS).respond(404)
        respx_router.get(url__startswith=HOST).respond(200, text="ok")
        urls = [f"{HOST}/a/%2e%2e/%2e%2e/escape", f"{HOST}/list?file=../../secret", f"{HOST}/x/y/z"]
        assert fetch(tmp_path / "root", live, *urls, "--save-dir", ".scratch/pages") == 0
        directory = tmp_path / "root" / ".scratch" / "pages"
        assert len(list(directory.iterdir())) == 3
        assert all(path.is_file() for path in directory.iterdir())
        assert sorted(path.name for path in tmp_path.iterdir()) == ["root"]
        assert sorted(path.name for path in (tmp_path / "root").iterdir()) == [".scratch"]
        assert sorted(path.name for path in (tmp_path / "root" / ".scratch").iterdir()) == ["pages"]

    @pytest.mark.parametrize(
        "directory", ["tests/fixtures", "tests/fixtures/sites-x", "tests/fixtures/_probe/deep"]
    )
    def test_a_directory_inside_the_fixtures_is_refused_before_any_request(
        self,
        live: PoliteClient,
        tmp_path: Path,
        respx_router: respx.MockRouter,
        capsys: pytest.CaptureFixture[str],
        directory: str,
    ) -> None:
        for spelling in (directory, str(tmp_path / directory), f"data/../{directory}"):
            with pytest.raises(SystemExit) as caught:
                fetch(tmp_path, live, PAGE, "--save-dir", spelling)
            assert caught.value.code == 2
            assert "tests/fixtures is for recorded fixtures" in capsys.readouterr().err
        assert respx_router.calls.call_count == 0
        assert list(tmp_path.iterdir()) == []

    def test_a_symlink_into_the_fixtures_is_refused_as_well(
        self, live: PoliteClient, tmp_path: Path, respx_router: respx.MockRouter
    ) -> None:
        (tmp_path / "tests" / "fixtures" / "sites-x").mkdir(parents=True)
        (tmp_path / "outside").mkdir()
        (tmp_path / "outside" / "shortcut").symlink_to(tmp_path / "tests" / "fixtures" / "sites-x")
        (tmp_path / ".scratch").symlink_to(tmp_path / "tests" / "fixtures")
        for directory in (str(tmp_path / "outside" / "shortcut" / "pages"), ".scratch/sites-x"):
            with pytest.raises(SystemExit) as caught:
                fetch(tmp_path, live, PAGE, "--save-dir", directory)
            assert caught.value.code == 2
        assert respx_router.calls.call_count == 0

    @pytest.mark.parametrize("directory", NOT_SCRATCH)
    def test_inside_the_project_only_the_scratch_directory_is_accepted(
        self,
        live: PoliteClient,
        tmp_path: Path,
        respx_router: respx.MockRouter,
        capsys: pytest.CaptureFixture[str],
        directory: str,
    ) -> None:
        """A body is whatever a site sent; it does not belong among committed or published files."""
        for spelling in (directory, str(tmp_path / directory)):
            if not spelling:
                continue
            with pytest.raises(SystemExit) as caught:
                fetch(tmp_path, live, PAGE, "--save-dir", spelling)
            assert caught.value.code == 2
            assert "inside the project only .scratch" in capsys.readouterr().err
        assert respx_router.calls.call_count == 0
        assert list(tmp_path.iterdir()) == []

    def test_a_scratch_directory_that_points_into_the_project_is_refused(
        self, live: PoliteClient, tmp_path: Path, respx_router: respx.MockRouter
    ) -> None:
        (tmp_path / "data").mkdir()
        (tmp_path / ".scratch").symlink_to(tmp_path / "data")
        with pytest.raises(SystemExit) as caught:
            fetch(tmp_path, live, PAGE, "--save-dir", ".scratch/topic")
        assert caught.value.code == 2
        assert respx_router.calls.call_count == 0
        assert list((tmp_path / "data").iterdir()) == []

    @pytest.mark.parametrize("directory", [".scratch", ".scratch/topic", ".scratch/a/b/c"])
    def test_the_scratch_directory_and_everything_below_it_is_accepted(
        self, live: PoliteClient, tmp_path: Path, respx_router: respx.MockRouter, directory: str
    ) -> None:
        respx_router.get(ROBOTS).respond(404)
        respx_router.get(PAGE).respond(200, text="ok")
        assert fetch(tmp_path, live, PAGE, "--save-dir", directory) == 0
        assert len(list((tmp_path / directory).iterdir())) == 1

    def test_a_file_that_cannot_be_written_is_an_error_for_that_url_only(
        self,
        live: PoliteClient,
        tmp_path: Path,
        respx_router: respx.MockRouter,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        respx_router.get(ROBOTS).respond(404)
        respx_router.get(url__startswith=PAGE).respond(200, text="ok")
        (tmp_path / ".scratch").mkdir()
        (tmp_path / ".scratch" / "blocked").write_text(
            "a file, not a directory\n", encoding="utf-8"
        )
        arguments = [f"{PAGE}?a=1", f"{PAGE}?a=2", "--save-dir", ".scratch/blocked"]
        assert fetch(tmp_path, live, *arguments) == 1
        captured = capsys.readouterr()
        assert captured.err.count("error: cannot save") == 2
        assert captured.out.count("fetched ") == 2
        assert "saved:" not in captured.out


class TestLiveClientOfTheCommand:
    def test_fetch_builds_the_same_client_as_record(
        self,
        tmp_path: Path,
        respx_router: respx.MockRouter,
        monkeypatch: pytest.MonkeyPatch,
        resolver: StubResolver,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        settings = (REPO_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
        settings = settings.replace("min_interval_seconds: 1.0", "min_interval_seconds: 0")
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "settings.yaml").write_text(settings, encoding="utf-8")
        monkeypatch.delenv(ENV_HTTP_CACHE_DIR, raising=False)
        robots = respx_router.get(ROBOTS).respond(404)
        respx_router.get(url__startswith=PAGE).respond(200, text="ok")
        arguments = ["--root", str(tmp_path), "fetch", f"{PAGE}?a=1", f"{PAGE}?a=2"]
        assert main(arguments, resolver=resolver) == 0
        captured = capsys.readouterr()
        assert captured.err == f"HTTP cache directory: {tmp_path / '.cache' / 'http'}\n"
        assert captured.out.count("fetched ") == 2
        expected = "seminars-bot/1.0 (+https://fridrichmethod.github.io/seminars/)"
        assert robots.calls.last.request.headers["user-agent"] == expected
        assert robots.call_count == 1
        assert (tmp_path / ".cache" / "http" / "hosts" / "events.example.edu.json").is_file()
        assert not (tmp_path / "tests").exists()
        assert set(resolver.calls) == {"events.example.edu"}


class TestRepository:
    def test_the_scratch_directory_is_ignored_by_git(self) -> None:
        lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert "/.scratch/" in lines

    def test_the_readme_documents_the_command_and_the_rule(self) -> None:
        readme = (REPO_ROOT / "tests" / "fixtures" / "README.md").read_text(encoding="utf-8")
        assert "python -m seminars.fixtures fetch" in readme
        assert "--save-dir .scratch/" in readme
        assert "curl" in readme
