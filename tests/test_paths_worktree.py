"""Tests for the default HTTP cache directory, which linked git worktrees share.

The repository layouts are built from plain files in ``tmp_path``, the way
``git worktree add`` leaves them on disk; git itself is never run.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
import respx

from fakes import FakeClock, StubResolver
from seminars.config import ENV_HTTP_CACHE_DIR, HttpSettings, ProjectPaths, main_checkout
from seminars.fixtures import main as fixtures_main
from seminars.http import PoliteClient

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGE = "https://dept.example.edu/events"


def make_checkout(root: Path) -> Path:
    """An ordinary checkout: ``.git`` is a directory."""
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


def add_worktree(
    main: Path,
    name: str,
    *,
    location: Path | None = None,
    relative_pointer: bool = False,
    commondir: str = "../..",
) -> Path:
    """A linked worktree of ``main``: a ``.git`` FILE that points into ``main/.git/worktrees``."""
    worktree = location or main / ".worktrees" / name
    admin = main / ".git" / "worktrees" / name
    admin.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (admin / "commondir").write_text(commondir + "\n", encoding="utf-8")
    (admin / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    (admin / "HEAD").write_text(f"ref: refs/heads/wt/{name}\n", encoding="utf-8")
    pointer = os.path.relpath(admin, worktree) if relative_pointer else str(admin)
    (worktree / ".git").write_text(f"gitdir: {pointer}\n", encoding="utf-8")
    return worktree


@pytest.fixture
def main(tmp_path: Path) -> Path:
    return make_checkout(tmp_path / "seminars")


class TestDefaultDirectory:
    def test_an_ordinary_checkout_uses_its_own_directory(self, main: Path) -> None:
        assert ProjectPaths(main).http_cache_dir({}) == main / ".cache" / "http"

    def test_a_tree_without_git_uses_its_own_directory(self, tmp_path: Path) -> None:
        assert ProjectPaths(tmp_path).http_cache_dir({}) == tmp_path / ".cache" / "http"

    def test_a_linked_worktree_uses_the_directory_of_the_main_checkout(self, main: Path) -> None:
        worktree = add_worktree(main, "N1-sites")
        assert ProjectPaths(worktree).http_cache_dir({}) == main / ".cache" / "http"
        assert ProjectPaths(worktree).cache_dir == worktree / ".cache"

    def test_all_worktrees_and_the_main_checkout_agree(self, main: Path, tmp_path: Path) -> None:
        inside = add_worktree(main, "N1-sites")
        other = add_worktree(main, "N1-custom")
        outside = add_worktree(main, "elsewhere", location=tmp_path / "far" / "away")
        directories = {
            ProjectPaths(root).http_cache_dir({}) for root in (main, inside, other, outside)
        }
        assert directories == {main / ".cache" / "http"}

    def test_a_relative_pointer_is_followed(self, main: Path) -> None:
        worktree = add_worktree(main, "N1-sites", relative_pointer=True)
        pointer = (worktree / ".git").read_text(encoding="utf-8")
        assert pointer == "gitdir: ../../.git/worktrees/N1-sites\n"
        assert ProjectPaths(worktree).http_cache_dir({}) == main / ".cache" / "http"

    def test_an_absolute_commondir_is_followed(self, main: Path) -> None:
        worktree = add_worktree(main, "N1-sites", commondir=str(main / ".git"))
        assert ProjectPaths(worktree).http_cache_dir({}) == main / ".cache" / "http"

    def test_line_endings_and_spaces_around_the_paths_do_not_matter(self, main: Path) -> None:
        worktree = add_worktree(main, "N1-sites")
        admin = main / ".git" / "worktrees" / "N1-sites"
        (worktree / ".git").write_bytes(f"gitdir:   {admin}  \r\n".encode())
        (admin / "commondir").write_bytes(b"  ../..\r\n")
        assert ProjectPaths(worktree).http_cache_dir({}) == main / ".cache" / "http"

    def test_the_main_checkout_is_found_through_a_symlinked_path(
        self, main: Path, tmp_path: Path
    ) -> None:
        worktree = add_worktree(main, "N1-sites")
        link = tmp_path / "link"
        link.symlink_to(worktree, target_is_directory=True)
        assert ProjectPaths(link).http_cache_dir({}) == main / ".cache" / "http"

    def test_this_repository(self) -> None:
        """Whatever kind of checkout the tests run in, the answer is a .cache/http directory."""
        directory = ProjectPaths(REPO_ROOT).http_cache_dir({})
        assert directory.parts[-2:] == (".cache", "http")
        if (REPO_ROOT / ".git").is_dir():
            assert directory == REPO_ROOT / ".cache" / "http"


class TestMainCheckout:
    def test_only_a_linked_worktree_has_one(self, main: Path, tmp_path: Path) -> None:
        worktree = add_worktree(main, "N1-sites")
        assert main_checkout(worktree) == main
        assert main_checkout(main) is None
        assert main_checkout(tmp_path) is None
        assert main_checkout(tmp_path / "missing") is None


class TestEnvironmentOverride:
    def test_the_variable_wins_over_the_worktree_default(self, main: Path, tmp_path: Path) -> None:
        worktree = add_worktree(main, "N1-sites")
        paths = ProjectPaths(worktree)
        shared = tmp_path / "shared-http-cache"
        assert paths.http_cache_dir({ENV_HTTP_CACHE_DIR: str(shared)}) == shared
        assert paths.http_cache_dir({ENV_HTTP_CACHE_DIR: "mine"}) == worktree / "mine"
        assert paths.http_cache_dir({ENV_HTTP_CACHE_DIR: ""}) == main / ".cache" / "http"

    def test_the_process_environment_is_the_default(
        self, main: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = add_worktree(main, "N1-sites")
        monkeypatch.setenv(ENV_HTTP_CACHE_DIR, str(tmp_path / "shared"))
        assert ProjectPaths(worktree).http_cache_dir() == tmp_path / "shared"
        monkeypatch.delenv(ENV_HTTP_CACHE_DIR)
        assert ProjectPaths(worktree).http_cache_dir() == main / ".cache" / "http"


def empty_pointer(worktree: Path, main: Path) -> None:
    (worktree / ".git").write_text("", encoding="utf-8")


def pointer_without_keyword(worktree: Path, main: Path) -> None:
    (worktree / ".git").write_text(str(main / ".git" / "worktrees" / "wt") + "\n", encoding="utf-8")


def pointer_without_path(worktree: Path, main: Path) -> None:
    (worktree / ".git").write_text("gitdir:\n", encoding="utf-8")


def pointer_that_is_not_text(worktree: Path, main: Path) -> None:
    (worktree / ".git").write_bytes(b"gitdir: \xff\xfe\x00\n")


def pointer_with_a_nul_byte(worktree: Path, main: Path) -> None:
    (worktree / ".git").write_bytes(b"gitdir: /tmp/a\x00b\n")


def pointer_to_a_missing_directory(worktree: Path, main: Path) -> None:
    (worktree / ".git").write_text(f"gitdir: {main / '.git' / 'gone'}\n", encoding="utf-8")


def missing_commondir(worktree: Path, main: Path) -> None:
    """What a git submodule looks like: a pointer, but no ``commondir`` behind it."""
    (main / ".git" / "worktrees" / "wt" / "commondir").unlink()


def empty_commondir(worktree: Path, main: Path) -> None:
    (main / ".git" / "worktrees" / "wt" / "commondir").write_text("\n", encoding="utf-8")


def commondir_that_is_a_directory(worktree: Path, main: Path) -> None:
    commondir = main / ".git" / "worktrees" / "wt" / "commondir"
    commondir.unlink()
    commondir.mkdir()


def commondir_to_a_missing_directory(worktree: Path, main: Path) -> None:
    (main / ".git" / "worktrees" / "wt" / "commondir").write_text(
        "../../../nowhere/.git\n", encoding="utf-8"
    )


def commondir_of_a_bare_repository(worktree: Path, main: Path) -> None:
    """A repository without a main checkout: its directory is not called ``.git``."""
    (main / ".git" / "worktrees" / "wt" / "commondir").write_text("..\n", encoding="utf-8")


def unreadable_pointer(worktree: Path, main: Path) -> None:
    (worktree / ".git").chmod(0o000)
    if os.access(worktree / ".git", os.R_OK):
        pytest.skip("this user can read files without permission")


BREAKAGES: list[Callable[[Path, Path], None]] = [
    empty_pointer,
    pointer_without_keyword,
    pointer_without_path,
    pointer_that_is_not_text,
    pointer_with_a_nul_byte,
    pointer_to_a_missing_directory,
    missing_commondir,
    empty_commondir,
    commondir_that_is_a_directory,
    commondir_to_a_missing_directory,
    commondir_of_a_bare_repository,
    unreadable_pointer,
]


class TestAnythingUnexpectedFallsBack:
    @pytest.mark.parametrize("breakage", BREAKAGES, ids=lambda function: function.__name__)
    def test_the_directory_of_the_tree_itself_is_used_and_nothing_is_raised(
        self, main: Path, breakage: Callable[[Path, Path], None]
    ) -> None:
        worktree = add_worktree(main, "wt")
        assert ProjectPaths(worktree).http_cache_dir({}) == main / ".cache" / "http"
        breakage(worktree, main)
        try:
            assert ProjectPaths(worktree).http_cache_dir({}) == worktree / ".cache" / "http"
        finally:
            (worktree / ".git").chmod(0o644)

    def test_a_symlink_loop_falls_back(self, main: Path) -> None:
        worktree = add_worktree(main, "wt")
        admin = main / ".git" / "worktrees" / "wt"
        (admin / "loop").symlink_to(admin / "loop")
        (admin / "commondir").write_text("loop/.git\n", encoding="utf-8")
        assert ProjectPaths(worktree).http_cache_dir({}) == worktree / ".cache" / "http"


class TestCrawlDelayBetweenWorktrees:
    def test_clients_of_two_worktrees_wait_for_each_other_by_default(
        self,
        main: Path,
        http_settings: HttpSettings,
        fake_clock: FakeClock,
        respx_router: respx.MockRouter,
    ) -> None:
        respx_router.get("https://dept.example.edu/robots.txt").respond(
            200, text="User-agent: *\nCrawl-delay: 30\n"
        )
        respx_router.get(PAGE).respond(200, text="ok")
        for name in ("N1-sites", "N1-custom"):
            paths = ProjectPaths(add_worktree(main, name))
            client = PoliteClient(
                http_settings,
                cache_dir=paths.http_cache_dir({}),
                clock=fake_clock,
                sleep=fake_clock.sleep,
                resolver=StubResolver(),
            )
            client.get(PAGE)
            fake_clock.advance(10.0)
        assert fake_clock.sleeps == [30.0, 20.0]
        assert (main / ".cache" / "http" / "hosts" / "dept.example.edu.json").is_file()
        assert not (main / ".worktrees" / "N1-sites" / ".cache").exists()
        assert not (main / ".worktrees" / "N1-custom" / ".cache").exists()


class TestRecorderInAWorktree:
    def test_the_recorder_uses_and_names_the_directory_of_the_main_checkout(
        self,
        main: Path,
        respx_router: respx.MockRouter,
        resolver: StubResolver,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv(ENV_HTTP_CACHE_DIR, raising=False)
        worktree = add_worktree(main, "N1-sites")
        settings = (REPO_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
        settings = settings.replace("min_interval_seconds: 1.0", "min_interval_seconds: 0")
        (worktree / "config").mkdir()
        (worktree / "config" / "settings.yaml").write_text(settings, encoding="utf-8")
        respx_router.get("https://dept.example.edu/robots.txt").respond(404)
        respx_router.get(PAGE).respond(200, text="ok")
        arguments = ["--root", str(worktree), "record", "--source-id", "sites-dept", PAGE]
        assert fixtures_main(arguments, resolver=resolver) == 0
        shared = main / ".cache" / "http"
        assert capsys.readouterr().err == f"HTTP cache directory: {shared}\n"
        assert (shared / "hosts" / "dept.example.edu.json").is_file()
        assert not (worktree / ".cache").exists()
        assert (worktree / "tests" / "fixtures" / "sites-dept" / "manifest.json").is_file()
