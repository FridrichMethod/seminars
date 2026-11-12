"""The result cache of the LLM layer: one ASCII-only JSON file per request below data/llm_cache/.

``open_llm_cache`` is the implementation of ``seminars.contracts.LLMCache``;
the contract states the rules (what a file may contain, how an entry ages)
and tests/test_contract_llm_cache.py checks them against this module.

Layout: the entry of key ``k`` (64 hexadecimal digits, see
``seminars.contracts.llm_cache_key``) is the file ``<k[:2]>/<k>.json``::

    {
      "used": "2026-11-10",
      "value": {"fields": ["bio"], "is_talk": true}
    }

``used`` is the UTC day the entry last counted as used and the only source
of its age. The two-character directories keep each listing short: the
directory is committed, and a run changes only the files it writes.

A file that is not a valid entry (cut off, edited by hand, left by a merge)
is never an answer: a lookup treats it as a miss, the next ``put`` replaces
it and ``prune`` removes it. Files that do not look like entries (a README)
are left alone.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

from seminars.contracts import LLMCache, llm_cache_day, llm_cache_refresh_days
from seminars.models import LLMCacheStats, dump_ascii_json
from seminars.textio import read_text, write_text_atomic

KEY_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
"""What ``llm_cache_key`` returns; nothing else may name a file of the cache."""

SHARD_LENGTH: Final = 2
"""Leading characters of a key that name the directory of its entry."""

_SHARD_PATTERN: Final = re.compile(rf"[0-9a-f]{{{SHARD_LENGTH}}}")
_USED: Final = "used"
_VALUE: Final = "value"


@dataclass(frozen=True)
class _Entry:
    used: date
    value: dict[str, Any]


def _read_entry(path: Path) -> _Entry | None:
    """The entry stored in ``path``; ``None`` when the file is missing or not an entry."""
    try:
        document = json.loads(read_text(path))
    except (OSError, ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return None
    if not isinstance(document, dict):
        return None
    used, value = document.get(_USED), document.get(_VALUE)
    if not isinstance(used, str) or not isinstance(value, dict):
        return None
    try:
        return _Entry(used=date.fromisoformat(used), value=value)
    except ValueError:
        return None


class FileLLMCache:
    """``LLMCache`` over a directory; see the module docstring and the contract."""

    def __init__(self, directory: Path, *, ttl_days: int, today: date) -> None:
        self._directory = directory
        self._ttl_days = ttl_days
        self._today = today
        self._refresh_days = max(1, llm_cache_refresh_days(ttl_days))
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._writes = 0

    def _path(self, key: str) -> Path:
        if KEY_PATTERN.fullmatch(key) is None:
            raise ValueError("not a cache key: expected the 64 hex digits of llm_cache_key")
        return self._directory / key[:SHARD_LENGTH] / f"{key}.json"

    def _write(self, path: Path, value: Mapping[str, Any]) -> None:
        try:
            text = dump_ascii_json({_USED: self._today.isoformat(), _VALUE: dict(value)})
        except (TypeError, ValueError) as error:
            raise ValueError(f"a cache value must be plain JSON data: {error}") from error
        write_text_atomic(path, text)

    def _count(self, *, hit: bool) -> None:
        with self._lock:
            if hit:
                self._hits += 1
            else:
                self._misses += 1

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        entry = _read_entry(path)
        self._count(hit=entry is not None)
        if entry is None:
            return None
        if (self._today - entry.used).days >= self._refresh_days:
            self._write(path, entry.value)
        return entry.value

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        self._write(self._path(key), value)
        with self._lock:
            self._writes += 1

    def _entry_files(self) -> Iterator[Path]:
        if not self._directory.is_dir():
            return
        for shard in sorted(self._directory.iterdir()):
            if not shard.is_dir() or _SHARD_PATTERN.fullmatch(shard.name) is None:
                continue
            for path in sorted(shard.glob("*.json")):
                key = path.stem
                if KEY_PATTERN.fullmatch(key) is not None and key.startswith(shard.name):
                    yield path

    def prune(self) -> int:
        removed = 0
        for path in self._entry_files():
            entry = _read_entry(path)
            if entry is not None and (self._today - entry.used).days <= self._ttl_days:
                continue
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
                removed += 1
            with contextlib.suppress(OSError):  # not empty, or removed by someone else
                path.parent.rmdir()
        return removed

    def stats(self) -> LLMCacheStats:
        with self._lock:
            return LLMCacheStats(hits=self._hits, misses=self._misses, writes=self._writes)


def open_llm_cache(directory: Path, *, ttl_days: int, now: datetime) -> LLMCache:
    """Open the cache in ``directory``, creating the directory when needed.

    ``now`` is the time of the run and must be timezone-aware; its UTC date
    is "today" for every rule about age. Neither the wall clock nor a file
    time is ever read. ``ttl_days`` below 1 raises ``ValueError``.
    """
    if ttl_days < 1:
        raise ValueError(f"ttl_days must be at least 1, got {ttl_days}")
    today = llm_cache_day(now)
    directory.mkdir(parents=True, exist_ok=True)
    return FileLLMCache(directory, ttl_days=ttl_days, today=today)
