"""An entry that holds the empty object is a placeholder: it answers nothing.

The cache has no delete, so a user that may no longer keep an answer (one
that shows a secret, say) stores ``{}`` in its place. Such an entry counts
as a miss, is never renewed and ages out like any other entry. ``get``
still returns it exactly as it was stored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from seminars.contracts import llm_cache_key, llm_cache_refresh_days
from seminars.llm.cache import open_llm_cache
from seminars.models import LLMCacheStats

NOW: Final = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)
TTL: Final = 180
R: Final = llm_cache_refresh_days(TTL)
KEY: Final = llm_cache_key("v1", "extract", "a page")
VALUE: Final = {"reference": "2026-11-10", "answer": {"events": []}}


def entry_path(directory: Path) -> Path:
    return directory / KEY[:2] / f"{KEY}.json"


class TestPlaceholder:
    def test_it_is_returned_as_stored_and_counts_as_a_miss(self, tmp_path: Path) -> None:
        open_llm_cache(tmp_path, ttl_days=TTL, now=NOW).put(KEY, {})
        cache = open_llm_cache(tmp_path, ttl_days=TTL, now=NOW)
        assert cache.get(KEY) == {}
        assert cache.stats() == LLMCacheStats(hits=0, misses=1, writes=0)

    def test_it_is_never_renewed_and_ages_out(self, tmp_path: Path) -> None:
        open_llm_cache(tmp_path, ttl_days=TTL, now=NOW).put(KEY, {})
        before = entry_path(tmp_path).read_bytes()
        for days in (R, TTL - 1, TTL):
            cache = open_llm_cache(tmp_path, ttl_days=TTL, now=NOW + timedelta(days=days))
            assert cache.get(KEY) == {}
            assert entry_path(tmp_path).read_bytes() == before, f"renewed on day {days}"
        late = open_llm_cache(tmp_path, ttl_days=TTL, now=NOW + timedelta(days=TTL + 1))
        assert late.prune() == 1
        assert not entry_path(tmp_path).exists()

    def test_storing_an_answer_over_it_makes_a_hit_again(self, tmp_path: Path) -> None:
        cache = open_llm_cache(tmp_path, ttl_days=TTL, now=NOW)
        cache.put(KEY, {})
        cache.put(KEY, VALUE)
        assert cache.get(KEY) == VALUE
        assert cache.stats() == LLMCacheStats(hits=1, misses=0, writes=2)


class TestEntriesWithAValue:
    def test_an_empty_list_of_events_is_an_answer(self, tmp_path: Path) -> None:
        open_llm_cache(tmp_path, ttl_days=TTL, now=NOW).put(KEY, VALUE)
        cache = open_llm_cache(tmp_path, ttl_days=TTL, now=NOW + timedelta(days=R))
        assert cache.get(KEY) == VALUE
        assert cache.stats() == LLMCacheStats(hits=1, misses=0, writes=0)
        renewed = open_llm_cache(tmp_path, ttl_days=TTL, now=NOW + timedelta(days=TTL + 1))
        assert renewed.prune() == 0, "the hit after R days renewed the entry"
