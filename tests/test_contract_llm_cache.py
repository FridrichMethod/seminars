"""Conformance of an ``LLMCache``: what it writes below data/ and how its entries age.

The checks are plain functions over a cache factory. Here they run against
a small reference implementation, to show that they pass, and against
variants of it with the usual mistakes, to show that they catch them. The
real cache is checked as soon as ``seminars.llm.cache`` exists, and the
in-memory fake is held to the same age rules.
"""

from __future__ import annotations

import importlib.util
import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from fakes_llm import FakeLLMCache
from seminars.contracts import (
    IMPLEMENTATIONS,
    LLM_CACHE_REFRESH_CAP_DAYS,
    LLMCache,
    LLMCacheFactory,
    llm_cache_day,
    llm_cache_key,
    llm_cache_refresh_days,
    load_implementation,
)
from seminars.models import LLMCacheStats, dump_ascii_json
from seminars.textio import write_text_atomic
from seminars.textrules import data_tree_problems

TTL = 100
R = llm_cache_refresh_days(TTL)
WINDOW = TTL - R
DAY_ZERO = datetime(2020, 3, 1, 12, 0, tzinfo=UTC)
"""Years away from the real date: a cache that reads the wall clock fails every age check."""

VALUE = {"fields": ["bio"], "is_talk": True}
HOSTILE_ANSWER = {
    "drafts": [
        {
            "title": "Of" + chr(0xFB01) + "ce" + chr(0xA0) + "hours " + chr(0x1F680),
            "speaker": "Poincar" + chr(0xE9) + chr(0x200B),
            "abstract": "soft" + chr(0xAD) + "hyphen" + chr(0x2028) + "next\tline\r\n",
        }
    ]
}
"""What a page extraction answer can look like: it quotes page text that was not cleaned."""


def day(offset: int) -> datetime:
    """Noon UTC, ``offset`` days after ``DAY_ZERO``."""
    return DAY_ZERO + timedelta(days=offset)


def key_of(name: str) -> str:
    return llm_cache_key("v1", "tag", name)


def snapshot(directory: Path) -> dict[str, bytes]:
    """Every file below ``directory`` with its content."""
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


# --- the checks -----------------------------------------------------------------------------


def check_cache_keeps_data_clean(open_cache: LLMCacheFactory, directory: Path) -> None:
    """The part of the contract that concerns the charset rule of data/."""
    key = llm_cache_key("v1", "extract", "some page")
    cache = open_cache(directory, ttl_days=TTL, now=day(0))
    cache.put(key, HOSTILE_ANSWER)
    assert cache.get(key) == HOSTILE_ANSWER
    assert open_cache(directory, ttl_days=TTL, now=day(0)).get(key) == HOSTILE_ANSWER
    files = [path for path in directory.rglob("*") if path.is_file()]
    assert files, "the cache wrote nothing below its directory"
    assert data_tree_problems(directory) == []
    assert all(path.read_bytes().isascii() for path in files)


def check_entries_expire_by_their_recorded_date(
    open_cache: LLMCacheFactory, directory: Path
) -> None:
    """``prune`` removes what was recorded more than ``ttl_days`` ago, and only that."""
    never, early, renewed = key_of("never hit"), key_of("hit too early"), key_of("hit in time")
    first = open_cache(directory, ttl_days=TTL, now=day(0))
    assert first.get(never) is None
    for key in (never, early, renewed):
        first.put(key, VALUE)
    assert first.stats() == LLMCacheStats(hits=0, misses=1, writes=3)
    assert open_cache(directory, ttl_days=TTL, now=day(R - 1)).get(early) == VALUE
    in_time = open_cache(directory, ttl_days=TTL, now=day(R))
    assert in_time.get(renewed) == VALUE
    assert in_time.stats() == LLMCacheStats(hits=1, misses=0, writes=0)
    assert open_cache(directory, ttl_days=TTL, now=day(TTL)).prune() == 0, "ttl_days old is kept"
    late = open_cache(directory, ttl_days=TTL, now=day(TTL + 1))
    assert late.prune() == 2, "the entry hit after R days was renewed, the other two were not"
    assert late.get(never) is None
    assert late.get(early) is None
    assert late.prune() == 0
    assert open_cache(directory, ttl_days=TTL, now=day(R + TTL)).prune() == 0
    last = open_cache(directory, ttl_days=TTL, now=day(R + TTL + 1))
    assert last.prune() == 1
    assert last.get(renewed) is None


