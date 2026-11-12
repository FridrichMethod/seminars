"""Input left out for its length shows up in the run report, wherever it is left out."""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes_llm import FakeLLMClient
from seminars.classify.llm_tagger import build_llm_tagger
from seminars.config import LLMConfig
from seminars.llm.base import SkipRecorder, record_skipped_too_long
from seminars.llm.chain import ProviderChain
from seminars.models import LLMTagResult
from seminars.sources.generic.llm_page import MAX_CHUNKS_PER_SOURCE, build_page_extractor
from test_llm_fakes import StubProvider
from test_llm_page_extract import PAGE, SOURCE, payload
from test_llm_tagger import candidate

TAGGED = {"results": [{"index": 0, "is_talk": True, "fields": ["bio"]}]}


def chain_of(*providers: StubProvider, max_input_chars: int = 12000) -> ProviderChain:
    return ProviderChain(list(providers), max_calls=50, max_input_chars=max_input_chars)


def long_page(pieces: int) -> str:
    rows = "".join(f"<p>{f'Seminar {n}, Nov 17. ':x<450}</p>" for n in range(pieces))
    return f"<html><body>{rows}</body></html>"


class TestTheChainKeepsTheCount:
    def test_the_chain_is_a_skip_recorder_and_adds_up_what_callers_report(self) -> None:
        chain = chain_of(StubProvider("claude-cli"))
        assert isinstance(chain, SkipRecorder)
        chain.record_skipped_too_long(2)
        chain.restricted({"codex-cli"}).record_skipped_too_long(3)
        assert chain.usage().skipped_too_long == 5
        assert chain.usage().calls == 0

    def test_a_negative_count_is_refused(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            chain_of(StubProvider("claude-cli")).record_skipped_too_long(-1)

    def test_a_client_that_keeps_no_count_is_left_alone(self) -> None:
        client = FakeLLMClient()
        assert not isinstance(client, SkipRecorder)
        record_skipped_too_long(client, 4)  # nothing to report to, and no error
        assert client.calls == []


class TestTagging:
    def test_an_event_too_long_for_any_request_is_counted_and_the_rest_is_tagged(self) -> None:
        stub = StubProvider("claude-cli").always(TAGGED)
        chain = chain_of(stub)
        config = LLMConfig(max_input_chars=600, batch_size=1)
        huge = candidate(1, title="A very long title " * 100)
        answers = build_llm_tagger(chain, config=config).tag_batch([huge, candidate(2)])
        assert answers == {"event-2": LLMTagResult(fields=["bio"])}
        usage = chain.usage()
        assert (usage.calls, usage.skipped_too_long) == (1, 1)
        assert len(stub.calls) == 1

    def test_nothing_is_counted_when_every_event_fits(self) -> None:
        chain = chain_of(StubProvider("claude-cli").always(TAGGED))
        build_llm_tagger(chain, config=LLMConfig()).tag_batch([candidate(1)])
        assert chain.usage().skipped_too_long == 0


class TestPageReading:
    def test_pieces_after_the_cap_of_a_source_are_counted(self) -> None:
        stub = StubProvider("claude-cli").always({"events": []})
        chain = chain_of(stub)
        config = LLMConfig().evolve(max_input_chars=800)
        extractor = build_page_extractor(chain, config=config)
        extractor.extract(payload(long_page(MAX_CHUNKS_PER_SOURCE + 3)), SOURCE)
        usage = chain.usage()
        assert (usage.calls, usage.skipped_too_long) == (MAX_CHUNKS_PER_SOURCE, 3)

    def test_a_document_without_room_in_any_request_is_counted(self) -> None:
        stub = StubProvider("claude-cli").always({"events": []})
        chain = chain_of(stub, max_input_chars=300)
        config = LLMConfig().evolve(max_input_chars=300)
        build_page_extractor(chain, config=config).extract(payload(PAGE, PAGE), SOURCE)
        assert (chain.usage().calls, chain.usage().skipped_too_long) == (0, 2)
        assert stub.calls == []

    def test_a_page_that_fits_counts_nothing(self) -> None:
        chain = chain_of(StubProvider("claude-cli").always({"events": []}))
        build_page_extractor(chain, config=LLMConfig()).extract(payload(PAGE), SOURCE)
        assert chain.usage().skipped_too_long == 0

    def test_the_documents_name_the_cap_of_a_source(self) -> None:
        assert MAX_CHUNKS_PER_SOURCE == 6
        docs = Path(__file__).resolve().parents[1] / "docs"
        assert "at most six pieces per source" in (docs / "LLM_CREDENTIALS.md").read_text()
        chinese = (docs / "LLM_CREDENTIALS.zh-CN.md").read_text(encoding="utf-8")
        assert "\u6bcf\u4e2a\u6e90\u6700\u591a\u8bfb\u516d\u6bb5" in chinese
