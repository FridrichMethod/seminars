"""The status page and the digest say when a run without a provider applied stored answers.

Without an LLM provider the tagger and the page extractor still apply the
answers that earlier runs stored (``LLMUsage.stored_answers_only``). Such a
run is no rules-only run, and the two pages that describe the last run say
how many stored answers it used: the status page reads it from
data/last_run.json (``LastRun.llm_stored_answers``), the digest from
data/health.json (``HealthFile.llm_last_run``).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from factories import REFERENCE_NOW
from fakes import FakeAdapter
from fakes_llm import FakeLLMCache, FakeLLMFieldTagger
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig
from seminars.models import (
    HealthFile,
    LastRun,
    LLMHealthEntry,
    LLMRunEntry,
    LLMUsage,
    PublicStatusPayload,
    RunReport,
    read_model,
)
from seminars.outputs.site_data import write_site_data
from seminars.pipeline import services as wiring
from seminars.pipeline.store import load_state
from site_sample import GENERATED_AT
from test_digest import render, section, with_sources, zh
from test_pipeline_run import config, draft, run, services
from test_pipeline_run_stored_answers import used

__all__ = ["config"]  # the fixture of test_pipeline_run

HEALTH = "Source health"
STORED = LLMUsage(stored_answers_only=True, cache_hits=4)
PROVIDER = LLMUsage(enabled=True, provider="claude-cli", calls=2)


def report(llm: LLMUsage) -> RunReport:
    return RunReport(
        started_at=REFERENCE_NOW, finished_at=REFERENCE_NOW, mode="live", ok=True, llm=llm
    )


class TestTheRecords:
    def test_the_last_run_counts_the_stored_answers_of_a_run_without_a_provider(self) -> None:
        last = report(STORED).to_last_run()
        assert (last.llm_provider, last.llm_stored_answers) == (None, 4)

    def test_a_run_whose_provider_was_not_needed_counts_its_stored_answers(self) -> None:
        cached = LLMUsage(enabled=True, cache_hits=5)  # every answer was stored already
        assert report(cached).to_last_run().llm_stored_answers == 5

    def test_a_run_with_a_provider_or_with_rules_only_counts_none(self) -> None:
        assert report(PROVIDER.evolve(cache_hits=5)).to_last_run().llm_stored_answers is None
        assert report(LLMUsage()).to_last_run().llm_stored_answers is None
        nothing = LLMUsage(stored_answers_only=True)  # no provider, and no stored answer fit
        assert report(nothing).to_last_run().llm_stored_answers is None

    def test_the_health_file_keeps_what_the_llm_of_the_last_run_used(self) -> None:
        health = HealthFile().with_llm_run(REFERENCE_NOW, STORED)
        assert health.llm_last_run == LLMRunEntry(at=REFERENCE_NOW, stored_answers=4)
        later = REFERENCE_NOW + timedelta(days=1)
        health = health.with_llm_run(later, PROVIDER)
        assert health.llm_last_run == LLMRunEntry(at=later, provider="claude-cli")
        assert health.with_llm_run(later, LLMUsage()).llm_last_run == LLMRunEntry(at=later)

    def test_a_record_of_a_run_that_did_not_finish_is_dropped(self) -> None:
        later = REFERENCE_NOW + timedelta(hours=1)
        health = HealthFile().with_llm_run(later, STORED)
        assert health.until(REFERENCE_NOW).llm_last_run is None
        assert health.until(later).llm_last_run == health.llm_last_run

    def test_a_run_stores_the_record_with_its_health(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1")]
        bundle = services(
            FakeEmitters(),
            llm_tagger=FakeLLMFieldTagger(),
            read_only_cache=wiring.ReadOnlyCache(used(FakeLLMCache(), 3)),
        )
        run(config, bundle)
        state = load_state(config.paths)
        assert state.health.llm_last_run == LLMRunEntry(at=REFERENCE_NOW, stored_answers=3)
        assert state.last_run is not None
        assert state.last_run.llm_stored_answers == 3


class TestTheStatusPayload:
    def test_it_carries_the_count_of_the_last_run(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        last = LastRun(
            started_at=REFERENCE_NOW,
            finished_at=REFERENCE_NOW,
            ok=True,
            mode="live",
            llm_stored_answers=4,
        )
        paths = write_site_data(
            [], HealthFile(), last, tmp_path, config=app_config, generated_at=GENERATED_AT
        )
        status = read_model(paths[1], PublicStatusPayload)
        assert status is not None
        assert (status.llm_provider, status.llm_stored_answers) == (None, 4)


def last_sentence(config: AppConfig, health: HealthFile) -> str:
    return section(render(config, health=health), HEALTH)[-1]


class TestTheDigest:
    def test_a_run_without_an_answering_provider_says_how_many_stored_answers_it_used(
        self, app_config: AppConfig
    ) -> None:
        config = with_sources(app_config)
        health = HealthFile().with_llm_run(REFERENCE_NOW, STORED)
        assert last_sentence(config, health) == "\n".join(
            [
                "No LLM provider answered; field tagging used 4 answer(s) stored by earlier runs.",
                zh("digest.health.llm_stored", count=4),
            ]
        )

    def test_a_run_with_rules_only_after_one_with_a_provider_is_rules_only(
        self, app_config: AppConfig
    ) -> None:
        config = with_sources(app_config)
        entry = LLMHealthEntry(at=REFERENCE_NOW, status="ok", calls=3)
        health = HealthFile().with_llm_entry("claude-cli", entry)
        assert last_sentence(config, health).startswith("Field tagging used the LLM provider")
        later = REFERENCE_NOW + timedelta(days=1)
        health = health.with_llm_run(later, LLMUsage())
        assert last_sentence(config, health).startswith("Field tagging used rules only")

    def test_the_provider_of_the_last_run_is_named(self, app_config: AppConfig) -> None:
        config = with_sources(app_config)
        health = HealthFile().with_llm_run(REFERENCE_NOW, PROVIDER)
        assert last_sentence(config, health).startswith(
            "Field tagging used the LLM provider claude-cli."
        )
