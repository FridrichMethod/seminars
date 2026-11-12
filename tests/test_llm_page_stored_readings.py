"""The stored readings of a source apply page by page when nothing new can be asked.

A source with two pages, the first of which changed since its reading was
stored: without a provider, or once the run has used up its requests, the
first page gives nothing new, and the second, unchanged one still gives its
talk from the cache. Any other error of the client ends the extraction of
the source.
"""

from __future__ import annotations

from datetime import date
from typing import Final

import pytest

from factories import make_document, make_payload
from fakes_llm import FakeLLMCache, FakeLLMClient
from seminars.contracts import (
    LLMBudgetExceededError,
    LLMClient,
    LLMRateLimitError,
    LLMUnavailableError,
)
from seminars.models import EventDraft, RawPayload
from seminars.pipeline.services import NoProvider, ReadOnlyCache
from seminars.sources.generic.llm_page import build_page_extractor, cache_value
from test_llm_page_secrets import (
    BASE,
    CONFIG,
    PAGE_URL,
    SOURCE,
    answer,
    key_of,
    page_with,
    talk_with,
)

SECOND_URL: Final = f"{PAGE_URL}/page-2"
CHANGED: Final = {**BASE, "title": "How keys leak from private code"}


def two_pages() -> RawPayload:
    """The changed page first, then the page whose reading is stored."""
    documents = [
        make_document(url, text, content_type="text/html")
        for url, text in ((PAGE_URL, page_with(CHANGED)), (SECOND_URL, page_with(BASE)))
    ]
    return make_payload(*documents, source_id=SOURCE.id)


def stored() -> FakeLLMCache:
    """A cache with the reading of the unchanged page only."""
    entry = cache_value(date(2026, 11, 9), answer(talk_with(BASE)))
    return FakeLLMCache({key_of(page_with(BASE)): entry})


def extract(client: LLMClient, cache: FakeLLMCache) -> list[EventDraft]:
    extractor = build_page_extractor(client, config=CONFIG, cache=ReadOnlyCache(cache))
    return extractor.extract(two_pages(), SOURCE)


class TestStoredReadings:
    def test_without_a_provider_the_unchanged_page_still_gives_its_talk(self) -> None:
        cache = stored()
        drafts = extract(NoProvider(), cache)
        assert [draft.title for draft in drafts] == [BASE["title"]]
        assert cache.stats().hits == 1

    @pytest.mark.parametrize(
        "error",
        [
            LLMBudgetExceededError("the call cap of the run (40) is reached"),
            LLMUnavailableError("every provider failed"),
        ],
    )
    def test_after_an_error_that_sends_nothing_more_the_stored_reading_applies(
        self, error: Exception
    ) -> None:
        client = FakeLLMClient().fail(error)
        drafts = extract(client, stored())
        assert [draft.title for draft in drafts] == [BASE["title"]]
        assert len(client.calls) == 1

    def test_another_error_ends_the_extraction_of_the_source(self) -> None:
        client = FakeLLMClient().fail(LLMRateLimitError("slow down"))
        cache = stored()
        assert extract(client, cache) == []
        assert cache.stats().hits == 0
