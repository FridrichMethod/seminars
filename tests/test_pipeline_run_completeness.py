"""Whole runs in which a source delivers events but not all of them.

A detail page that fails drops its talk at some adapters, and a listing can
lose most of its items (pages that come back empty, a renamed field). The
talks that are missing for that reason are real: three such runs must not
delete them.
"""

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
    FakeIssueClient,
    FakePicksScorer,
)
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig
from seminars.http import HttpClient
from seminars.models import EventDraft, RawPayload, RunReport, SourceConfig, TimeWindow
from seminars.outputs.health import source_issue_title, update_health
from seminars.pipeline.run import RunOptions, healthy_sources, run_pipeline
from seminars.pipeline.secret_scan import build_secret_scanner
from seminars.pipeline.services import Services
from seminars.pipeline.store import read_events_file
from seminars.sources.base import AdapterRegistry, make_payload
from seminars.sources.generic.common import fetch_optional, get_document

URL = "https://slac.example.org/events"
START = datetime(2026, 12, 7, 16, 0, tzinfo=PACIFIC)
TALKS = ("1", "2", "3", "4")


class DetailAdapter(FakeAdapter):
    """Cards on a list page; a card whose detail page cannot be fetched gives no draft."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        listing = get_document(http, source, URL, "list")
        details = fetch_optional(http, source, [f"{URL}/{talk}" for talk in TALKS], "detail")
        return make_payload(source, [listing, *details], window=window)

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        pages = {document.url for document in payload.labelled("detail")}
        return [
            make_draft(
                source_id=source.id,
                native_id=talk,
                url=f"{URL}/{talk}",
                title=f"SLAC Colloquium talk {talk} on accelerators",
                speaker_text=f"Speaker {talk}",
                start=START + timedelta(days=int(talk)),
                end=None,
            )
            for talk in TALKS
            if f"{URL}/{talk}" in pages
        ]


class Site:
    """The pages of the source; ``broken`` detail pages answer 500."""

    def __init__(self) -> None:
        self.broken: set[str] = set()

    def client(self, source: SourceConfig) -> HttpClient:
        http = FakeHttpClient()
        http.add(URL, "list")
        for talk in TALKS:
            http.add(f"{URL}/{talk}", "detail", status=500 if talk in self.broken else 200)
        return http


def services(site: Site, issues: FakeIssueClient | None = None) -> Services:
    emitters = FakeEmitters()
    return Services(
        http_for=site.client,
        event_filter=FakeEventFilter(),
        field_tagger=FakeFieldTagger(),
        picks_scorer=FakePicksScorer(),
        write_ics_feeds=emitters.write_ics_feeds,
        write_site_data=emitters.write_site_data,
        update_health=update_health,
        secret_scanner=build_secret_scanner(),
        issues=issues,
    )


def run(config: AppConfig, bundle: Services, day: int) -> RunReport:
    return run_pipeline(
        config,
        bundle,
        now=REFERENCE_NOW + timedelta(days=day),
        options=RunOptions(),
        timer=FakeClock(),
    )


def stored_keys(config: AppConfig) -> list[str]:
    events = read_events_file(config.paths.events_file)
    return sorted(key for event in events for key in event.source_ids)


class TestADetailPageThatFails:
    @pytest.fixture
    def config(self, app_config: AppConfig, fake_registry: AdapterRegistry) -> AppConfig:
        DetailAdapter().register(fake_registry, "details")
        source = make_source(
            id="bayarea-slac",
            group="bayarea",
            adapter="details",
            url=URL,
            default_fields=["physics"],
        )
        return dataclasses.replace(app_config, sources=(source,))

    def test_its_talk_is_kept_while_the_page_fails(
        self, config: AppConfig, fake_issues: FakeIssueClient
    ) -> None:
        site = Site()
        bundle = services(site, fake_issues)
        run(config, bundle, 0)
        everything = stored_keys(config)
        assert len(everything) == 4
        site.broken = {"3"}
        reports = [run(config, bundle, day) for day in (1, 2, 3, 4)]
        assert stored_keys(config) == everything
        last = reports[-1]
        assert last.sources[0].status == "ok"
        assert (last.sources[0].drafts, last.sources[0].skipped_pages) == (3, 1)
        assert (
            "source bayarea-slac was not read in full; the events it did not deliver are not "
            "counted as missing"
        ) in last.warnings
        assert last.counts.removed == 0
        assert [issue.title for issue in fake_issues.issues] == [source_issue_title("bayarea-slac")]
        site.broken = set()
        back = run(config, bundle, 5)
        assert back.warnings == []
        assert stored_keys(config) == everything
        assert fake_issues.get(1).state == "closed"

    def test_the_source_counts_again_once_it_is_read_in_full(self, config: AppConfig) -> None:
        site = Site()
        bundle = services(site)
        run(config, bundle, 0)
        site.broken = {"3"}
        partial = run(config, bundle, 1)
        assert healthy_sources(partial.sources) == frozenset()
        site.broken = set()
        whole = run(config, bundle, 2)
        assert healthy_sources(whole.sources) == {"bayarea-slac"}


class TestAListingThatShrinks:
    @pytest.fixture
    def config(self, app_config: AppConfig, fake_adapter: FakeAdapter) -> AppConfig:
        source = make_source(id="localist-stanford", group="localist", adapter="fake", url=URL)
        return dataclasses.replace(app_config, sources=(source,))

    def drafts(self, count: int) -> list[EventDraft]:
        return [
            make_draft(
                source_id="localist-stanford",
                native_id=str(number),
                url=f"{URL}/{number}",
                title=f"Seminar number {number} on protein design",
                speaker_text=f"Speaker {number}",
                start=START + timedelta(hours=number),
                end=None,
            )
            for number in range(count)
        ]

    def test_its_missing_talks_are_kept(self, config: AppConfig, fake_adapter: FakeAdapter) -> None:
        bundle = services(Site())
        fake_adapter.drafts = self.drafts(20)
        for day in (0, 1, 2):
            run(config, bundle, day)
        assert len(stored_keys(config)) == 20
        fake_adapter.drafts = self.drafts(5)
        reports: list[Any] = [run(config, bundle, day) for day in (3, 4, 5, 6)]
        assert len(stored_keys(config)) == 20
        assert reports[-1].sources[0].shrank_from == 20
        assert reports[-1].counts.removed == 0
        assert any("was not read in full" in warning for warning in reports[-1].warnings)