def check_an_entry_in_regular_use_is_never_pruned(
    open_cache: LLMCacheFactory, directory: Path
) -> None:
    """The guarantee: one hit in every window of ``ttl_days - R`` days keeps an entry.

    The schedule is the worst one the guarantee allows: a hit one day too
    early to renew the date, then nothing for a whole window.
    """
    key = key_of("regular")
    open_cache(directory, ttl_days=TTL, now=day(0)).put(key, VALUE)
    today = 0
    for _ in range(6):
        for gap in (R - 1, WINDOW):
            today += gap
            cache = open_cache(directory, ttl_days=TTL, now=day(today))
            assert cache.prune() == 0, f"pruned on day {today} although it is in use"
            assert cache.get(key) == VALUE
            assert cache.prune() == 0


def check_days_are_utc_dates_of_the_given_time(
    open_cache: LLMCacheFactory, directory: Path
) -> None:
    """Age is counted in UTC calendar days of ``now``; a naive ``now`` is refused."""
    renewed, kept_old = key_of("utc date reached"), key_of("utc date not reached")
    first = open_cache(directory, ttl_days=TTL, now=day(0))
    first.put(renewed, VALUE)
    first.put(kept_old, VALUE)
    west = timezone(timedelta(hours=-8))
    east = timezone(timedelta(hours=9))
    # 16:30 on the day before in the west is 00:30 UTC on day R: the date is R days old.
    early_on_day_r = (day(R).replace(hour=0, minute=30)).astimezone(west)
    assert early_on_day_r.date() == day(R - 1).date()
    assert open_cache(directory, ttl_days=TTL, now=early_on_day_r).get(renewed) == VALUE
    # 08:30 on day R in the east is 23:30 UTC on day R - 1: one day too early.
    late_on_day_before = (day(R - 1).replace(hour=23, minute=30)).astimezone(east)
    assert late_on_day_before.date() == day(R).date()
    assert open_cache(directory, ttl_days=TTL, now=late_on_day_before).get(kept_old) == VALUE
    late = open_cache(directory, ttl_days=TTL, now=day(TTL + 1))
    assert late.prune() == 1
    assert late.get(kept_old) is None
    try:
        open_cache(directory, ttl_days=TTL, now=datetime(2020, 3, 1, 12, 0))
    except ValueError:
        pass
    else:
        raise AssertionError("a naive time of the run was accepted")


def check_an_earlier_run_leaves_later_entries_alone(
    open_cache: LLMCacheFactory, directory: Path
) -> None:
    """An entry dated after today (an offline run with an older ``now``) is used, not touched."""
    key = key_of("from the future")
    open_cache(directory, ttl_days=TTL, now=day(2 * TTL)).put(key, VALUE)
    earlier = open_cache(directory, ttl_days=TTL, now=day(0))
    assert earlier.get(key) == VALUE
    assert earlier.prune() == 0
    assert open_cache(directory, ttl_days=TTL, now=day(3 * TTL)).prune() == 0
    assert open_cache(directory, ttl_days=TTL, now=day(3 * TTL + 1)).prune() == 1


def check_files_change_only_when_a_date_is_renewed(
    open_cache: LLMCacheFactory, directory: Path
) -> None:
    """A hit rewrites nothing unless the recorded date is at least R days old."""
    key = key_of("stable file")
    first = open_cache(directory, ttl_days=TTL, now=day(0))
    first.put(key, VALUE)
    written = snapshot(directory)
    assert written, "the cache wrote nothing below its directory"
    assert first.get(key) == VALUE
    assert first.get(key) == VALUE
    assert snapshot(directory) == written, "a hit on the day of the write changed a file"
    again = open_cache(directory, ttl_days=TTL, now=day(0).replace(hour=23, minute=59))
    assert again.get(key) == VALUE
    assert snapshot(directory) == written, "a second run on the same day changed a file"
    for offset in (1, R - 1):
        assert open_cache(directory, ttl_days=TTL, now=day(offset)).get(key) == VALUE
        assert snapshot(directory) == written, f"a hit after {offset} day(s) changed a file"
    renewing = open_cache(directory, ttl_days=TTL, now=day(R))
    assert renewing.get(key) == VALUE
    renewed = snapshot(directory)
    assert renewed != written, "a hit after R days must record the new date in the file"
    assert renewing.get(key) == VALUE
    assert open_cache(directory, ttl_days=TTL, now=day(R)).get(key) == VALUE
    assert snapshot(directory) == renewed, "a second hit on the day of the renewal changed a file"
    earlier = open_cache(directory, ttl_days=TTL, now=day(0))
    assert earlier.get(key) == VALUE
    assert earlier.prune() == 0
    assert snapshot(directory) == renewed, "a run with an earlier time changed a file"


