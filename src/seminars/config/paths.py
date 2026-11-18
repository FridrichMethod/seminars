"""Where every file of the project lives, relative to the repository root."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from seminars.models import FEEDS_DIRNAME

ENV_HTTP_CACHE_DIR: Final = "SEMINARS_HTTP_CACHE_DIR"
"""Environment variable that moves the HTTP cache; see ``ProjectPaths.http_cache_dir``."""

_GITDIR_KEYWORD: Final = "gitdir:"


class ConfigError(Exception):
    """A configuration file is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class ProjectPaths:
    """All well-known paths. Nothing else in the code spells these names out.

    The build output is a self-contained static site; pages load everything
    with relative URLs::

        public/
          index.html, status.html, ...   copied from site/
          locales/<lang>.json            copied from locales/
          feeds/<feed id>.ics            the ten calendar feeds
          events.json, status.json       the public payloads
    """

    root: Path

    # configuration (committed)
    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.yaml"

    @property
    def llm_file(self) -> Path:
        return self.config_dir / "llm.yaml"

    @property
    def taxonomy_file(self) -> Path:
        return self.config_dir / "taxonomy.yaml"

    @property
    def interests_file(self) -> Path:
        return self.config_dir / "interests.yaml"

    @property
    def sources_dir(self) -> Path:
        return self.config_dir / "sources.d"

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    # data (committed; everything in here is public)
    @property
    def data_dir(self) -> Path:
        """Root of the committed data files (events, uid map, health, reports, LLM cache).

        Everything below it is public and has to pass the charset rule for
        ``data/**``: text from a source is cleaned before it is written here,
        or the file is ASCII-only JSON. ``seminars.textrules`` has the rule,
        the cleaning function and ``data_tree_problems``, the check that
        ``seminars run`` applies to this directory before it finishes.
        """
        return self.root / "data"

    @property
    def events_file(self) -> Path:
        return self.data_dir / "events.json"

    @property
    def uid_map_file(self) -> Path:
        return self.data_dir / "uid_map.json"

    @property
    def health_file(self) -> Path:
        return self.data_dir / "health.json"

    @property
    def last_run_file(self) -> Path:
        return self.data_dir / "last_run.json"

    @property
    def run_report_file(self) -> Path:
        return self.data_dir / "run_report.json"

    @property
    def llm_cache_dir(self) -> Path:
        return self.data_dir / "llm_cache"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    def archive_file(self, year: int) -> Path:
        """JSON Lines archive of past events that started in ``year``."""
        return self.archive_dir / f"{year}.jsonl"

    # site sources and locale files (committed)
    @property
    def site_dir(self) -> Path:
        return self.root / "site"

    @property
    def locales_dir(self) -> Path:
        return self.root / "locales"

    # build output (git-ignored, deployed to GitHub Pages)
    @property
    def public_dir(self) -> Path:
        return self.root / "public"

    @property
    def public_feeds_dir(self) -> Path:
        return self.public_dir / FEEDS_DIRNAME

    @property
    def public_locales_dir(self) -> Path:
        return self.public_dir / "locales"

    @property
    def public_events_file(self) -> Path:
        return self.public_dir / "events.json"

    @property
    def public_status_file(self) -> Path:
        return self.public_dir / "status.json"

    # tests and caches
    @property
    def fixtures_dir(self) -> Path:
        return self.root / "tests" / "fixtures"

    @property
    def cache_dir(self) -> Path:
        return self.root / ".cache"

    @property
    def scratch_dir(self) -> Path:
        """Git-ignored space for looking at sources (``seminars.fixtures fetch --save-dir``)."""
        return self.root / ".scratch"

    def http_cache_dir(self, env: Mapping[str, str] | None = None) -> Path:
        """Directory of the live HTTP client's cache, shared by all worktrees of the repository.

        The directory holds more than cached pages: the time of the last
        request to every host and the per-host lock live here, and they are
        what keeps the crawl delay between separate processes (see
        ``seminars.httpcache``). Every process that may contact the same
        hosts at the same time has to use the same directory, so the default
        is the same for every checkout of one repository:

        * an ordinary checkout (``.git`` is a directory) and a tree without
          git use ``<root>/.cache/http``;
        * a linked git worktree (``.git`` is a file) uses
          ``<main checkout>/.cache/http``, the directory of the checkout it
          was created from. The main checkout is found by reading two small
          files, the ``gitdir:`` line of ``.git`` and the ``commondir`` file
          in the directory it names; git is not run.

        Whenever those files are not what a linked worktree has (a
        submodule, a bare repository without a main checkout, something
        unreadable), the answer is ``<root>/.cache/http``; this method never
        raises. In those layouts, and for separate clones, set the variable.

        ``env`` defaults to ``os.environ``. A non-empty ``SEMINARS_HTTP_CACHE_DIR``
        replaces the default in every case; a relative value is resolved
        against the project root.
        """
        override = (os.environ if env is None else env).get(ENV_HTTP_CACHE_DIR, "")
        if override:
            return self.root / Path(override).expanduser()
        main = main_checkout(self.root)
        return (main if main is not None else self.root) / ".cache" / "http"


def main_checkout(root: Path) -> Path | None:
    """The main checkout of the repository when ``root`` is a linked git worktree.

    A linked worktree has a file ``.git`` that reads ``gitdir: <directory>``.
    That directory, ``<main checkout>/.git/worktrees/<name>``, has a file
    ``commondir`` with the path of the repository's ``.git`` directory
    (usually ``../..``); relative paths count from the file they stand in.
    The parent of that ``.git`` directory is the main checkout.

    ``None`` for anything else: an ordinary checkout, a tree without git, a
    submodule, a bare repository, unreadable or malformed files. Nothing is
    raised and git is not run.
    """
    try:
        pointer = root / ".git"
        if not pointer.is_file():
            return None
        line = pointer.read_text(encoding="utf-8").strip()
        if not line.startswith(_GITDIR_KEYWORD):
            return None
        git_dir = root / line.removeprefix(_GITDIR_KEYWORD).strip()
        common = (git_dir / "commondir").read_text(encoding="utf-8").strip()
        if not common:
            return None
        repository = (git_dir / common).resolve()
        if repository.name != ".git" or not repository.is_dir():
            return None
        return repository.parent
    except (OSError, ValueError, RuntimeError):  # unreadable, not text, NUL in a path, loops
        return None


def find_project_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` (default: the current directory) to the repository root.

    The root is the first directory that contains config/settings.yaml.
    """
    origin = (start or Path.cwd()).resolve()
    for candidate in (origin, *origin.parents):
        if (candidate / "config" / "settings.yaml").is_file():
            return candidate
    raise ConfigError(f"no config/settings.yaml found in {origin} or any parent directory")
