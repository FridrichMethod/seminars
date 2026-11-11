"""The file cache of LLM answers, beyond the conformance checks of the contract.

tests/test_contract_llm_cache.py holds the age rules and the charset rule and
runs them against this implementation. Here: where the files go, what
happens to files that are not valid entries, and which keys are accepted.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from seminars.contracts import LLMCache, LLMCacheFactory, llm_cache_key, load_implementation
from seminars.llm.cache import open_llm_cache
from seminars.models import LLMCacheStats

NOW = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)
KEY = llm_cache_key("v1", "tag", "a talk")
VALUE = {"fields": ["bio"], "is_talk": True}


def entry_path(directory: Path, key: str) -> Path:
    return directory / key[:2] / f"{key}.json"


class TestLayout:
    def test_the_contract_points_at_this_function(self) -> None:
        factory: LLMCacheFactory = load_implementation("llm_cache")
        assert factory is open_llm_cache

    def test_it_is_a_cache(self, tmp_path: Path) -> None:
        cache: LLMCache = open_llm_cache(tmp_path, ttl_days=180, now=NOW)
        assert isinstance(cache, LLMCache)

    def test_opening_creates_the_directory(self, tmp_path: Path) -> None:
        directory = tmp_path / "data" / "llm_cache"
        open_llm_cache(directory, ttl_days=180, now=NOW)
        assert directory.is_dir()
        assert list(directory.iterdir()) == []

    def test_an_entry_is_one_file_in_a_directory_named_after_the_key(self, tmp_path: Path) -> None:
        open_llm_cache(tmp_path, ttl_days=180, now=NOW).put(KEY, VALUE)
        files = [path for path in tmp_path.rglob("*") if path.is_file()]
        assert files == [entry_path(tmp_path, KEY)]

    def test_the_file_is_sorted_indented_ascii_json_with_the_day_of_the_run(
        self, tmp_path: Path
    ) -> None:
        value = {"title": "Poincar" + chr(0xE9), "fields": ["math"]}
        open_llm_cache(tmp_path, ttl_days=180, now=NOW).put(KEY, value)
        text = entry_path(tmp_path, KEY).read_text(encoding="utf-8")
        assert text.isascii()
        assert text.endswith("}\n")
        assert "\r" not in text
        assert json.loads(text) == {"used": "2026-11-10", "value": value}
        assert text.index('"used"') < text.index('"value"')

    def test_no_temporary_file_is_left_behind(self, tmp_path: Path) -> None:
        cache = open_llm_cache(tmp_path, ttl_days=180, now=NOW)
        for index in range(5):
            cache.put(llm_cache_key("v1", "tag", str(index)), VALUE)
        assert [path.name for path in tmp_path.rglob("*.tmp")] == []


class TestValues:
    def test_a_value_comes_back_as_a_copy(self, tmp_path: Path) -> None:
        cache = open_llm_cache(tmp_path, ttl_days=180, now=NOW)
        cache.put(KEY, VALUE)
        first = cache.get(KEY)
        assert first == VALUE
        assert first is not None
        first["fields"].append("chem")
        assert cache.get(KEY) == VALUE

    def test_put_replaces_an_entry(self, tmp_path: Path) -> None:
        cache = open_llm_cache(tmp_path, ttl_days=180, now=NOW)
        cache.put(KEY, VALUE)
        cache.put(KEY, {"fields": [], "is_talk": False})
        assert cache.get(KEY) == {"fields": [], "is_talk": False}
        assert cache.stats() == LLMCacheStats(hits=1, misses=0, writes=2)

    def test_put_does_not_keep_a_reference_to_the_given_mapping(self, tmp_path: Path) -> None:
        cache = open_llm_cache(tmp_path, ttl_days=180, now=NOW)
        value = {"fields": ["bio"]}
        cache.put(KEY, value)
        value["fields"].append("med")
        assert cache.get(KEY) == {"fields": ["bio"]}

    def test_a_value_that_is_not_json_is_refused_and_nothing_is_written(
        self, tmp_path: Path
    ) -> None:
        cache = open_llm_cache(tmp_path, ttl_days=180, now=NOW)
        with pytest.raises(ValueError, match="JSON"):
            cache.put(KEY, {"score": float("nan")})
        with pytest.raises(ValueError, match="JSON"):
            cache.put(KEY, {"when": NOW})
        assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
        assert cache.stats().writes == 0


class TestKeys:
    @pytest.mark.parametrize(
        "key",
        ["", "abc", "../../etc/passwd", KEY.upper(), KEY + "0", KEY[:-1] + "/", "g" * 64],
    )
    def test_only_keys_of_llm_cache_key_are_accepted(self, tmp_path: Path, key: str) -> None:
        cache = open_llm_cache(tmp_path / "cache", ttl_days=180, now=NOW)
        with pytest.raises(ValueError, match="not a cache key"):
            cache.get(key)
        with pytest.raises(ValueError, match="not a cache key"):
            cache.put(key, VALUE)
        assert [path for path in tmp_path.rglob("*") if path.is_file()] == []


class TestOpening:
    def test_a_naive_time_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            open_llm_cache(tmp_path, ttl_days=180, now=datetime(2026, 11, 10, 20, 0))

    @pytest.mark.parametrize("ttl_days", [0, -3])
    def test_a_lifetime_below_one_day_is_refused(self, tmp_path: Path, ttl_days: int) -> None:
        with pytest.raises(ValueError, match="ttl_days"):
            open_llm_cache(tmp_path, ttl_days=ttl_days, now=NOW)

    def test_a_lifetime_of_one_day_never_renews_on_the_day_of_the_write(
        self, tmp_path: Path
    ) -> None:
        cache = open_llm_cache(tmp_path, ttl_days=1, now=NOW)
        cache.put(KEY, VALUE)
        written = entry_path(tmp_path, KEY).read_bytes()
        assert cache.get(KEY) == VALUE
        assert entry_path(tmp_path, KEY).read_bytes() == written
        next_day = open_llm_cache(tmp_path, ttl_days=1, now=NOW + timedelta(days=1))
        assert next_day.get(KEY) == VALUE
        assert json.loads(entry_path(tmp_path, KEY).read_text(encoding="utf-8"))["used"] == (
            "2026-11-11"
        )


class TestBrokenFiles:
    """A file that is not an entry is never an answer; the cache repairs itself."""

    @pytest.mark.parametrize(
        "content",
        [
            "",
            "not json\n",
            "[1, 2]\n",
            '{"value": {"fields": []}}\n',
            '{"used": "2026-11-10"}\n',
            '{"used": "yesterday", "value": {}}\n',
            '{"used": 20261110, "value": {}}\n',
            '{"used": "2026-11-10", "value": [1]}\n',
            "\xff\xfe broken bytes",
        ],
    )
    def test_a_broken_entry_is_a_miss_and_is_replaced_by_the_next_put(
        self, tmp_path: Path, content: str
    ) -> None:
        path = entry_path(tmp_path, KEY)
        path.parent.mkdir(parents=True)
        path.write_bytes(content.encode("latin-1"))
        cache = open_llm_cache(tmp_path, ttl_days=180, now=NOW)
        assert cache.get(KEY) is None
        assert cache.stats() == LLMCacheStats(hits=0, misses=1, writes=0)
        cache.put(KEY, VALUE)
        assert cache.get(KEY) == VALUE

    def test_prune_removes_broken_entries(self, tmp_path: Path) -> None:
        cache = open_llm_cache(tmp_path, ttl_days=180, now=NOW)
        cache.put(KEY, VALUE)
        other = llm_cache_key("v1", "tag", "broken")
        broken = entry_path(tmp_path, other)
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("{}\n", encoding="utf-8")
        assert cache.prune() == 1
        assert not broken.exists()
        assert cache.get(KEY) == VALUE

    def test_prune_leaves_files_that_are_not_entries_alone(self, tmp_path: Path) -> None:
        cache = open_llm_cache(tmp_path, ttl_days=180, now=NOW)
        cache.put(KEY, VALUE)
        readme = tmp_path / "README.md"
        readme.write_text("notes\n", encoding="utf-8")
        stray = tmp_path / KEY[:2] / "notes.json"
        stray.write_text("{}\n", encoding="utf-8")
        misplaced = tmp_path / "zz" / f"{KEY}.json"
        misplaced.parent.mkdir()
        misplaced.write_text("{}\n", encoding="utf-8")
        late = open_llm_cache(tmp_path, ttl_days=180, now=NOW + timedelta(days=400))
        assert late.prune() == 1
        assert readme.exists()
        assert stray.exists()
        assert misplaced.exists()

    def test_prune_removes_the_directories_it_emptied(self, tmp_path: Path) -> None:
        open_llm_cache(tmp_path, ttl_days=180, now=NOW).put(KEY, VALUE)
        late = open_llm_cache(tmp_path, ttl_days=180, now=NOW + timedelta(days=181))
        assert late.prune() == 1
        assert list(tmp_path.iterdir()) == []

    def test_prune_of_a_directory_that_was_removed_finds_nothing(self, tmp_path: Path) -> None:
        directory = tmp_path / "cache"
        cache = open_llm_cache(directory, ttl_days=180, now=NOW)
        directory.rmdir()
        assert cache.prune() == 0
        assert cache.get(KEY) is None


class TestThreads:
    def test_counters_are_exact_when_several_threads_use_the_cache(self, tmp_path: Path) -> None:
        cache = open_llm_cache(tmp_path, ttl_days=180, now=NOW)
        keys = [llm_cache_key("v1", "tag", str(index)) for index in range(8)]

        def work(key: str) -> None:
            for _ in range(20):
                cache.get(llm_cache_key("v1", "tag", "absent"))
                cache.put(key, VALUE)
                assert cache.get(key) == VALUE

        threads = [threading.Thread(target=work, args=(key,)) for key in keys]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert cache.stats() == LLMCacheStats(hits=160, misses=160, writes=160)