def check_file_times_do_not_matter(open_cache: LLMCacheFactory, directory: Path) -> None:
    """Age comes from the date inside the file: git does not keep file times."""

    def set_times(moment: datetime) -> None:
        for path in directory.rglob("*"):
            os.utime(path, (moment.timestamp(), moment.timestamp()))

    old, fresh = key_of("old entry, new file time"), key_of("new entry, old file time")
    open_cache(directory, ttl_days=TTL, now=day(0)).put(old, VALUE)
    open_cache(directory, ttl_days=TTL, now=day(TTL)).put(fresh, VALUE)
    set_times(day(TTL + 1))  # what a fresh checkout looks like: every file is "new"
    checkout = open_cache(directory, ttl_days=TTL, now=day(TTL + 1))
    assert checkout.prune() == 1, "an old entry survived because its file looked new"
    assert checkout.get(old) is None
    set_times(day(-5 * TTL))  # and the other way round
    later = open_cache(directory, ttl_days=TTL, now=day(TTL + 2))
    assert later.prune() == 0, "a recent entry was pruned because its file looked old"
    before = snapshot(directory)
    assert later.get(fresh) == VALUE
    assert snapshot(directory) == before, "a hit was renewed because its file looked old"


AGE_CHECKS: list[Callable[[LLMCacheFactory, Path], None]] = [
    check_entries_expire_by_their_recorded_date,
    check_an_entry_in_regular_use_is_never_pruned,
    check_days_are_utc_dates_of_the_given_time,
    check_an_earlier_run_leaves_later_entries_alone,
]
"""What every cache must do, whatever it stores its entries in."""

FILE_CHECKS: list[Callable[[LLMCacheFactory, Path], None]] = [
    check_cache_keeps_data_clean,
    check_files_change_only_when_a_date_is_renewed,
    check_file_times_do_not_matter,
]
"""What a cache that writes files below data/llm_cache/ must do in addition."""

ALL_CHECKS = [*AGE_CHECKS, *FILE_CHECKS]


def check_a_placeholder_answers_nothing(open_cache: LLMCacheFactory, directory: Path) -> None:
    """A stored ``{}`` is returned as stored, counts as a miss, is never renewed, ages out."""
    key = key_of("placeholder")
    open_cache(directory, ttl_days=TTL, now=day(0)).put(key, {})
    for offset in (0, R, TTL):
        cache = open_cache(directory, ttl_days=TTL, now=day(offset))
        assert cache.get(key) == {}, offset
        assert cache.stats() == LLMCacheStats(hits=0, misses=1, writes=0), offset
    assert open_cache(directory, ttl_days=TTL, now=day(TTL + 1)).prune() == 1


def names(checks: list[Callable[[LLMCacheFactory, Path], None]]) -> list[str]:
    return [check.__name__.removeprefix("check_") for check in checks]


# --- a reference implementation and its broken variants -------------------------------------


class ReferenceCache:
    """The smallest cache that follows the contract: one ASCII-only JSON file per key."""

    def __init__(self, directory: Path, *, ttl_days: int, now: datetime) -> None:
        self._directory = directory
        self._ttl_days = ttl_days
        self._today = self.day_of(now)
        self._hits = self._misses = self._writes = 0

    @staticmethod
    def day_of(now: datetime) -> date:
        return llm_cache_day(now)

    def _path(self, key: str) -> Path:
        return self._directory / f"{key}.json"

    def recorded(self, path: Path) -> date:
        return date.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["used"])

    def write(self, key: str, value: Mapping[str, Any]) -> None:
        entry = {"used": self._today.isoformat(), "value": dict(value)}
        write_text_atomic(self._path(key), dump_ascii_json(entry))

    def renews(self, age_days: int) -> bool:
        return age_days >= max(1, llm_cache_refresh_days(self._ttl_days))

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.is_file():
            self._misses += 1
            return None
        self._hits += 1
        value = json.loads(path.read_text(encoding="utf-8"))["value"]
        if self.renews((self._today - self.recorded(path)).days):
            self.write(key, value)
        return value

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        self._writes += 1
        self.write(key, value)

    def prune(self) -> int:
        paths = sorted(self._directory.glob("*.json")) if self._directory.is_dir() else []
        expired = [p for p in paths if (self._today - self.recorded(p)).days > self._ttl_days]
        for path in expired:
            path.unlink()
        return len(expired)

    def stats(self) -> LLMCacheStats:
        return LLMCacheStats(hits=self._hits, misses=self._misses, writes=self._writes)


