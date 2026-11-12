"""Tests for telling a source that was read in full from one that was not.

A source can deliver events and still miss some: a detail page that could
not be fetched (its card is dropped), or a listing that shrank sharply
against its recent history (pages missing, items silently skipped). Such a
run says nothing about the events the source did not deliver.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from factories import REFERENCE_NOW, make_draft, make_source
from fakes import FakeAdapter, FakeClock, FakeHttpClient
from seminars.config import AppConfig
from seminars.http import HttpClient
from seminars.models import (
    EventDraft,
    RawPayload,
    SourceConfig,
    SourceHealth,
    SourceHealthEntry,
    SourceRunResult,
    TimeWindow,
)
from seminars.pipeline.collect import (
    SHRINK_LOOKBACK_RUNS,
    SHRINK_MIN_DROP,
    SHRINK_MIN_RUNS,
    collect_sources,
    compare_with_history,
)
from seminars.pipeline.normalize import NormalizeContext
from seminars.sources.base import AdapterRegistry, make_payload
from seminars.sources.generic.common import fetch_optional, get_document

URL = "https://example.edu/events"


@pytest.fixture
def context(app_config: AppConfig) -> NormalizeContext:
    settings = app_config.settings
    return NormalizeContext(
        settings=settings, now=REFERENCE_NOW, window=settings.window_at(REFERENCE_NOW)
    )


class DetailAdapter(FakeAdapter):
    """A list page plus one detail page per talk; a talk without its detail page is dropped."""

    def __init__(self, talks: list[str]) -> None:
        super().__init__()
        self.talks = talks

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        listing = get_document(http, source, URL, "list")
        details = fetch_optional(http, source, [f"{URL}/{talk}" for talk in self.talks], "detail")
        return make_payload(source, [listing, *details], window=window)

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        pages = {document.url for document in payload.labelled("detail")}
        return [
            make_draft(
                source_id=source.id,
                native_id=talk,
                url=f"{URL}/{talk}",
                title=f"Talk {talk} on protein design",
            )
            for talk in self.talks
            if f"{URL}/{talk}" in pages
        ]


def site(broken: tuple[str, ...] = ()) -> FakeHttpClient:
    http = FakeHttpClient()
    http.add(URL, "list")
    for talk in ("1", "2", "3", "4"):
        http.add(f"{URL}/{talk}", "detail", status=500 if talk in broken else 200)
    return http


def collect(context: NormalizeContext, http: FakeHttpClient) -> SourceRunResult:
    source = make_source(id="bayarea-slac", group="bayarea", adapter="details", url=URL)
    [outcome] = collect_sources(
        [source], lambda chosen: http, context=context, extractor=None, timer=FakeClock()
    )
    return outcome.result


class TestSkippedPages:
    def test_a_source_that_fetched_every_page_was_read_in_full(
        self, fake_registry: AdapterRegistry, context: NormalizeContext
    ) -> None:
        DetailAdapter(["1", "2", "3", "4"]).register(fake_registry, "details")
        result = collect(context, site())
        assert (result.status, result.drafts, result.skipped_pages) == ("ok", 4, 0)
        assert result.read_in_full
        assert result.warnings == []

    def test_a_detail_page_that_failed_means_the_source_was_not_read_in_full(
        self, fake_registry: AdapterRegistry, context: NormalizeContext
    ) -> None:
        DetailAdapter(["1", "2", "3", "4"]).register(fake_registry, "details")
        result = collect(context, site(broken=("3",)))
        assert (result.status, result.drafts, result.skipped_pages) == ("ok", 3, 1)
        assert not result.read_in_full
        assert result.warnings == [
            "1 page(s) could not be fetched and were left out: the source was not read in full"
        ]


def full(accepted: int, days: int = 0, **extra: Any) -> SourceHealthEntry:
    moment = REFERENCE_NOW - timedelta(days=days)
    return SourceHealthEntry(at=moment, status="ok", events=accepted, accepted=accepted, **extra)


def history(*entries: SourceHealthEntry) -> SourceHealth:
    return SourceHealth(history=list(entries))


def outcome(accepted: int, **extra: Any) -> SourceRunResult:
    return SourceRunResult(
        source_id="localist-stanford", status="ok", drafts=accepted, events=accepted, **extra
    )


class TestShrunkListings:
    def test_a_listing_that_lost_most_of_its_items_is_not_complete(self) -> None:
        judged = compare_with_history(outcome(72), history(full(188), full(190), full(185)))
        assert judged.shrank_from == 188
        assert not judged.read_in_full
        assert judged.warnings == [
            "the source parsed 72 usable draft(s) where it usually has 188: "
            "the listing is taken as incomplete"
        ]

    def test_ordinary_ups_and_downs_are_fine(self) -> None:
        past = history(full(56), full(60), full(52))
        assert compare_with_history(outcome(40), past) == outcome(40)

    def test_a_small_source_losing_one_talk_is_not_judged_by_its_count(self) -> None:
        past = history(full(4), full(4), full(4))
        assert SHRINK_MIN_DROP > 1
        assert compare_with_history(outcome(3), past).read_in_full
        assert compare_with_history(outcome(1), past).read_in_full

    def test_without_enough_full_reads_nothing_is_judged(self) -> None:
        few = history(*[full(188) for _ in range(SHRINK_MIN_RUNS - 1)])
        assert compare_with_history(outcome(7), few).read_in_full
        assert compare_with_history(outcome(7), None).read_in_full

    def test_runs_that_were_not_complete_are_no_measure(self) -> None:
        shrunk = [full(7, shrank_from=188) for _ in range(5)]
        skipped = full(150, skipped_pages=2)
        past = history(full(188), full(188), full(188), *shrunk, skipped)
        assert compare_with_history(outcome(7), past).shrank_from == 188

    def test_entries_without_a_count_are_no_measure(self) -> None:
        old = SourceHealthEntry(at=REFERENCE_NOW, status="ok", events=188)
        assert compare_with_history(outcome(7), history(old, old, old)).read_in_full

    def test_a_lasting_new_size_becomes_the_measure(self) -> None:
        entries = [full(188), full(188), full(188)]
        flagged = 0
        for _ in range(2 * SHRINK_LOOKBACK_RUNS):
            judged = compare_with_history(outcome(20), history(*entries[-30:]))
            flagged += not judged.read_in_full
            entries.append(
                full(20, shrank_from=judged.shrank_from)
                if judged.shrank_from is not None
                else full(20)
            )
        assert flagged == SHRINK_LOOKBACK_RUNS - SHRINK_MIN_RUNS + 1

    def test_an_empty_or_failed_run_is_left_to_the_other_rules(self) -> None:
        past = history(full(188), full(188), full(188))
        empty = SourceRunResult(source_id="localist-stanford", status="empty")
        failed = SourceRunResult(source_id="localist-stanford", status="failed", error="x")
        assert compare_with_history(empty, past) == empty
        assert compare_with_history(failed, past) == failed
