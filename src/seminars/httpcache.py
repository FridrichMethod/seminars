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
With ``directory=None`` nothing is read, written or locked. A directory
that cannot be written to (read-only, full, a file in its place) is the
same as none: the failure is logged once and the client goes on without a
cache and without the lock between processes.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final, Self

from pydantic import BaseModel, ValidationError

from seminars.models import Model, dump_json
from seminars.textio import read_text, write_text_atomic

if sys.platform != "win32":
    import fcntl

logger = logging.getLogger(__name__)

_MAX_HOST_FILE_STEM: Final = 80


def header_safe(value: str | None) -> str | None:
    """``value`` when it can be sent as a request header, otherwise ``None``.

    An ETag or Last-Modified value is sent back in the next request. A
    server can put bytes outside printable ASCII into it, which no request
    header may contain; such a validator is useless and is dropped.
    """
    if value and value.isascii() and value.isprintable():
        return value
    return None


class CachedPage(Model):
    """A cached 200 response that carried a validator."""

    url: str
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None
    text: str

    @property
    def usable(self) -> bool:
        """Whether a conditional request can be made from this entry.

        It needs a validator, and every validator it has must be fit for a
        request header (``header_safe``).
        """
        validators = [value for value in (self.etag, self.last_modified) if value is not None]
        return bool(validators) and all(header_safe(value) for value in validators)


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

    def trimmed(self, *, oldest: float, newest: float, keep: int) -> Self:
        """The record without the robots.txt copies nobody will use again.

        A copy stays when it was fetched between ``oldest`` and ``newest``
        (the window in which a copy counts as recent) and is among the
        ``keep`` latest ones. A site decides how many origins a host has
        (any port is one) and how large each robots.txt is, and the record
        is read and written with every request, so it must not grow with
        them.
        """
        recent = [
            (copy.fetched_at, origin)
            for origin, copy in self.robots.items()
            if oldest <= copy.fetched_at <= newest
        ]
        kept = {origin for _, origin in sorted(recent)[max(len(recent) - keep, 0) :]}
        robots = {origin: copy for origin, copy in self.robots.items() if origin in kept}
        return self if len(robots) == len(self.robots) else self.evolve(robots=robots)


class HttpCache:
    """Reads and writes the records described in the module docstring."""

    def __init__(self, directory: Path | None) -> None:
        self._directory = directory
        self._complained = False

    def _page_path(self, url: str) -> Path | None:
        if self._directory is None:
            return None
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self._directory / "pages" / f"{digest}.json"

    def _host_path(self, host: str, suffix: str = ".json") -> Path | None:
        """The file of ``host``: its name when that is a short, plain file name.

        Any other host (an IPv6 address, a name of up to 253 characters)
        gets a readable prefix and a digest, so that the name is valid on
        every file system and two hosts never share a file.
        """
        if self._directory is None:
            return None
        name = host.lower()
        stem = re.sub(r"[^a-z0-9.-]", "_", name).strip(".") or "host"
        if stem != name or len(stem) > _MAX_HOST_FILE_STEM:
            digest = hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()[:16]
            stem = f"{stem[:_MAX_HOST_FILE_STEM]}-{digest}"
        return self._directory / "hosts" / (stem + suffix)

    def _complain(self, action: str, error: OSError) -> None:
        """Say once that the directory cannot be used; the client goes on without it."""
        if not self._complained:
            self._complained = True
            logger.warning(
                "HTTP cache directory %s cannot be used (%s: %s); going on without it",
                self._directory,
                action,
                error.strerror or type(error).__name__,
            )

    @staticmethod
    def _read[T: BaseModel](path: Path | None, model: type[T]) -> T | None:
        if path is None or not path.is_file():
            return None
        try:
            return model.model_validate_json(read_text(path))
        except (OSError, ValueError, ValidationError):
            return None

    def _write(self, path: Path | None, record: BaseModel) -> None:
        if path is None:
            return
        try:
            write_text_atomic(path, dump_json(record))
        except OSError as error:
            self._complain("writing", error)

    def page(self, url: str) -> CachedPage | None:
        """The cached response of ``url``, if there is one that can be revalidated."""
        page = self._read(self._page_path(url), CachedPage)
        return page if page is not None and page.url == url and page.usable else None

    def store_page(self, page: CachedPage) -> None:
        self._write(self._page_path(page.url), page)

    def host(self, host: str) -> HostRecord:
        """The record of ``host``; an empty record when nothing is known."""
        return self._read(self._host_path(host), HostRecord) or HostRecord()

    def store_host(self, host: str, record: HostRecord) -> None:
        self._write(self._host_path(host), record)

    @contextlib.contextmanager
    def host_lock(self, host: str, wait: Callable[[], None] | None = None) -> Iterator[None]:
        """Hold the lock of ``host`` among all processes that share this directory.

        Blocks until the lock is free. Whoever holds it is the only one
        allowed to send a request to ``host``; take it before reading the
        host record and keep it until the record is written back. The lock
        is released when the block ends and also when the process dies.
        Without a directory, with one that cannot be written to, and on
        Windows, this is a no-op.

        With ``wait``, the lock is tried instead of waited for: as long as
        somebody else holds it, ``wait`` is called before the next try. It
        should pause for a moment, and it ends the waiting by raising.
        """
        path = self._host_path(host, ".lock")
        if path is None or sys.platform == "win32":
            yield
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as error:
            self._complain("locking", error)
            yield
            return
        try:
            locked = self._acquire(descriptor, wait)
            try:
                yield
            finally:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _acquire(self, descriptor: int, wait: Callable[[], None] | None) -> bool:
        """Take the lock on ``descriptor``; False when this file system cannot lock at all."""
        try:
            if wait is None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                return True
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except BlockingIOError:
                    pass
                wait()
        except OSError as error:
            self._complain("locking", error)
            return False