class ReadableCache(ReferenceCache):
    """Mistake: writes readable JSON, so page text reaches data/ uncleaned."""

    def write(self, key: str, value: Mapping[str, Any]) -> None:
        entry = {"used": self._today.isoformat(), "value": dict(value)}
        self._directory.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")


class FileTimeCache(ReferenceCache):
    """Mistake: takes the age from the modification time of the file."""

    def recorded(self, path: Path) -> date:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date()

    def write(self, key: str, value: Mapping[str, Any]) -> None:
        write_text_atomic(self._path(key), dump_ascii_json({"value": dict(value)}))
        noon = datetime(self._today.year, self._today.month, self._today.day, 12, tzinfo=UTC)
        os.utime(self._path(key), (noon.timestamp(), noon.timestamp()))


class EagerCache(ReferenceCache):
    """Mistake: renews the date on every hit, so the daily run rewrites every file it uses."""

    def renews(self, age_days: int) -> bool:
        return age_days >= 1


class ForgetfulCache(ReferenceCache):
    """Mistake: never renews a date, so entries in daily use are pruned after ttl_days."""

    def renews(self, age_days: int) -> bool:
        return False


class WallClockCache(ReferenceCache):
    """Mistake: asks the clock instead of using the time of the run."""

    @staticmethod
    def day_of(now: datetime) -> date:
        llm_cache_day(now)
        return datetime.now(tz=UTC).date()


class LocalDateCache(ReferenceCache):
    """Mistake: uses the calendar date of ``now`` in its own zone, not in UTC."""

    @staticmethod
    def day_of(now: datetime) -> date:
        llm_cache_day(now)
        return now.date()


class TrustingCache(ReferenceCache):
    """Mistake: accepts a naive ``now``, whose date depends on the machine."""

    @staticmethod
    def day_of(now: datetime) -> date:
        return now.date() if now.utcoffset() is None else llm_cache_day(now)


MISTAKES: list[tuple[type[ReferenceCache], Callable[[LLMCacheFactory, Path], None]]] = [
    (ReadableCache, check_cache_keeps_data_clean),
    (FileTimeCache, check_file_times_do_not_matter),
    (EagerCache, check_files_change_only_when_a_date_is_renewed),
    (EagerCache, check_entries_expire_by_their_recorded_date),
    (ForgetfulCache, check_an_entry_in_regular_use_is_never_pruned),
    (ForgetfulCache, check_entries_expire_by_their_recorded_date),
    (WallClockCache, check_entries_expire_by_their_recorded_date),
    (WallClockCache, check_an_earlier_run_leaves_later_entries_alone),
    (LocalDateCache, check_days_are_utc_dates_of_the_given_time),
    (TrustingCache, check_days_are_utc_dates_of_the_given_time),
]
"""Each broken variant with a check that must fail for it."""


class TestTheRule:
    def test_refresh_days_are_half_the_lifetime_but_at_most_thirty(self) -> None:
        assert LLM_CACHE_REFRESH_CAP_DAYS == 30
        assert [llm_cache_refresh_days(ttl) for ttl in (1, 2, 7, 59, 60, 61, 180, 365)] == [
            0,
            1,
            3,
            29,
            30,
            30,
            30,
            30,
        ]
        assert (TTL, R, WINDOW) == (100, 30, 70)

    def test_the_day_of_a_run_is_its_utc_date(self) -> None:
        west = timezone(timedelta(hours=-8))
        assert llm_cache_day(datetime(2026, 11, 10, 20, 0, tzinfo=UTC)) == date(2026, 11, 10)
        assert llm_cache_day(datetime(2026, 11, 10, 15, 59, tzinfo=west)) == date(2026, 11, 10)
        assert llm_cache_day(datetime(2026, 11, 10, 16, 0, tzinfo=west)) == date(2026, 11, 11)
        with pytest.raises(ValueError, match="timezone-aware"):
            llm_cache_day(datetime(2026, 11, 10, 20, 0))


