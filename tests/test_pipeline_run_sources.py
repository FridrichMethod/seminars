"""Tests for whole runs in which a source of the stored events is down, disabled or removed."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from typing import Any

import pytest

from factories import PACIFIC, REFERENCE_NOW, make_draft, make_source
from fakes import (
    FakeAdapter,
    FakeClock,
    FakeEventFilter,
    FakeFieldTagger,
    FakeHttpClient,
    FakePicksScorer,
)
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig
from seminars.http import HttpStatusError
from seminars.models import Event, EventDraft, RunReport, SourceConfig
from seminars.pipeline.diff import visible_content
from seminars.pipeline.run import RunOptions, run_pipeline
from seminars.pipeline.secret_scan import build_secret_scanner
from seminars.pipeline.services import Services
from seminars.pipeline.store import read_events_file
from seminars.sources.base import AdapterRegistry

A_URL = "https://a.example.edu/events"
B_URL = "https://b.example.edu/events"
START = datetime(2026, 11, 18, 16, 0, tzinfo=PACIFIC)


def source(source_id: str, url: str, adapter: str, **overrides: Any) -> SourceConfig:
    return make_source(id=source_id, url=url, adapter=adapter, **overrides)


def draft(source_id: str, native_id: str, **overrides: Any) -> EventDraft:
    base = A_URL if source_id == "sites-a" else B_URL
    values: dict[str, Any] = {
        "source_id": source_id,
        "native_id": native_id,
        "url": f"{base}/{native_id}",
        "title": f"Talk number {native_id} on protein folding",
        "speaker_text": f"Speaker {native_id.upper()} Person",
        "start": START + timedelta(days=int(native_id)),
        "end": None,
    }
    return make_draft(**{**values, **overrides})


def services(**overrides: Any) -> Services:
    emitters = FakeEmitters()
    values: dict[str, Any] = {
        "http_for": lambda chosen: FakeHttpClient(),
        "event_filter": FakeEventFilter(),
        "field_tagger": FakeFieldTagger(),
        "picks_scorer": FakePicksScorer(),
        "write_ics_feeds": emitters.write_ics_feeds,
        "write_site_data": emitters.write_site_data,
        "update_health": emitters.update_health,
        "secret_scanner": build_secret_scanner(),
    }
    return Services(**{**values, **overrides})


def run(config: AppConfig, day: int) -> RunReport:
    return run_pipeline(
        config,
        services(),
        now=REFERENCE_NOW + timedelta(days=day),
        options=RunOptions(),
        timer=FakeClock(),
    )


def stored(config: AppConfig) -> list[Event]:
    return list(read_events_file(config.paths.events_file))


class Adapters:
    def __init__(self, registry: AdapterRegistry) -> None:
        self.a = FakeAdapter().register(registry, "adapter_a")
        self.b = FakeAdapter().register(registry, "adapter_b")


@pytest.fixture
def adapters(fake_registry: AdapterRegistry) -> Adapters:
    return Adapters(fake_registry)


@pytest.fixture
def config(app_config: AppConfig) -> AppConfig:
    sources = (source("sites-a", A_URL, "adapter_a"), source("sites-b", B_URL, "adapter_b"))
    return dataclasses.replace(app_config, sources=sources)


class TestAMergedTalkWhileOneSourceIsDown:
    def listings(self, adapters: Adapters) -> None:
        shared = {"speaker_text": "Ada Lovelace", "start": START}
        adapters.a.drafts = [
            draft(
                "sites-a",
                "1",
                title="Probability Seminar: random walks on groups",
                location="Sloan Hall 380",
                abstract="A long abstract from the department page.",
                **shared,
            )
        ]
        adapters.b.drafts = [draft("sites-b", "9", title="TBA", location=None, **shared)]

    def test_failure_and_recovery_leave_the_event_as_it_was(
        self, config: AppConfig, adapters: Adapters
    ) -> None:
        self.listings(adapters)
        first = run(config, 0)
        assert first.counts.merged == 1
        (before,) = stored(config)
        adapters.a.fetch_error = HttpStatusError(A_URL, 500)
        down = run(config, 1)
        assert down.sources[0].status == "failed"
        (during,) = stored(config)
        adapters.a.fetch_error = None
        back = run(config, 2)
        (after,) = stored(config)
        for event in (during, after):
            assert visible_content(event) == visible_content(before)
            assert (event.uid, event.sequence) == (before.uid, before.sequence)
        assert (down.counts.changed, back.counts.changed) == (0, 0)
        assert sorted(during.source_ids) == ["sites-a:1", "sites-b:9"]


class TestRemovedAndDisabledSources:
    def test_the_events_of_a_removed_source_leave_at_the_next_run(
        self, config: AppConfig, adapters: Adapters
    ) -> None:
        adapters.a.drafts = [draft("sites-a", "1")]
        adapters.b.drafts = [draft("sites-b", "2")]
        run(config, 0)
        assert len(stored(config)) == 2
        without_b = dataclasses.replace(config, sources=config.sources[:1])
        report = run(without_b, 1)
        assert [event.source_ids for event in stored(without_b)] == [["sites-a:1"]]
        assert report.counts.removed == 1

    def test_a_disabled_source_is_like_a_removed_one(
        self, config: AppConfig, adapters: Adapters
    ) -> None:
        adapters.a.drafts = [draft("sites-a", "1")]
        adapters.b.drafts = [draft("sites-b", "2")]
        run(config, 0)
        disabled = dataclasses.replace(
            config, sources=(config.sources[0], config.sources[1].evolve(enabled=False))
        )
        run(disabled, 1)
        assert [event.source_ids for event in stored(disabled)] == [["sites-a:1"]]

    def test_a_merged_talk_the_remaining_source_drops_is_removed_after_three_runs(
        self, config: AppConfig, adapters: Adapters
    ) -> None:
        shared = {"title": "Talk number 1 on protein folding", "speaker_text": "Ada Lovelace"}
        adapters.a.drafts = [draft("sites-a", "1", **shared), draft("sites-a", "2")]
        adapters.b.drafts = [draft("sites-b", "9", start=START + timedelta(days=1), **shared)]
        run(config, 0)
        assert len(stored(config)) == 2
        without_b = dataclasses.replace(config, sources=config.sources[:1])
        adapters.a.drafts = [draft("sites-a", "2")]
        counts = [len(stored(without_b)) for _ in [run(without_b, day) for day in (1, 2, 3)]]
        assert counts[-1] == 1
        assert [event.source_ids for event in stored(without_b)] == [["sites-a:2"]]

    def test_a_run_limited_to_one_source_concludes_nothing_about_the_other(
        self, config: AppConfig, adapters: Adapters
    ) -> None:
        adapters.a.drafts = [draft("sites-a", "1")]
        adapters.b.drafts = [draft("sites-b", "2")]
        run(config, 0)
        for day in (1, 2, 3, 4):
            run_pipeline(
                config,
                services(),
                now=REFERENCE_NOW + timedelta(days=day),
                options=RunOptions(source_ids=("sites-a",)),
                timer=FakeClock(),
            )
        assert sorted(key for event in stored(config) for key in event.source_ids) == [
            "sites-a:1",
            "sites-b:2",
        ]
