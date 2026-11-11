"""Where every file of the project lives, relative to the repository root."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from seminars.models import FEEDS_DIRNAME


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
          feeds/<feed id>.ics            the nine calendar feeds
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
    def http_cache_dir(self) -> Path:
        return self.cache_dir / "http"


def find_project_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` (default: the current directory) to the repository root.

    The root is the first directory that contains config/settings.yaml.
    """
    origin = (start or Path.cwd()).resolve()
    for candidate in (origin, *origin.parents):
        if (candidate / "config" / "settings.yaml").is_file():
            return candidate
    raise ConfigError(f"no config/settings.yaml found in {origin} or any parent directory")