class TestReferenceImplementation:
    @pytest.mark.parametrize("check", ALL_CHECKS, ids=names(ALL_CHECKS))
    def test_a_cache_that_follows_the_contract_passes(
        self, tmp_path: Path, check: Callable[[LLMCacheFactory, Path], None]
    ) -> None:
        check(ReferenceCache, tmp_path / "data" / "llm_cache")

    @pytest.mark.parametrize(
        ("broken", "check"),
        MISTAKES,
        ids=[
            f"{broken.__name__}-{check.__name__.removeprefix('check_')}"
            for broken, check in MISTAKES
        ],
    )
    def test_a_mistake_is_caught(
        self,
        tmp_path: Path,
        broken: type[ReferenceCache],
        check: Callable[[LLMCacheFactory, Path], None],
    ) -> None:
        with pytest.raises(AssertionError):
            check(broken, tmp_path / "data" / "llm_cache")

    def test_the_reference_is_a_cache(self, tmp_path: Path) -> None:
        cache: LLMCache = ReferenceCache(tmp_path, ttl_days=TTL, now=day(0))
        factory: LLMCacheFactory = ReferenceCache
        assert isinstance(cache, LLMCache)
        assert factory is ReferenceCache

    def test_a_second_hit_on_the_same_day_does_not_rewrite_the_file(self, tmp_path: Path) -> None:
        """The reason for the rule: the daily commit must not contain every cache file."""
        key = key_of("daily")
        ReferenceCache(tmp_path, ttl_days=180, now=day(0)).put(key, VALUE)
        (path,) = tmp_path.iterdir()
        content, modified = path.read_bytes(), path.stat().st_mtime_ns
        for offset in (0, 0, 1, 2, 29):
            cache = ReferenceCache(tmp_path, ttl_days=180, now=day(offset))
            assert cache.get(key) == VALUE
            assert cache.get(key) == VALUE
            assert path.read_bytes() == content
            assert path.stat().st_mtime_ns == modified, "the file was written again"
        assert ReferenceCache(tmp_path, ttl_days=180, now=day(30)).get(key) == VALUE
        assert path.read_bytes() != content
        assert json.loads(path.read_text(encoding="utf-8"))["used"] == "2020-03-31"


class TestFake:
    """The in-memory fake follows the same age rules, so consumers see realistic behaviour."""

    @staticmethod
    def shared_store() -> LLMCacheFactory:
        entries: dict[str, dict[str, Any]] = {}
        dates: dict[str, date] = {}

        def open_cache(directory: Path, *, ttl_days: int, now: datetime) -> LLMCache:
            cache = FakeLLMCache(ttl_days=ttl_days, now=now)
            cache.entries, cache.dates = entries, dates
            return cache

        return open_cache

    @pytest.mark.parametrize("check", AGE_CHECKS, ids=names(AGE_CHECKS))
    def test_the_fake_follows_the_age_rules(
        self, tmp_path: Path, check: Callable[[LLMCacheFactory, Path], None]
    ) -> None:
        check(self.shared_store(), tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_a_placeholder_of_the_fake_answers_nothing(self, tmp_path: Path) -> None:
        check_a_placeholder_answers_nothing(self.shared_store(), tmp_path)


class TestRealCache:
    @pytest.mark.parametrize("check", ALL_CHECKS, ids=names(ALL_CHECKS))
    def test_the_real_cache(
        self, tmp_path: Path, check: Callable[[LLMCacheFactory, Path], None]
    ) -> None:
        """Runs as soon as ``seminars.llm.cache`` exists."""
        module_name = IMPLEMENTATIONS["llm_cache"].split(":")[0]
        if importlib.util.find_spec(module_name) is None:
            pytest.skip(f"{module_name} is not implemented yet")
        open_cache: LLMCacheFactory = load_implementation("llm_cache")
        check(open_cache, tmp_path / "data" / "llm_cache")

    def test_a_placeholder_of_the_real_cache_answers_nothing(self, tmp_path: Path) -> None:
        open_cache: LLMCacheFactory = load_implementation("llm_cache")
        check_a_placeholder_answers_nothing(open_cache, tmp_path / "data" / "llm_cache")
