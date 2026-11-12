"""Private data that the scrubber cannot see as it is never reaches the LLM cache.

An address written with an entity or a zero width space, a phone number
with a no-break space: normalization would remove each of them from the
event, so none may stay in data/llm_cache, which is committed to a public
repository. The drafts still carry the rest of the talk. Every private
value in this file is made up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest

from factories import make_document, make_payload
from fakes_llm import FakeLLMClient
from seminars.config import LLMConfig
from seminars.contracts import LLMCache
from seminars.models import EventDraft
from seminars.pipeline.privacy import scrub_private
from seminars.pipeline.text import paragraphs
from seminars.sources.generic.llm_page import build_page_extractor
from test_llm_page_privacy import fields_of, stored_text, text_values
from test_llm_page_secrets import (
    BASE,
    PAGE_URL,
    SOURCE,
    answer,
    file_cache,
    page_with,
    payload,
    stored_items,
    talk_with,
)

CONFIG: Final = LLMConfig()
PRIVATE: Final = ("jane.doe", "jdoe", "555-0142", "482913")


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
