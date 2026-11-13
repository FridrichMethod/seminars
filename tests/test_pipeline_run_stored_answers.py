"""A run without an LLM provider that still applies the answers stored by earlier runs.

Without a provider the tagger and the page extractor answer from the cache
alone (``seminars.pipeline.services``). Such a run is no rules-only run:
the run report says how many stored answers it used, and so does the
command line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from factories import REFERENCE_NOW
from fakes import FakeAdapter
from fakes_llm import FakeLLMCache, FakeLLMClient, FakeLLMFieldTagger, FakePageExtractor
from fakes_outputs import FakeEmitters
from seminars import cli
from seminars.config import AppConfig
from seminars.models import LLMCacheStats, LLMUsage, RunReport
from seminars.pipeline import services as wiring
from test_pipeline_run import config, draft, run, services
from test_pipeline_services_cached_verdicts import Parts, build, install

__all__ = ["config"]  # the fixture of test_pipeline_run


def used(cache: FakeLLMCache, hits: int) -> FakeLLMCache:
    """``cache`` after ``hits`` lookups that it answered."""
    cache.put("stored", {"fields": ["bio"]})
    for _ in range(hits):
        cache.get("stored")
    return cache


class TestTheRunReport:
    def test_the_stored_answers_that_were_used_are_counted(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1")]
        cache = used(FakeLLMCache(), 3)
        bundle = services(
            FakeEmitters(),
            llm_tagger=FakeLLMFieldTagger(),
            page_extractor=FakePageExtractor(),
            read_only_cache=wiring.ReadOnlyCache(cache),
        )
        report = run(config, bundle)
        assert report.llm == LLMUsage(stored_answers_only=True, cache_hits=3)
        assert report.warnings == []
        assert cache.prunes == 0

    @pytest.mark.parametrize("part", ["llm_tagger", "page_extractor"])
    def test_either_part_without_a_provider_is_reported_so(
        self, config: AppConfig, fake_adapter: FakeAdapter, part: str
    ) -> None:
        tagger_or_extractor = FakeLLMFieldTagger() if part == "llm_tagger" else FakePageExtractor()
        report = run(config, services(FakeEmitters(), **{part: tagger_or_extractor}))
        assert report.llm == LLMUsage(stored_answers_only=True)

    def test_rules_only_is_no_such_run(self, config: AppConfig, fake_adapter: FakeAdapter) -> None:
        report = run(config, services(FakeEmitters()))
        assert report.llm == LLMUsage()

    def test_unreadable_counters_are_a_warning(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        class Unreadable(wiring.ReadOnlyCache):
            def stats(self) -> LLMCacheStats:
                raise RuntimeError("no counters")

        bundle = services(
            FakeEmitters(),
            llm_tagger=FakeLLMFieldTagger(),
            read_only_cache=Unreadable(FakeLLMCache()),
        )
        report = run(config, bundle)
        assert report.llm == LLMUsage(stored_answers_only=True)
        assert report.warnings == ["reading the LLM cache counters failed (RuntimeError)"]


class TestTheCommandLine:
    def test_the_llm_line_says_how_many_stored_answers_were_used(self) -> None:
        report = cli_report(LLMUsage(stored_answers_only=True, cache_hits=4))
        assert "llm: no provider, 4 stored answers used" in cli.report_lines(report)

    def test_rules_only_is_still_said_as_before(self) -> None:
        assert "llm: off (rules only)" in cli.report_lines(cli_report(LLMUsage()))


class TestTheServices:
    def test_without_a_provider_the_read_only_cache_is_handed_to_the_run(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cache = FakeLLMCache()
        install(monkeypatch, Parts(cache))
        bundle = build(app_config, llm_cache_dir=tmp_path / "cache")
        assert isinstance(bundle.read_only_cache, wiring.ReadOnlyCache)
        assert bundle.read_only_cache.cache is cache
        assert bundle.llm_cache is None

    def test_with_a_provider_there_is_none(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        install(monkeypatch, Parts(FakeLLMCache(), chain=FakeLLMClient()))
        bundle = build(app_config, llm_cache_dir=tmp_path / "cache")
        assert bundle.read_only_cache is None

    def test_rules_only_on_request_has_none(
        self, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, Parts(FakeLLMCache()))
        assert build(app_config, use_llm=False).read_only_cache is None


def cli_report(llm: LLMUsage) -> RunReport:
    return RunReport(
        started_at=REFERENCE_NOW, finished_at=REFERENCE_NOW, mode="live", ok=True, llm=llm
    )
