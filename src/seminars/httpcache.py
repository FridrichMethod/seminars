"""On-disk state of the polite HTTP client, kept under a git-ignored cache directory.

Three kinds of records, all small JSON files:

* ``pages/<sha256 of url>.json``: body plus ETag / Last-Modified of a response,
  used for conditional requests;
* ``hosts/<host>.json``: when the host was last contacted (so that the crawl
  delay also holds across separate processes, for example repeated runs of
  the fixture recorder) and the robots.txt copies of its origins.

The cache is disposable: unreadable or outdated files are treated as absent.
With ``directory=None`` nothing is read or written.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pydantic import BaseModel, ValidationError

from seminars.models import Model, dump_json
from seminars.textio import read_text, write_text_atomic


class CachedPage(Model):
    """A cached 200 response that carried a validator."""

    url: str
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None
    text: str


class CachedRobots(Model):
    """A robots.txt answer: ``text`` is None when the file does not exist (4xx)."""

    fetched_at: float  # epoch seconds
    text: str | None = None


class HostRecord(Model):
    """What is remembered about one host between processes."""

    last_request_at: float | None = None  # epoch seconds
    robots: dict[str, CachedRobots] = {}  # keyed by origin, e.g. "https://example.edu"


class HttpCache:
    """Reads and writes the records described in the module docstring."""

    def __init__(self, directory: Path | None) -> None:
        self._directory = directory

    def _page_path(self, url: str) -> Path | None:
        if self._directory is None:
            return None
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self._directory / "pages" / f"{digest}.json"

    def _host_path(self, host: str) -> Path | None:
        if self._directory is None:
            return None
        return self._directory / "hosts" / (re.sub(r"[^a-z0-9.-]", "_", host.lower()) + ".json")

    @staticmethod
    def _read[T: BaseModel](path: Path | None, model: type[T]) -> T | None:
        if path is None or not path.is_file():
            return None
        try:
            return model.model_validate_json(read_text(path))
        except (OSError, ValueError, ValidationError):
            return None

    @staticmethod
    def _write(path: Path | None, record: BaseModel) -> None:
        if path is not None:
            write_text_atomic(path, dump_json(record))

    def page(self, url: str) -> CachedPage | None:
        """The cached response of ``url``, if any."""
        page = self._read(self._page_path(url), CachedPage)
        return page if page is not None and page.url == url else None

    def store_page(self, page: CachedPage) -> None:
        self._write(self._page_path(page.url), page)

    def host(self, host: str) -> HostRecord:
        """The record of ``host``; an empty record when nothing is known."""
        return self._read(self._host_path(host), HostRecord) or HostRecord()

    def store_host(self, host: str, record: HostRecord) -> None:
        self._write(self._host_path(host), record)
