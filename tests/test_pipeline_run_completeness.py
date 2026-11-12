"""Whole runs in which a source delivers events but not all of them.

A detail page that fails drops its talk at some adapters, and a listing can
lose most of its items (pages that come back empty, a renamed field). The
talks that are missing for that reason are real: three such runs must not
delete them. A listing that stays short is taken at its new size in the
end, and that run says so before a talk it does not list can be deleted.
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
from seminars.models import (
    HEALTH_HISTORY_LIMIT,
    EventDraft,
    RawPayload,
    RunReport,
    SourceConfig,
    TimeWindow,
)
from seminars.outputs.health import source_issue_title, update_health
from seminars.pipeline.collect import SHRINK_MIN_RUNS
from seminars.pipeline.run import RunOptions, healthy_sources, run_pipeline
from seminars.pipeline.secret_scan import build_secret_scanner
from seminars.pipeline.services import Services
from seminars.pipeline.store import read_events_file
from seminars.sources.base import AdapterRegistry, make_payload
from seminars.sources.generic.common import fetch_optional, get_document

URL = "https://slac.example.org/events"
START = datetime(2026, 12, 7, 16, 0, tzinfo=PACIFIC)
LATER = START + timedelta(days=70)  # still ahead after weeks of daily runs
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


def listing(count: int, start: datetime = START) -> list[EventDraft]:
    return [
        make_draft(
            source_id="localist-stanford",
            native_id=str(number),
            url=f"{URL}/{number}",
            title=f"Seminar number {number} on protein design",
            speaker_text=f"Speaker {number}",
            start=start + timedelta(hours=number),
            end=None,
        )
        for number in range(count)
    ]


@pytest.fixture
def localist(app_config: AppConfig, fake_adapter: FakeAdapter) -> AppConfig:
    source = make_source(id="localist-stanford", group="localist", adapter="fake", url=URL)
    return dataclasses.replace(app_config, sources=(source,))


class TestAListingThatShrinks:
    def test_its_missing_talks_are_kept(
        self, localist: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        bundle = services(Site())
        fake_adapter.drafts = listing(20)
        for day in (0, 1, 2):
            run(localist, bundle, day)
        assert len(stored_keys(localist)) == 20
        fake_adapter.drafts = listing(5)
        reports: list[Any] = [run(localist, bundle, day) for day in (3, 4, 5, 6)]
        assert len(stored_keys(localist)) == 20
        assert reports[-1].sources[0].shrank_from == 20
        assert reports[-1].counts.removed == 0
        assert any("was not read in full" in warning for warning in reports[-1].warnings)


ALARM = HEALTH_HISTORY_LIMIT - SHRINK_MIN_RUNS + 1
"""Runs in a row that a listing which stays short is taken as incomplete."""


class TestAListingThatStaysShort:
    """A listing that does not come back is taken at its new size, and the run says so."""

    def short_runs(self, config: AppConfig, adapter: FakeAdapter, bundle: Services) -> list[Any]:
        """Three full reads of 20 talks, then ``ALARM`` runs that list 5 of them."""
        adapter.drafts = listing(20, LATER)
        for day in (0, 1, 2):
            run(config, bundle, day)
        adapter.drafts = listing(5, LATER)
        return [run(config, bundle, day) for day in range(3, 3 + ALARM)]

    def test_its_talks_are_kept_while_the_alarm_lasts(
        self, localist: AppConfig, fake_adapter: FakeAdapter, fake_issues: FakeIssueClient
    ) -> None:
        short = self.short_runs(localist, fake_adapter, services(Site(), fake_issues))
        assert [report.sources[0].shrank_from for report in short] == [20] * ALARM
        assert sum(report.counts.removed for report in short) == 0
        assert len(stored_keys(localist)) == 20
        assert [issue.state for issue in fake_issues.issues] == ["open"]
        assert fake_issues.comments == {}

    def test_the_new_size_is_taken_with_a_warning_and_its_own_comment(
        self, localist: AppConfig, fake_adapter: FakeAdapter, fake_issues: FakeIssueClient
    ) -> None:
        bundle = services(Site(), fake_issues)
        self.short_runs(localist, fake_adapter, bundle)
        taken = run(localist, bundle, 3 + ALARM)
        result = taken.sources[0]
        assert (result.shrank_from, result.resized_from, result.read_in_full) == (None, 20, True)
        assert (
            "source localist-stanford has listed far fewer events than usual for so long that "
            "its smaller listing is now taken as its usual size; the stored events it does not "
            "list count as missing"
        ) in taken.warnings
        assert [issue.state for issue in fake_issues.issues] == ["closed"]
        (comment,) = fake_issues.comments[1]
        assert "smaller size is now taken as the usual one" in comment
        assert "read in full again" not in comment
        after = [run(localist, bundle, day) for day in range(4 + ALARM, 7 + ALARM)]
        assert all(report.sources[0].read_in_full for report in after)
        assert all(report.sources[0].resized_from is None for report in after)
        assert all(report.warnings == [] for report in after)
        assert sum(report.counts.removed for report in [taken, *after]) == 15
        assert len(stored_keys(localist)) == 5
        assert len(fake_issues.issues) == 1

    def test_a_drop_after_a_partial_recovery_as_the_measure_ran_out_is_taken_with_a_warning(
        self, localist: AppConfig, fake_adapter: FakeAdapter, fake_issues: FakeIssueClient
    ) -> None:
        bundle = services(Site(), fake_issues)
        self.short_runs(localist, fake_adapter, bundle)
        fake_adapter.drafts = listing(11, LATER)  # half the usual size or more: read in full
        back = run(localist, bundle, 3 + ALARM)
        assert (back.sources[0].read_in_full, back.sources[0].resized_from) == (True, None)
        assert [issue.state for issue in fake_issues.issues] == ["closed"]
        fake_adapter.drafts = listing(5, LATER)
        dropped = run(localist, bundle, 4 + ALARM)
        assert dropped.sources[0].resized_from == 20
        assert any("now taken as its usual size" in warning for warning in dropped.warnings)
        assert dropped.counts.removed == 0


class SkippingAdapter(FakeAdapter):
    """``FakeAdapter`` that also asks for a detail page, which it goes on without."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        fetch_optional(http, source, [f"{URL}/1"], "detail")
        return super().fetch(http, source, window=window)


class TestANewSizeInARunThatSkippedAPage:
    def test_the_new_size_waits_for_a_run_read_in_full_and_closes_with_its_comment(
        self,
        app_config: AppConfig,
        fake_registry: AdapterRegistry,
        fake_clock: FakeClock,
        fake_issues: FakeIssueClient,
    ) -> None:
        adapter = SkippingAdapter(clock=fake_clock).register(fake_registry, "skipping")
        source = make_source(id="localist-stanford", group="localist", adapter="skipping", url=URL)
        config = dataclasses.replace(app_config, sources=(source,))
        site = Site()
        bundle = services(site, fake_issues)
        TestAListingThatStaysShort().short_runs(config, adapter, bundle)
        site.broken = {"1"}
        skipping = run(config, bundle, 3 + ALARM).sources[0]
        assert (skipping.skipped_pages, skipping.shrank_from, skipping.resized_from) == (
            1,
            20,
            None,
        )
        assert [issue.state for issue in fake_issues.issues] == ["open"]
        site.broken = set()
        taken = run(config, bundle, 4 + ALARM)
        assert (taken.sources[0].resized_from, taken.sources[0].read_in_full) == (20, True)
        assert [issue.state for issue in fake_issues.issues] == ["closed"]
        (comment,) = fake_issues.comments[1]
        assert "smaller size is now taken as the usual one" in comment
