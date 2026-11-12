"""Private data that the scrubber cannot see as it is never reaches the LLM cache.

An address written with an entity, a phone number with a no-break space, a
code after a label the model copied with a slip: normalization would remove
each of them from the event, so none may stay in data/llm_cache, which is
committed to a public repository. The drafts still carry the rest of the
talk. Every private value in this file is made up.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Final

import pytest

from factories import make_document, make_payload
from fakes_llm import FakeLLMClient
from seminars.config import LLMConfig
from seminars.contracts import LLMCache
from seminars.llm import privacy_filter
from seminars.models import EventDraft, LLMCacheStats
from seminars.pipeline.privacy import scrub_private
from seminars.pipeline.text import paragraphs
from seminars.sources.generic.llm_page import build_page_extractor, cache_value
from test_llm_page_privacy import fields_of, stored_text, text_values
from test_llm_page_secrets import (
    BASE,
    PAGE_URL,
    SOURCE,
    answer,
    file_cache,
    key_of,
    page_with,
    payload,
    stored_items,
    talk_with,
)

CONFIG: Final = LLMConfig()
PRIVATE: Final = ("jane.doe", "jdoe", "555-0142", "482913")

LONG: Final = (
    "We measure how fast a key pushed to a public repository is found and used, "
    "and what a maintainer can do about it in the first hour."
)
"""An abstract long enough for grounding to allow its full number of edits."""


def private_in(text: str) -> list[str]:
    return [value for value in PRIVATE if value in text]


def plain_text(values: dict[str, str]) -> str:
    """A document that is not HTML and announces one talk with ``values``."""
    return (
        f"Security seminars\n{values['title']}\n{values['name']}, {values['affiliation']}\n"
        f"Tuesday, November 17, 2026, 4:00 PM - 5:00 PM\n{values['location']}\n"
        f"{values['series']}\n{values['abstract']}\n"
    )


def extract_document(
    client: Any, text: str, content_type: str, cache: LLMCache
) -> list[EventDraft]:
    document = make_document(PAGE_URL, text, content_type=content_type)
    extractor = build_page_extractor(client, config=CONFIG, cache=cache)
    return extractor.extract(make_payload(document, source_id=SOURCE.id), SOURCE)


def extract(client: Any, text: str, cache: LLMCache) -> list[EventDraft]:
    return build_page_extractor(client, config=CONFIG, cache=cache).extract(payload(text), SOURCE)


def normalization_finds_nothing(item: dict[str, Any]) -> bool:
    """Whether normalization would scrub nothing out of the cached values of ``item``."""
    for value in text_values(item):
        read = paragraphs(value, limit=100_000)
        if read is not None and scrub_private(read) != read:
            return False
    return True


class TestDocumentsThatAreNotHtml:
    """Feeds, plain text and calendars reach the model as they are, entities and all."""

    @pytest.mark.parametrize(
        ("affiliation", "content_type", "cached"),
        [
            (
                "Stanford University, jane.doe&#64;example.edu",
                "application/rss+xml",
                "Stanford University, [email removed]",
            ),
            (
                "Stanford University, jane.doe\u200b@example.edu",
                "text/plain",
                "Stanford University, [email removed]",
            ),
            (
                "Stanford University, (650)&nbsp;555-0142",
                "text/calendar",
                "Stanford University, [phone removed]",
            ),
            ("Stanford University, jane.doe<b></b>@example.edu", "text/plain", None),
        ],
    )
    def test_an_affiliation_is_cached_without_what_normalization_would_remove(
        self, affiliation: str, content_type: str, cached: str | None, tmp_path: Path
    ) -> None:
        assert scrub_private(affiliation) == affiliation, "the scrubber alone misses it"
        values = {**BASE, "affiliation": affiliation}
        directory = tmp_path / "llm_cache"
        client = FakeLLMClient(answer(talk_with(values)))
        [draft] = extract_document(client, plain_text(values), content_type, file_cache(directory))
        assert private_in(stored_text(directory)) == []
        [item] = stored_items(directory)
        assert item["speakers"] == [{"name": BASE["name"], "affiliation": cached}]
        assert normalization_finds_nothing(item)
        assert fields_of(draft) == {**BASE, "affiliation": cached}

    @pytest.mark.parametrize("field", sorted(BASE))
    def test_every_text_value_is_read_before_it_is_scrubbed(
        self, field: str, tmp_path: Path
    ) -> None:
        note = "Questions: jane.doe&#64;example.edu or (650)&nbsp;555-0142."
        values = {**BASE, field: f"{BASE[field]}, {note}"}
        directory = tmp_path / "llm_cache"
        client = FakeLLMClient(answer(talk_with(values)))
        [draft] = extract_document(client, plain_text(values), "text/plain", file_cache(directory))
        assert private_in(stored_text(directory)) == []
        [item] = stored_items(directory)
        assert normalization_finds_nothing(item)
        expected = f"{BASE[field]}, Questions: [email removed] or [phone removed]."
        assert fields_of(draft) == {**BASE, field: expected}


class TestAnHtmlPageThatShowsAnEntity:
    def test_the_entity_is_read_as_normalization_reads_it(self, tmp_path: Path) -> None:
        shown = "Stanford University, jane.doe&#64;example.edu"
        page = page_with({**BASE, "affiliation": shown.replace("&", "&amp;")})
        directory = tmp_path / "llm_cache"
        talk = talk_with({**BASE, "affiliation": shown})
        [draft] = extract(FakeLLMClient(answer(talk)), page, file_cache(directory))
        assert private_in(stored_text(directory)) == []
        [item] = stored_items(directory)
        assert item["speakers"][0]["affiliation"] == "Stanford University, [email removed]"
        assert draft.speakers[0].affiliation == "Stanford University, [email removed]"


class TestASlipOfTheModel:
    """Grounding lets a value differ from the page by a few letters, enough to undo a scrub."""

    PAGE_ABSTRACT: Final = f"{LONG} Zoom passcode: 482913"

    @pytest.mark.parametrize("label", ["Zoom pascode", "Zoom passcde", "Zoom passcoed"])
    def test_a_code_after_a_misspelled_label_is_not_cached(
        self, label: str, tmp_path: Path
    ) -> None:
        written = f"{LONG} {label}: 482913"
        assert scrub_private(written) == written, "the scrubber does not know the label"
        values = {**BASE, "abstract": self.PAGE_ABSTRACT}
        directory = tmp_path / "llm_cache"
        talk = {**talk_with(values), "abstract": written}
        [draft] = extract(FakeLLMClient(answer(talk)), page_with(values), file_cache(directory))
        assert private_in(stored_text(directory)) == []
        [item] = stored_items(directory)
        assert item["abstract"] == f"{LONG} Zoom passcode: [removed]"
        assert draft.abstract is not None
        assert draft.abstract.startswith(LONG)
        assert private_in(draft.abstract) == []
        assert fields_of(draft) == {**BASE, "abstract": draft.abstract}

    def test_such_an_entry_from_before_loses_the_value_when_it_is_read(
        self, tmp_path: Path
    ) -> None:
        values = {**BASE, "abstract": self.PAGE_ABSTRACT}
        text = page_with(values)
        talk = {**talk_with(values), "abstract": f"{LONG} Zoom pascode: 482913"}
        directory = tmp_path / "llm_cache"
        file_cache(directory).put(key_of(text), cache_value(date(2026, 11, 9), answer(talk)))
        cache = file_cache(directory, days_later=1)
        [draft] = extract(FakeLLMClient(), text, cache)
        assert cache.stats() == LLMCacheStats(hits=1, misses=0, writes=1)
        assert private_in(stored_text(directory)) == []
        [item] = stored_items(directory)
        assert item["abstract"] is None
        assert fields_of(draft) == {**BASE, "abstract": None}


class TestOlderEntriesAndTheRulesOfToday:
    VALUES: Final = {**BASE, "affiliation": "Stanford University. E-mail: jdoe at stanford.edu"}
    TALK: Final = {
        **talk_with(VALUES),
        "speakers": [{"name": BASE["name"], "affiliation": "jdoe at stanford.edu"}],
    }

    def test_a_value_that_is_private_only_on_the_page_goes_when_it_is_read(
        self, tmp_path: Path
    ) -> None:
        assert scrub_private("jdoe at stanford.edu") == "jdoe at stanford.edu"
        text = page_with(self.VALUES)
        directory = tmp_path / "llm_cache"
        file_cache(directory).put(key_of(text), cache_value(date(2026, 11, 9), answer(self.TALK)))
        cache = file_cache(directory, days_later=1)
        [draft] = extract(FakeLLMClient(), text, cache)
        assert cache.stats() == LLMCacheStats(hits=1, misses=0, writes=1)
        assert private_in(stored_text(directory)) == []
        [item] = stored_items(directory)
        assert item["speakers"] == [{"name": BASE["name"], "affiliation": None}]
        assert fields_of(draft) == {**BASE, "affiliation": None}

    def test_an_entry_written_before_the_scrubber_knew_a_case_is_cleaned(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        text = page_with(self.VALUES)
        directory = tmp_path / "llm_cache"
        with monkeypatch.context() as patch:
            patch.setattr(privacy_filter, "project_scrubber", lambda: lambda value: value)
            [first] = extract(FakeLLMClient(answer(self.TALK)), text, file_cache(directory))
        assert first.speakers[0].affiliation == "jdoe at stanford.edu"
        assert "jdoe" in stored_text(directory)

        cache = file_cache(directory, days_later=1)
        [draft] = extract(FakeLLMClient(), text, cache)
        assert cache.stats().writes == 1
        assert private_in(stored_text(directory)) == []
        assert fields_of(draft) == {**BASE, "affiliation": None}
        settled = file_cache(directory, days_later=2)
        assert extract(FakeLLMClient(), text, settled) == [draft]
        assert settled.stats().writes == 0


class TestBlankValues:
    """Only a value that the scrubber emptied goes; one that was blank already is left alone.

    The other half of the rule is
    ``test_llm_page_privacy.TestOlderEntries.test_a_value_that_is_nothing_but_private_data_goes``.
    """

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_an_entry_with_a_blank_value_is_not_stored_again(
        self, blank: str, tmp_path: Path
    ) -> None:
        text = page_with(BASE)
        talk = {**talk_with(BASE), "speakers": [{"name": BASE["name"], "affiliation": blank}]}
        directory = tmp_path / "llm_cache"
        file_cache(directory).put(key_of(text), cache_value(date(2026, 11, 9), answer(talk)))
        [entry] = sorted(directory.rglob("*.json"))
        before = entry.read_bytes()
        cache = file_cache(directory, days_later=1)
        [draft] = extract(FakeLLMClient(), text, cache)
        assert cache.stats() == LLMCacheStats(hits=1, misses=0, writes=0)
        assert entry.read_bytes() == before
        assert json.loads(before)["value"]["answer"]["events"][0]["speakers"][0] == {
            "name": BASE["name"],
            "affiliation": blank,
        }
        assert fields_of(draft) == {**BASE, "affiliation": None}


class TestWhiteSpace:
    """Normalization collapses white space before it scrubs, so the cache is checked that way.

    A line field loses every run of white space, line breaks included; a
    block field keeps its lines and collapses the white space in each.
    """

    @pytest.mark.parametrize(
        ("note", "cached"),
        [
            ("Zoom passcode:" + "\n" * 5 + "482913", "Zoom passcode: [removed]"),
            ("Zoom passcode:" + " " * 6 + "482913", "Zoom passcode: [removed]"),
            ("Mail jane.doe @" + " " * 5 + "example.edu", "Mail [email removed]"),
            ("Call (650)" + " " * 4 + "555-0142", "Call [phone removed]"),
        ],
    )
    def test_a_value_is_cached_without_what_collapsing_reveals(
        self, note: str, cached: str, tmp_path: Path
    ) -> None:
        assert scrub_private(note) == note, "the scrubber alone misses it"
        values = {**BASE, "affiliation": f"Stanford University, {note}"}
        directory = tmp_path / "llm_cache"
        client = FakeLLMClient(answer(talk_with(values)))
        [draft] = extract_document(client, plain_text(values), "text/plain", file_cache(directory))
        assert private_in(stored_text(directory)) == []
        [item] = stored_items(directory)
        assert normalization_finds_nothing(item)
        affiliation = item["speakers"][0]["affiliation"]
        read = None if affiliation is None else " ".join(affiliation.split())
        assert read in (None, f"Stanford University, {cached}")
        assert private_in(json.dumps(fields_of(draft))) == []

    @pytest.mark.parametrize(
        "value",
        [
            "Zoom passcode:" + "\n" * 5 + "482913",
            "Mail jane.doe @" + " " * 5 + "example.edu",
            "Passcode:" + " \n " * 3 + "482913",
        ],
    )
    def test_an_entry_from_before_loses_such_a_value_when_it_is_read(
        self, value: str, tmp_path: Path
    ) -> None:
        text = page_with(BASE)
        talk = {**talk_with(BASE), "speakers": [{"name": BASE["name"], "affiliation": value}]}
        directory = tmp_path / "llm_cache"
        file_cache(directory).put(key_of(text), cache_value(date(2026, 11, 9), answer(talk)))
        cache = file_cache(directory, days_later=1)
        [draft] = extract(FakeLLMClient(), text, cache)
        assert cache.stats().writes == 1
        assert private_in(stored_text(directory)) == []
        assert private_in(json.dumps(fields_of(draft))) == []
