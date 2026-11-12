"""The page extractor keeps meeting codes, e-mail addresses and phone numbers out of the cache.

data/llm_cache is committed to a public repository, and an answer quotes the
page. Every text value of an answer goes through the scrubber of
normalization before it is cached, and so does every entry that is read; the
drafts still carry the rest of the talk. A hit is stored again only when
the scrubber or the secret filter changed it, never because grounding
reads it differently today. Every private value in this file is made up.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Final

import pytest

from fakes import FakeSecretScanner
from fakes_llm import FakeLLMCache, FakeLLMClient
from seminars.config import LLMConfig
from seminars.contracts import IMPLEMENTATIONS, LLMCache, SecretScanner
from seminars.llm.privacy_filter import project_scrubber
from seminars.models import EventDraft, LLMCacheStats
from seminars.pipeline.privacy import scrub_private
from seminars.sources.generic.llm_page import build_page_extractor, cache_value
from test_llm_page_secrets import (
    BASE,
    SOURCE,
    answer,
    file_cache,
    key_of,
    page_with,
    payload,
    stored_items,
    talk_with,
)

EMAIL: Final = "jane.doe@example.edu"
PHONE: Final = "(650) 555-0142"
CODE: Final = "482913"
NOTE: Final = f"Questions: {EMAIL} or {PHONE}. Zoom passcode: {CODE}"
"""What a talk announcement often adds: the address and phone of the host, the Zoom code."""

LINK: Final = "https://stanford.zoom.us/j/123?pwd=" + "abcDEF123"
PRIVATE: Final = (EMAIL, "555-0142", CODE, "pwd=")
CONFIG: Final = LLMConfig()


def private_in(text: str) -> list[str]:
    """The private values of this file that ``text`` shows."""
    return [value for value in PRIVATE if value in text]


def stored_text(directory: Path) -> str:
    """Every cache file below ``directory`` as it is committed."""
    return "".join(path.read_text(encoding="utf-8") for path in sorted(directory.rglob("*.json")))


def text_values(item: dict[str, Any]) -> list[str]:
    """Every text value of one cached answer item."""
    values = [item[name] for name in ("title", "series", "location", "abstract")]
    for speaker in item["speakers"]:
        values.extend((speaker["name"], speaker["affiliation"]))
    return [value for value in values if value is not None]


def extract(
    client: Any, text: str, cache: LLMCache | None, config: LLMConfig = CONFIG
) -> list[EventDraft]:
    extractor = build_page_extractor(client, config=config, cache=cache)
    return extractor.extract(payload(text), SOURCE)


def fields_of(draft: EventDraft) -> dict[str, str | None]:
    """The text values of a draft under the names of ``BASE``."""
    [speaker] = draft.speakers
    return {
        "title": draft.title,
        "name": speaker.name,
        "affiliation": speaker.affiliation,
        "series": draft.series,
        "location": draft.location,
        "abstract": draft.abstract,
    }


def entry_scanner() -> SecretScanner:
    """A scanner factory that only finds something in a whole cache entry, never in a value."""
    return FakeSecretScanner(['"cancelled": false'])


def room_scrubber(text: str) -> str:
    """A scrubber for ``IMPLEMENTATIONS``: takes the room of ``BASE`` out of every text."""
    return text.replace("Room 104", "[room removed]")


class TestTheScrubber:
    def test_it_is_the_one_of_normalization(self) -> None:
        assert project_scrubber() is scrub_private

    def test_the_extractor_takes_it_from_the_contract(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setitem(IMPLEMENTATIONS, "private_scrubber", f"{__name__}:room_scrubber")
        directory = tmp_path / "llm_cache"
        client = FakeLLMClient(answer(talk_with(BASE)))
        [draft] = extract(client, page_with(BASE), file_cache(directory))
        assert draft.location == "Gates Computer Science Building, [room removed]"
        assert "Room 104" not in stored_text(directory)
        assert draft.source_text is not None
        assert "Room 104" not in draft.source_text

    def test_the_samples_are_private_to_it(self) -> None:
        assert private_in(scrub_private(f"{NOTE}. Join at {LINK}")) == []
        assert private_in(scrub_private(page_with(BASE))) == []


class TestNewAnswers:
    @pytest.mark.parametrize("field", sorted(BASE))
    def test_a_text_value_is_cached_scrubbed_and_the_rest_is_kept(
        self, field: str, tmp_path: Path
    ) -> None:
        values = {**BASE, field: f"{BASE[field]}, {NOTE}"}
        directory = tmp_path / "llm_cache"
        client = FakeLLMClient(answer(talk_with(values)))
        [draft] = extract(client, page_with(values), file_cache(directory))
        assert private_in(stored_text(directory)) == []
        [item] = stored_items(directory)
        assert all(scrub_private(value) == value for value in text_values(item))
        expected = {**BASE, field: scrub_private(values[field])}
        assert expected[field] != values[field]
        assert fields_of(draft) == expected
        assert private_in(json.dumps(fields_of(draft))) == []
        assert draft.source_text is not None
        assert private_in(draft.source_text) == [], "the text the values are grounded in"
        assert draft.url == "https://cs.stanford.edu/events/leaked-keys"
        assert str(draft.start) == "2026-11-17 16:00:00"

    def test_a_meeting_link_in_the_text_loses_its_code(self, tmp_path: Path) -> None:
        values = {**BASE, "abstract": f"{BASE['abstract']} Join at {LINK} on the day."}
        directory = tmp_path / "llm_cache"
        client = FakeLLMClient(answer(talk_with(values)))
        [draft] = extract(client, page_with(values), file_cache(directory))
        assert private_in(stored_text(directory)) == []
        assert draft.abstract == scrub_private(values["abstract"])
        assert draft.abstract is not None
        assert "https://stanford.zoom.us/j/123" in draft.abstract

    def test_a_value_that_is_private_only_in_the_words_around_it_is_not_cached(
        self, tmp_path: Path
    ) -> None:
        spelled = "jdoe at stanford.edu"
        assert scrub_private(spelled) == spelled, "on its own it reads like a place"
        values = {**BASE, "affiliation": f"E-mail: {spelled}"}
        talk = talk_with(values)
        talk["speakers"] = [{"name": BASE["name"], "affiliation": spelled}]
        directory = tmp_path / "llm_cache"
        [draft] = extract(FakeLLMClient(answer(talk)), page_with(values), file_cache(directory))
        assert spelled not in stored_text(directory)
        [item] = stored_items(directory)
        assert item["speakers"] == [{"name": BASE["name"], "affiliation": None}]
        assert fields_of(draft) == {**BASE, "affiliation": None}

    def test_the_next_run_reads_the_scrubbed_entry_as_it_is(self, tmp_path: Path) -> None:
        values = {**BASE, "abstract": f"{BASE['abstract']} {NOTE}"}
        directory = tmp_path / "llm_cache"
        first = extract(
            FakeLLMClient(answer(talk_with(values))), page_with(values), file_cache(directory)
        )
        before = stored_text(directory)
        cache = file_cache(directory, days_later=1)
        later = FakeLLMClient()
        assert extract(later, page_with(values), cache) == first
        assert later.calls == []
        assert stored_text(directory) == before
        assert cache.stats() == LLMCacheStats(hits=1, misses=0, writes=0)


class TestOlderEntries:
    VALUES: Final = {
        **BASE,
        "affiliation": f"{BASE['affiliation']} ({EMAIL})",
        "location": f"{BASE['location']}, or call {PHONE}",
        "abstract": f"{BASE['abstract']} {NOTE}",
    }

    def written_before(self, directory: Path) -> str:
        """An entry written before answers were scrubbed; returns the page it is for."""
        text = page_with(self.VALUES)
        entry = cache_value(date(2026, 11, 9), answer(talk_with(self.VALUES)))
        file_cache(directory).put(key_of(text), entry)
        assert private_in(stored_text(directory)) == list(PRIVATE[:3])
        return text

    def test_an_entry_with_private_data_is_scrubbed_when_it_is_read(self, tmp_path: Path) -> None:
        directory = tmp_path / "llm_cache"
        text = self.written_before(directory)
        cache = file_cache(directory, days_later=1)
        later = FakeLLMClient()
        [draft] = extract(later, text, cache)
        assert later.calls == []
        assert cache.stats() == LLMCacheStats(hits=1, misses=0, writes=1)
        assert private_in(stored_text(directory)) == []
        [item] = stored_items(directory)
        assert item["speakers"][0]["affiliation"] == BASE["affiliation"]
        assert item["location"] == scrub_private(self.VALUES["location"])
        assert item["abstract"] == scrub_private(self.VALUES["abstract"])
        assert (item["title"], item["series"]) == (BASE["title"], BASE["series"])
        entry = json.loads(stored_text(directory))
        assert entry["value"]["reference"] == "2026-11-09", "the day it was first read stays"
        assert fields_of(draft) == {
            **BASE,
            "location": scrub_private(self.VALUES["location"]),
            "abstract": scrub_private(self.VALUES["abstract"]),
        }

    def test_a_scrubbed_entry_is_not_stored_again(self, tmp_path: Path) -> None:
        directory = tmp_path / "llm_cache"
        text = self.written_before(directory)
        first = extract(FakeLLMClient(), text, file_cache(directory, days_later=1))
        before = stored_text(directory)
        cache = file_cache(directory, days_later=2)
        assert extract(FakeLLMClient(), text, cache) == first
        assert stored_text(directory) == before
        assert cache.stats().writes == 0

    def test_a_value_that_is_nothing_but_private_data_goes(self, tmp_path: Path) -> None:
        directory = tmp_path / "llm_cache"
        values = {**BASE, "affiliation": f"({EMAIL})"}
        assert scrub_private(values["affiliation"]) == ""
        text = page_with(values)
        entry = cache_value(date(2026, 11, 9), answer(talk_with(values)))
        file_cache(directory).put(key_of(text), entry)
        [draft] = extract(FakeLLMClient(), text, file_cache(directory, days_later=1))
        [item] = stored_items(directory)
        assert item["speakers"] == [{"name": BASE["name"], "affiliation": None}]
        assert fields_of(draft) == {**BASE, "affiliation": None}

    def test_an_entry_without_private_data_is_left_as_it_is(self, tmp_path: Path) -> None:
        directory = tmp_path / "llm_cache"
        text = page_with(BASE)
        file_cache(directory).put(
            key_of(text), cache_value(date(2026, 11, 9), answer(talk_with(BASE)))
        )
        before = stored_text(directory)
        cache = file_cache(directory, days_later=1)
        [draft] = extract(FakeLLMClient(), text, cache)
        assert fields_of(draft) == BASE
        assert stored_text(directory) == before
        assert cache.stats() == LLMCacheStats(hits=1, misses=0, writes=0)


class TestGroundingOfAHit:
    """Grounding depends on the configuration, so a hit is grounded when it is read, not stored."""

    TYPO: Final = BASE["title"].replace("leak", "leek")
    STRICT: Final = LLMConfig(grounding_min_similarity=100)

    def cached(self, directory: Path, values: dict[str, str]) -> str:
        """The page of ``values`` and an entry with a title the model got one letter wrong.

        A new answer is stored in the words of the page where the model's
        differ, so only an entry from before has such a title.
        """
        text = page_with(values)
        talk = {**talk_with(values), "title": self.TYPO}
        file_cache(directory).put(key_of(text), cache_value(date(2026, 11, 9), answer(talk)))
        [draft] = extract(FakeLLMClient(), text, file_cache(directory))
        assert draft.title == BASE["title"]
        return text

    def test_a_new_answer_is_cached_in_the_words_of_the_page(self, tmp_path: Path) -> None:
        directory = tmp_path / "llm_cache"
        text = page_with(BASE)
        talk = {**talk_with(BASE), "title": self.TYPO}
        [draft] = extract(FakeLLMClient(answer(talk)), text, file_cache(directory))
        assert draft.title == BASE["title"]
        [item] = stored_items(directory)
        assert item["title"] == BASE["title"]
        [strict] = extract(FakeLLMClient(), text, file_cache(directory, days_later=1), self.STRICT)
        assert strict.title == BASE["title"], "page text is grounded by any threshold"

    def test_a_stricter_grounding_never_rewrites_an_entry(self, tmp_path: Path) -> None:
        directory = tmp_path / "llm_cache"
        text = self.cached(directory, BASE)
        before = stored_text(directory)
        strict = file_cache(directory, days_later=1)
        [draft] = extract(FakeLLMClient(), text, strict, self.STRICT)
        assert draft.title is None
        assert stored_text(directory) == before
        assert strict.stats() == LLMCacheStats(hits=1, misses=0, writes=0)
        [again] = extract(FakeLLMClient(), text, file_cache(directory, days_later=2))
        assert again.title == BASE["title"], "the usual grounding takes the title again"

    def test_a_rewrite_keeps_what_the_current_grounding_does_not_take(self, tmp_path: Path) -> None:
        directory = tmp_path / "llm_cache"
        values = {**BASE, "abstract": f"{BASE['abstract']} {NOTE}"}
        text = page_with(values)
        talk = {**talk_with(values), "title": self.TYPO}
        entry = cache_value(date(2026, 11, 9), answer(talk))
        file_cache(directory).put(key_of(text), entry)
        strict = file_cache(directory, days_later=1)
        [draft] = extract(FakeLLMClient(), text, strict, self.STRICT)
        assert draft.title is None
        assert strict.stats().writes == 1
        [item] = stored_items(directory)
        assert item["title"] == self.TYPO
        assert item["abstract"] == scrub_private(values["abstract"])
        [again] = extract(FakeLLMClient(), text, file_cache(directory, days_later=2))
        assert again.title == BASE["title"]


class TestPlaceholder:
    def test_an_entry_that_cannot_be_cleaned_counts_as_a_miss_from_then_on(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setitem(IMPLEMENTATIONS, "secret_scanner", f"{__name__}:entry_scanner")
        directory = tmp_path / "llm_cache"
        text = page_with(BASE)
        entry = cache_value(date(2026, 11, 9), answer(talk_with(BASE)))
        file_cache(directory).put(key_of(text), entry)

        replaced = file_cache(directory, days_later=1)
        [draft] = extract(FakeLLMClient(), text, replaced)
        assert draft.title == BASE["title"]
        assert replaced.stats() == LLMCacheStats(hits=1, misses=0, writes=1)
        assert json.loads(stored_text(directory))["value"] == {}

        asked = FakeLLMClient(answer(talk_with(BASE)))
        later = file_cache(directory, days_later=2)
        assert len(extract(asked, text, later)) == 1
        assert len(asked.calls) == 1
        assert later.stats() == LLMCacheStats(hits=0, misses=1, writes=0)
        assert json.loads(stored_text(directory))["value"] == {}

    def test_a_placeholder_from_the_fake_cache_also_reads_as_no_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(IMPLEMENTATIONS, "secret_scanner", f"{__name__}:entry_scanner")
        cache = FakeLLMCache({key_of(page_with(BASE)): {}})
        asked = FakeLLMClient(answer(talk_with(BASE)))
        assert len(extract(asked, page_with(BASE), cache)) == 1
        assert len(asked.calls) == 1
        assert cache.writes == 0
