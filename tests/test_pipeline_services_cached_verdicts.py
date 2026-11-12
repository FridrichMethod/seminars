"""Without a provider the LLM answers stored in earlier runs still apply; nothing is asked.

A run whose credential is missing and a run whose credential is refused
must give the same data: both answer the open cases of tagging and the
page readings from data/llm_cache and from nothing else. The cache is
only read in such a run: nothing new is stored in it and nothing is
pruned.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from factories import REFERENCE_NOW, make_candidate, make_event
from fakes import FakeEventFilter, FakeFieldTagger, FakeIssueClient, FakePicksScorer
from fakes_llm import FakeLLMCache, FakeLLMClient
from fakes_outputs import FakeEmitters
from seminars.classify.llm_tagger import build_llm_tagger, event_payload, payload_key
from seminars.config import NO_PROVIDER, AppConfig
from seminars.contracts import LLMCache, LLMClient, LLMUnavailableError, llm_cache_key
from seminars.llm.cache import open_llm_cache
from seminars.models import Candidate, LLMTagResult, TagDecision
from seminars.pipeline import services as wiring
from seminars.pipeline.secret_scan import build_secret_scanner
from seminars.pipeline.tagging import tag_candidates
from seminars.sources.generic import llm_page
from test_llm_page_extract import PAGE, SOURCE, answer, chunk_of, payload
from test_llm_page_extract import TALK as PAGE_TALK


class Parts:
    """Hands out fakes, the real LLM tagger and cache, and records the factory calls."""

    def __init__(self, cache: LLMCache, chain: FakeLLMClient | None = None) -> None:
        self.cache = cache
        self.chain = chain
        self.emitters = FakeEmitters()
        self.calls: dict[str, dict[str, Any]] = {}

    def _record(self, key: str, factory: Callable[..., Any]) -> Callable[..., Any]:
        def recorded(*args: Any, **kwargs: Any) -> Any:
            self.calls[key] = {"args": args, "kwargs": kwargs}
            return factory(*args, **kwargs)

        return recorded

    def load(self, key: str) -> Any:
        table: dict[str, Any] = {
            "event_filter": lambda *args: FakeEventFilter(),
            "field_tagger": lambda *args: FakeFieldTagger(),
            "picks_scorer": lambda *args: FakePicksScorer(),
            "ics_feeds": self.emitters.write_ics_feeds,
            "site_data": self.emitters.write_site_data,
            "health_update": self.emitters.update_health,
            "secret_scanner": build_secret_scanner,
            "llm_client": self._record(key, lambda *args: self.chain),
            "llm_cache": self._record(key, lambda *args, **kwargs: self.cache),
            "llm_tagger": self._record(key, build_llm_tagger),
            "page_extractor": self._record(key, llm_page.build_page_extractor),
            "issue_client": lambda env: FakeIssueClient(),
        }
        return table[key]


def install(monkeypatch: pytest.MonkeyPatch, parts: Parts) -> Parts:
    monkeypatch.setattr(wiring, "load", parts.load)
    return parts


def build(config: AppConfig, **options: Any) -> wiring.Services:
    return wiring.build_services(config, {}, now=REFERENCE_NOW, offline=True, **options)


def stored(item: Candidate, answer: LLMTagResult) -> dict[str, dict[str, Any]]:
    return {payload_key(event_payload(item)): answer.model_dump(mode="json")}


SOCIAL = make_candidate(make_event(uid="social", title="Mixer in the courtyard", fields=[]))
TALK = make_candidate(make_event(uid="talk", title="Enzyme kinetics", fields=[]))
NEW = make_candidate(make_event(uid="new", title="A talk nobody asked about yet", fields=[]))
NOT_A_TALK = LLMTagResult(fields=[], is_talk=False)
CHEMISTRY = LLMTagResult(fields=["chem"], is_talk=True)


class NeedsLLM:
    """The rules cannot place any event."""

    def tag(self, candidate: Candidate) -> TagDecision:
        return TagDecision(fields=[], method="rules", needs_llm=True)


class TestTheWiring:
    def test_without_a_provider_a_tagger_answers_from_the_cache(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cache = FakeLLMCache({**stored(SOCIAL, NOT_A_TALK), **stored(TALK, CHEMISTRY)})
        parts = install(monkeypatch, Parts(cache))
        bundle = build(app_config, llm_cache_dir=tmp_path / "cache")
        assert bundle.llm is None
        assert bundle.llm_tagger is not None
        assert parts.calls["llm_cache"]["args"] == (tmp_path / "cache",)
        assert parts.calls["llm_cache"]["kwargs"] == {
            "ttl_days": app_config.llm.cache_ttl_days,
            "now": REFERENCE_NOW,
        }
        answers = bundle.llm_tagger.tag_batch([SOCIAL, TALK, NEW])
        assert answers == {"social": NOT_A_TALK, "talk": CHEMISTRY}

    def test_the_run_neither_counts_nor_prunes_that_cache(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, Parts(FakeLLMCache()))
        assert build(app_config).llm_cache is None

    def test_nothing_is_asked_and_nothing_is_stored(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = FakeLLMCache()
        parts = install(monkeypatch, Parts(cache))
        bundle = build(app_config)
        assert bundle.llm_tagger is not None
        assert bundle.llm_tagger.tag_batch([NEW]) == {}
        assert cache.writes == 0
        handed: LLMCache = parts.calls["llm_tagger"]["kwargs"]["cache"]
        handed.put(payload_key(event_payload(NEW)), CHEMISTRY.model_dump(mode="json"))
        assert handed.prune() == 0
        assert cache.entries == {}
        assert cache.prunes == 0

    def test_the_client_of_that_tagger_never_answers(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parts = install(monkeypatch, Parts(FakeLLMCache()))
        build(app_config)
        (client,) = parts.calls["llm_tagger"]["args"]
        assert isinstance(client, LLMClient)
        assert client.name == NO_PROVIDER

        class Anything(BaseModel):
            value: int = 0

        with pytest.raises(LLMUnavailableError):
            client.complete_json("system", "user", Anything, max_tokens=16)

    def test_rules_only_on_request_reads_no_cache_either(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parts = install(monkeypatch, Parts(FakeLLMCache(stored(SOCIAL, NOT_A_TALK))))
        bundle = build(app_config, use_llm=False)
        assert bundle.llm_tagger is None
        assert "llm_cache" not in parts.calls

    def test_with_a_provider_the_tagger_gets_the_chain_and_the_cache_itself(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = FakeLLMCache()
        parts = install(monkeypatch, Parts(cache, chain=FakeLLMClient()))
        bundle = build(app_config)
        assert bundle.llm_cache is cache
        assert parts.calls["llm_tagger"]["args"] == (parts.chain,)
        assert parts.calls["llm_tagger"]["kwargs"]["cache"] is cache


class TestTheSameDataWithAndWithoutACredential:
    def tagged(self, tagger: Any) -> tuple[list[str], dict[str, list[str]]]:
        result = tag_candidates([SOCIAL, TALK, NEW], tagger=NeedsLLM(), llm_tagger=tagger)
        excluded = [item.title for item in result.excluded]
        return excluded, {event.uid: list(event.fields) for event in result.events}

    def test_no_credential_gives_what_a_refused_credential_gives(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        directory = tmp_path / "llm_cache"
        cache = open_llm_cache(directory, ttl_days=180, now=REFERENCE_NOW)
        for key, value in {**stored(SOCIAL, NOT_A_TALK), **stored(TALK, CHEMISTRY)}.items():
            cache.put(key, value)
        before = sorted(path.relative_to(directory) for path in directory.rglob("*"))

        refused = FakeLLMClient().fail(LLMUnavailableError("401"), times=5)
        with_refused = build_llm_tagger(refused, config=app_config.llm, cache=cache)

        install(monkeypatch, Parts(cache))
        without = build(app_config).llm_tagger

        expected = (["Mixer in the courtyard"], {"talk": ["chem"], "new": []})
        assert self.tagged(with_refused) == expected
        assert self.tagged(without) == expected
        after = sorted(path.relative_to(directory) for path in directory.rglob("*"))
        assert after == before


def test_the_stored_answers_reach_the_tagger_unchanged(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries: Mapping[str, dict[str, Any]] = stored(TALK, CHEMISTRY)
    cache = FakeLLMCache(entries)
    parts = install(monkeypatch, Parts(cache))
    build(app_config)
    handed: LLMCache = parts.calls["llm_tagger"]["kwargs"]["cache"]
    (key,) = entries
    assert handed.get(key) == CHEMISTRY.model_dump(mode="json")
    assert handed.stats() == cache.stats()


PAGE_KEY = llm_cache_key(llm_page.PROMPT_VERSION, llm_page.TASK, chunk_of())
"""The cache key of the reading of the one page these tests read."""

READ_DAY = date(2026, 11, 10)


class TestPageReadings:
    def test_without_a_provider_a_stored_reading_still_gives_its_talks(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = FakeLLMCache({PAGE_KEY: llm_page.cache_value(READ_DAY, answer(PAGE_TALK))})
        install(monkeypatch, Parts(cache))
        extractor = build(app_config).page_extractor
        assert extractor is not None
        [draft] = extractor.extract(payload(), SOURCE)
        assert draft.title == PAGE_TALK["title"]

    def test_a_page_without_a_stored_reading_gives_nothing_and_asks_nothing(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = FakeLLMCache()
        parts = install(monkeypatch, Parts(cache))
        extractor = build(app_config).page_extractor
        assert extractor is not None
        assert extractor.extract(payload(), SOURCE) == []
        assert cache.writes == 0
        (client,) = parts.calls["page_extractor"]["args"]
        assert client.name == NO_PROVIDER
        assert isinstance(parts.calls["page_extractor"]["kwargs"]["cache"], wiring.ReadOnlyCache)

    def test_no_credential_reads_what_a_refused_credential_reads(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        directory = tmp_path / "llm_cache"
        cache = open_llm_cache(directory, ttl_days=180, now=REFERENCE_NOW)
        cache.put(PAGE_KEY, llm_page.cache_value(READ_DAY, answer(PAGE_TALK)))
        before = sorted(path.relative_to(directory) for path in directory.rglob("*"))
        refused = FakeLLMClient().fail(LLMUnavailableError("401"), times=5)
        with_refused = llm_page.build_page_extractor(refused, config=app_config.llm, cache=cache)
        install(monkeypatch, Parts(cache))
        without = build(app_config).page_extractor
        assert without is not None
        other_page = PAGE.replace("Upcoming seminars", "Seminars this term")
        for text in (PAGE, other_page):
            assert without.extract(payload(text), SOURCE) == with_refused.extract(
                payload(text), SOURCE
            )
        assert [draft.title for draft in without.extract(payload(), SOURCE)] == [PAGE_TALK["title"]]
        after = sorted(path.relative_to(directory) for path in directory.rglob("*"))
        assert after == before
