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
    HEALTH_HISTORY_LIMIT,
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

    def test_an_empty_or_failed_run_is_left_to_the_other_rules(self) -> None:
        past = history(full(188), full(188), full(188))
        empty = SourceRunResult(source_id="localist-stanford", status="empty")
        failed = SourceRunResult(source_id="localist-stanford", status="failed", error="x")
        assert compare_with_history(empty, past) == empty
        assert compare_with_history(failed, past) == failed


def shrunk(accepted: int, usual: int) -> SourceHealthEntry:
    return full(accepted, shrank_from=usual)


def failed() -> SourceHealthEntry:
    return SourceHealthEntry(at=REFERENCE_NOW, status="failed", error="HttpStatusError: HTTP 500")


def recorded(entries: list[SourceHealthEntry], judged: SourceRunResult) -> list[SourceHealthEntry]:
    """``entries`` with the run ``judged``, as the health history keeps it."""
    entry = full(judged.accepted, shrank_from=judged.shrank_from, resized_from=judged.resized_from)
    return [*entries, entry][-HEALTH_HISTORY_LIMIT:]


class TestALastingShortfall:
    def test_it_lasts_until_the_history_holds_too_few_full_reads(self) -> None:
        entries = [full(188), full(188), full(188)]
        judged: list[SourceRunResult] = []
        for _ in range(HEALTH_HISTORY_LIMIT + 5):
            judged.append(compare_with_history(outcome(20), history(*entries)))
            entries = recorded(entries, judged[-1])
        alarm = HEALTH_HISTORY_LIMIT - SHRINK_MIN_RUNS + 1
        assert [run.shrank_from for run in judged[:alarm]] == [188] * alarm
        taken = judged[alarm]
        assert (taken.shrank_from, taken.resized_from, taken.read_in_full) == (None, 188, True)
        assert taken.warnings == [
            "the source parsed 20 usable draft(s) where it used to have 188, for so long that "
            "this is now taken as the usual size of its listing: the stored events it does not "
            "list count as missing"
        ]
        assert all(run == outcome(20) for run in judged[alarm + 1 :])

    def test_one_run_back_in_size_does_not_end_the_measure(self) -> None:
        streak = [shrunk(20, 188) for _ in range(SHRINK_LOOKBACK_RUNS)]
        past = history(full(188), full(188), full(188), *streak, full(150), shrunk(20, 188))
        assert compare_with_history(outcome(20), past).shrank_from == 188

    def test_failed_runs_do_not_end_the_measure(self) -> None:
        past = history(full(188), full(188), full(188), *[failed() for _ in range(12)])
        assert compare_with_history(outcome(20), past).shrank_from == 188

    def test_a_run_back_in_size_when_the_measure_ran_out_is_a_full_read(self) -> None:
        past = history(*[shrunk(20, 188) for _ in range(HEALTH_HISTORY_LIMIT)])
        assert compare_with_history(outcome(150), past) == outcome(150)
        assert compare_with_history(outcome(20), past).resized_from == 188

    def test_the_full_reads_before_a_new_size_are_no_measure(self) -> None:
        resized = full(20, resized_from=188)
        past = history(full(188), full(188), shrunk(20, 188), resized)
        assert compare_with_history(outcome(20), past) == outcome(20)

    def test_the_new_size_is_the_measure_once_it_was_read_in_full_often_enough(self) -> None:
        resized = full(20, resized_from=188)
        past = history(full(188), full(188), resized, full(20), full(20))
        assert compare_with_history(outcome(5), past).shrank_from == 20

    def test_without_an_earlier_shortfall_a_short_history_judges_nothing(self) -> None:
        past = history(full(188), full(188), *[failed() for _ in range(HEALTH_HISTORY_LIMIT - 2)])
        assert compare_with_history(outcome(20), past) == outcome(20)
