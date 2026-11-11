"""On-disk state of the polite HTTP client, kept under a git-ignored cache directory.

Three kinds of files:

* ``pages/<sha256 of url>.json``: body plus ETag / Last-Modified of a response,
  used for conditional requests;
* ``hosts/<host>.json``: when the host was last contacted and the robots.txt
  copies of its origins;
* ``hosts/<host>.lock``: an empty file that carries the lock of the host.

The host record and the host lock are what make the crawl delay and "one
request at a time per host" hold between processes: a client takes the lock,
reads the record, waits, sends its request, writes the record and releases
the lock. This only covers processes that use the SAME cache directory.
``seminars.config.ProjectPaths.http_cache_dir`` therefore gives every git
worktree of one repository the directory of the main checkout. Separate
clones have a directory each and know nothing about each other unless
``SEMINARS_HTTP_CACHE_DIR`` points them at one directory.

The lock is an advisory ``flock``. It exists on POSIX systems; on Windows
``host_lock`` does nothing, so there the guarantee is limited to a single
process.

The cache is disposable: unreadable or outdated files are treated as absent.
With ``directory=None`` nothing is read, written or locked.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ValidationError

from seminars.models import Model, dump_json
from seminars.textio import read_text, write_text_atomic

if sys.platform != "win32":
    import fcntl


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

    def merge(self, other: HostRecord) -> Self:
        """Both views of one host combined.

        The result has the later request time and, for every origin, the
        robots.txt copy that was fetched last. A client merges the record on
        disk into its own after taking the host lock, so it sees what other
        processes did in the meantime without losing what it knows itself.
        """
        times = [at for at in (self.last_request_at, other.last_request_at) if at is not None]
        robots = dict(self.robots)
        for origin, copy in other.robots.items():
            known = robots.get(origin)
            if known is None or copy.fetched_at > known.fetched_at:
                robots[origin] = copy
        return type(self)(last_request_at=max(times, default=None), robots=robots)


class HttpCache:
    """Reads and writes the records described in the module docstring."""

    def __init__(self, directory: Path | None) -> None:
        self._directory = directory

    def _page_path(self, url: str) -> Path | None:
        if self._directory is None:
            return None
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self._directory / "pages" / f"{digest}.json"

    def _host_path(self, host: str, suffix: str = ".json") -> Path | None:
        if self._directory is None:
            return None
        return self._directory / "hosts" / (re.sub(r"[^a-z0-9.-]", "_", host.lower()) + suffix)

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

    @contextlib.contextmanager
    def host_lock(self, host: str) -> Iterator[None]:
        """Hold the lock of ``host`` among all processes that share this directory.

        Blocks until the lock is free. Whoever holds it is the only one
        allowed to send a request to ``host``; take it before reading the
        host record and keep it until the record is written back. The lock
        is released when the block ends and also when the process dies.
        Without a directory, and on Windows, this is a no-op.
        """
        path = self._host_path(host, ".lock")
        if path is None or sys.platform == "win32":
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
