"""Tests for the source stage: isolation of sources, time limit, fallback, error reports."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from factories import REFERENCE_NOW, make_document, make_draft, make_source
from fakes import FakeAdapter, FakeClock, FakeHttpClient
from fakes_llm import FakePageExtractor
from seminars.config import AppConfig
from seminars.fixtures import FixtureStore
from seminars.http import (
    CrawlDelayTooLongError,
    DeadlineExceededError,
    ForbiddenHostError,
    HostBudgetExceededError,
    HttpClient,
    HttpError,
    HttpStatusError,
    ResponseTooLargeError,
    RobotsDisallowedError,
    RobotsUnavailableError,
)
from seminars.models import EventDraft, FetchedDocument, RawPayload, SourceConfig, TimeWindow
from seminars.pipeline.clock import ClockError, current_time, run_time
from seminars.pipeline.collect import (
    MAX_ERROR_CHARS,
    BoundedHttpClient,
    Fetched,
    SourceOutcome,
    SourceTimeLimitError,
    collect_sources,
    describe_error,
    digest_source,
    error_kind,
    fetch_sources,
)
from seminars.pipeline.normalize import NormalizeContext
from seminars.pipeline.secret_scan import build_secret_scanner
from seminars.sources.base import (
    AdapterRegistry,
    ForeignHostError,
    ParseError,
    UnknownAdapterError,
    fetch_urls,
)

URL = "https://example.edu/events"


@pytest.fixture
def context(app_config: AppConfig) -> NormalizeContext:
    settings = app_config.settings
    return NormalizeContext(
        settings=settings, now=REFERENCE_NOW, window=settings.window_at(REFERENCE_NOW)
    )


def source(source_id: str, **overrides: object) -> SourceConfig:
    values: dict[str, object] = {"id": source_id, "adapter": "fake", "url": f"{URL}/{source_id}"}
    return make_source(**{**values, **overrides})


def draft(source_id: str, native_id: str = "1", **overrides: object) -> EventDraft:
    return make_draft(
        source_id=source_id, native_id=native_id, url=f"{URL}/{source_id}/{native_id}", **overrides
    )


def no_http(source: SourceConfig) -> HttpClient:
    return FakeHttpClient()


def collect(
    sources: list[SourceConfig],
    context: NormalizeContext,
    *,
    http_for: Callable[[SourceConfig], HttpClient] = no_http,
    extractor: FakePageExtractor | None = None,
    timer: Callable[[], float] | None = None,
) -> dict[str, SourceOutcome]:
    outcomes = collect_sources(
        sources, http_for, context=context, extractor=extractor, timer=timer or FakeClock()
    )
    return {outcome.result.source_id: outcome for outcome in outcomes}


class TestIsolation:
    def test_a_healthy_source_gives_candidates(
        self, fake_adapter: FakeAdapter, context: NormalizeContext
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1"), draft("sites-a", "2", title="Second talk")]
        [outcome] = collect_sources(
            [source("sites-a")], no_http, context=context, extractor=None, timer=FakeClock()
        )
        result = outcome.result
        assert (result.status, result.drafts, result.events, result.error) == ("ok", 2, 2, None)
        assert result.extraction == "feed"
        assert [c.event.source_ids for c in outcome.candidates] == [
            ["sites-a:1"],
            ["sites-a:2"],
        ]

    def test_a_source_without_events_is_empty(
        self, fake_adapter: FakeAdapter, context: NormalizeContext
    ) -> None:
        [outcome] = collect_sources(
            [source("sites-a")], no_http, context=context, extractor=None, timer=FakeClock()
        )
        assert (outcome.result.status, outcome.result.extraction) == ("empty", None)

    def test_one_failing_source_does_not_stop_the_others(
        self, fake_registry: AdapterRegistry, context: NormalizeContext
    ) -> None:
        FakeAdapter([draft("sites-b")]).register(fake_registry, "fake")
        FakeAdapter(fetch_error=HttpStatusError(f"{URL}/x", 503)).register(fake_registry, "down")
        sources = [source("sites-a", adapter="down"), source("sites-b"), source("sites-c")]
        outcomes = collect_sources(
            sources, no_http, context=context, extractor=None, timer=FakeClock()
        )
        assert [(o.result.source_id, o.result.status) for o in outcomes] == [
            ("sites-a", "failed"),
            ("sites-b", "ok"),
            ("sites-c", "empty"),
        ]
        failed = outcomes[0].result
        assert failed.error_kind == "http"
        assert failed.error is not None
        assert failed.error.startswith("HttpStatusError: ")

    def test_an_unknown_adapter_is_a_configuration_error(
        self, fake_registry: AdapterRegistry, context: NormalizeContext
    ) -> None:
        [outcome] = collect_sources(
            [source("sites-a", adapter="nowhere")],
            no_http,
            context=context,
            extractor=None,
            timer=FakeClock(),
        )
        assert (outcome.result.status, outcome.result.error_kind) == ("failed", "config")

    def test_a_bug_in_an_adapter_is_an_internal_error(
        self, fake_registry: AdapterRegistry, context: NormalizeContext
    ) -> None:
        FakeAdapter(fetch_error=RuntimeError("oops")).register(fake_registry, "fake")
        [outcome] = collect_sources(
            [source("sites-a")], no_http, context=context, extractor=None, timer=FakeClock()
        )
        assert outcome.result.error_kind == "internal"
        assert outcome.result.error == "RuntimeError: oops"

    def test_a_payload_of_another_source_is_refused(
        self, fake_registry: AdapterRegistry, context: NormalizeContext
    ) -> None:
        class Impostor(FakeAdapter):
            def fetch(
                self, http: HttpClient, source: SourceConfig, *, window: TimeWindow
            ) -> RawPayload:
                payload = super().fetch(http, source, window=window)
                return payload.evolve(source_id="sites-other")

        Impostor().register(fake_registry, "fake")
        [outcome] = collect_sources(
            [source("sites-a")], no_http, context=context, extractor=None, timer=FakeClock()
        )
        assert (outcome.result.status, outcome.result.error_kind) == ("failed", "internal")
        assert "did not return a payload of its source" in (outcome.result.error or "")

    def test_a_parse_error_fails_the_source(
        self, fake_registry: AdapterRegistry, context: NormalizeContext
    ) -> None:
        FakeAdapter(parse_error=ParseError("not a feed")).register(fake_registry, "fake")
        [outcome] = collect_sources(
            [source("sites-a")], no_http, context=context, extractor=None, timer=FakeClock()
        )
        assert (outcome.result.error_kind, outcome.result.error) == (
            "parse",
            "ParseError: not a feed",
        )

    def test_the_outcomes_follow_the_order_of_the_sources(
        self, fake_adapter: FakeAdapter, context: NormalizeContext
    ) -> None:
        names = [f"sites-{letter}" for letter in "hgfedcba"]
        fake_adapter.drafts = [draft(name) for name in names]
        outcomes = collect_sources(
            [source(name) for name in names],
            no_http,
            context=context,
            extractor=None,
            timer=FakeClock(),
        )
        assert [o.result.source_id for o in outcomes] == names

    def test_bad_drafts_and_drafts_outside_the_window_are_counted(
        self, fake_adapter: FakeAdapter, context: NormalizeContext
    ) -> None:
        far = REFERENCE_NOW + timedelta(days=400)
        fake_adapter.drafts = [
            draft("sites-a", "1"),
            draft("sites-a", "2", start=far, end=None),
            draft("sites-a", "3", start="whenever", end=None),
        ]
        [outcome] = collect_sources(
            [source("sites-a")], no_http, context=context, extractor=None, timer=FakeClock()
        )
        result = outcome.result
        assert (result.drafts, result.events, result.outside_window, result.invalid) == (3, 1, 1, 1)
        assert [item.reason for item in outcome.invalid] == ["invalid"]

    def test_no_sources_fetch_nothing(self) -> None:
        window = TimeWindow.around(
            REFERENCE_NOW, days_back=1, days_ahead=1, timezone="America/Los_Angeles"
        )
        assert fetch_sources([], no_http, window=window, timer=FakeClock()) == []

    def test_a_fetch_without_payload_or_error_is_a_failure(self, context: NormalizeContext) -> None:
        outcome = digest_source(
            Fetched(source("sites-a")), context=context, extractor=None, timer=FakeClock()
        )
        assert (outcome.result.status, outcome.result.error_kind) == ("failed", "internal")


class SlowHttp:
    """Every request takes ``seconds`` on the fake clock."""

    def __init__(self, clock: FakeClock, seconds: float) -> None:
        self._clock = clock
        self._seconds = seconds
        self._inner = FakeHttpClient(clock=clock)
        self.requests: list[str] = []

    def add(self, url: str) -> None:
        self._inner.add(url, "<html></html>")

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        allow_statuses: Collection[int] = (),
    ) -> FetchedDocument:
        self.requests.append(url)
        self._clock.advance(self._seconds)
        return self._inner.get(url)


class PagingAdapter:
    """Fetches every URL of its source, one after the other; parses nothing."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        return fetch_urls(http, source, window=window)

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        return []


class TestTimeLimit:
    def test_requests_are_counted_and_refused_once_the_time_is_used_up(self) -> None:
        clock = FakeClock()
        inner = SlowHttp(clock, 400.0)
        for number in range(4):
            inner.add(f"{URL}/{number}")
        client = BoundedHttpClient(inner, limit=900.0, timer=clock)
        for number in range(3):
            client.get(f"{URL}/{number}")
        with pytest.raises(SourceTimeLimitError) as caught:
            client.get(f"{URL}/3")
        assert isinstance(caught.value, HttpError)
        assert client.requests == 3
        assert inner.requests == [f"{URL}/0", f"{URL}/1", f"{URL}/2"]

    def test_a_slow_source_fails_alone(
        self,
        fake_registry: AdapterRegistry,
        fake_adapter: FakeAdapter,
        context: NormalizeContext,
    ) -> None:
        fake_registry.register("paging")(PagingAdapter)
        clock = FakeClock()
        slow = SlowHttp(clock, 400.0)
        urls = [f"{URL}/slow/{number}" for number in range(5)]
        for url in urls:
            slow.add(url)
        fake_adapter.drafts = [draft("sites-fast")]
        sources = [
            make_source(id="sites-slow", adapter="paging", url=None, urls=urls),
            source("sites-fast"),
        ]

        def http_for(chosen: SourceConfig) -> HttpClient:
            return slow if chosen.id == "sites-slow" else FakeHttpClient()

        outcomes = collect(sources, context, http_for=http_for, timer=clock)
        slow_result = outcomes["sites-slow"].result
        assert (slow_result.status, slow_result.error_kind, slow_result.requests) == (
            "failed",
            "http",
            3,
        )
        assert "SourceTimeLimitError" in (slow_result.error or "")
        assert slow_result.duration_seconds == pytest.approx(1200.0)
        assert outcomes["sites-fast"].result.status == "ok"


class TestFallback:
    def test_the_extractor_steps_in_when_parsing_gives_nothing(
        self, fake_adapter: FakeAdapter, context: NormalizeContext
    ) -> None:
        extractor = FakePageExtractor([draft("sites-a", extraction="llm", source_text="x")])
        outcomes = collect([source("sites-a", llm_fallback=True)], context, extractor=extractor)
        result = outcomes["sites-a"].result
        assert (result.status, result.llm_fallback_used, result.extraction) == ("ok", True, "llm")
        assert extractor.requested == ["sites-a"]
        assert result.warnings == []

    def test_the_extractor_is_not_asked_when_parsing_worked(
        self, fake_adapter: FakeAdapter, context: NormalizeContext
    ) -> None:
        fake_adapter.drafts = [draft("sites-a")]
        extractor = FakePageExtractor([draft("sites-a", "9", extraction="llm", source_text="x")])
        collect([source("sites-a", llm_fallback=True)], context, extractor=extractor)
        assert extractor.requested == []

    def test_a_parse_error_is_rescued_by_the_extractor(
        self, fake_registry: AdapterRegistry, context: NormalizeContext
    ) -> None:
        FakeAdapter(parse_error=ParseError("odd page")).register(fake_registry, "fake")
        extractor = FakePageExtractor([draft("sites-a", extraction="llm", source_text="x")])
        outcomes = collect([source("sites-a", llm_fallback=True)], context, extractor=extractor)
        result = outcomes["sites-a"].result
        assert result.status == "ok"
        assert result.warnings == ["rule-based parsing failed; the LLM extraction was used"]

    def test_a_parse_error_stands_when_the_extractor_finds_nothing(
        self, fake_registry: AdapterRegistry, context: NormalizeContext
    ) -> None:
        FakeAdapter(parse_error=ParseError("odd page")).register(fake_registry, "fake")
        outcomes = collect(
            [source("sites-a", llm_fallback=True)], context, extractor=FakePageExtractor()
        )
        assert outcomes["sites-a"].result.error_kind == "parse"

    def test_without_an_llm_the_source_is_empty_and_says_why(
        self, fake_adapter: FakeAdapter, context: NormalizeContext
    ) -> None:
        outcomes = collect([source("sites-a", llm_fallback=True)], context)
        result = outcomes["sites-a"].result
        assert result.status == "empty"
        assert result.warnings == ["no LLM is available for the page extraction of this source"]

    def test_without_an_llm_a_parse_error_fails_the_source(
        self, fake_registry: AdapterRegistry, context: NormalizeContext
    ) -> None:
        FakeAdapter(parse_error=ParseError("odd page")).register(fake_registry, "fake")
        outcomes = collect([source("sites-a", llm_fallback=True)], context)
        assert outcomes["sites-a"].result.status == "failed"


class TestErrors:
    @pytest.mark.parametrize(
        ("error", "kind"),
        [
            (UnknownAdapterError("x"), "config"),
            (ForeignHostError("sites-a", URL), "config"),
            (RobotsDisallowedError(URL, "disallowed"), "robots"),
            (RobotsUnavailableError(URL, "unreachable"), "robots"),
            (CrawlDelayTooLongError(URL, "too long"), "robots"),
            (HostBudgetExceededError(URL, "budget"), "budget"),
            (DeadlineExceededError(URL, "deadline"), "http"),
            (ResponseTooLargeError(URL, 10), "http"),
            (ForbiddenHostError(URL, "private"), "http"),
            (SourceTimeLimitError(URL, 900.0), "http"),
            (ParseError("x"), "parse"),
            (ValueError("x"), "internal"),
        ],
    )
    def test_the_kind_of_a_failure(self, error: Exception, kind: str) -> None:
        assert error_kind(error) == kind

    def test_a_validation_error_counts_values_and_quotes_nothing(self) -> None:
        with pytest.raises(ValidationError) as caught:
            make_draft(url="javascript:alert(1)")
        assert error_kind(caught.value) == "parse"
        text = describe_error(caught.value, lambda value: value)
        assert text.startswith("ValidationError: ")
        assert "value(s) do not fit the schema" in text
        assert "javascript" not in text

    def test_the_message_is_printable_ascii_redacted_and_short(self) -> None:
        scanner = build_secret_scanner()
        forged = "sk-" + "ant-" + "api03-" + "Z" * 40
        message = "caf\u00e9\n\x1b[31m" + forged + " " + "x" * 400
        text = describe_error(RuntimeError(message), scanner.redact)
        assert text.isascii()
        assert text.isprintable()
        assert forged not in text
        assert "[redacted]" in text
        assert len(text) == MAX_ERROR_CHARS

    def test_an_empty_message_names_the_type(self) -> None:
        assert describe_error(RuntimeError(), lambda value: value) == "RuntimeError"

    def test_a_withheld_credential_is_reported_as_a_warning(
        self, fake_adapter: FakeAdapter, app_config: AppConfig
    ) -> None:
        scanner = build_secret_scanner()
        settings = app_config.settings
        context = NormalizeContext(
            settings=settings,
            now=REFERENCE_NOW,
            window=settings.window_at(REFERENCE_NOW),
            redact=scanner.redact,
            is_secret=scanner.has_secret,
        )
        forged = "gh" + "p_" + "Q" * 36
        fake_adapter.drafts = [draft("sites-a", abstract=f"Join with token {forged} today.")]
        [outcome] = collect_sources(
            [source("sites-a")], no_http, context=context, extractor=None, timer=FakeClock()
        )
        assert any("withheld" in warning for warning in outcome.result.warnings)
        assert forged not in (outcome.candidates[0].event.abstract or "")


class TestClock:
    def test_the_live_time_is_aware_and_whole_seconds(self) -> None:
        now = current_time()
        assert now.utcoffset() == timedelta(0)
        assert now.microsecond == 0

    def test_a_live_run_reads_the_clock(self, tmp_path: Path) -> None:
        before = datetime.now(UTC) - timedelta(seconds=1)
        assert run_time(offline=False, fixtures_dir=tmp_path) >= before

    def test_an_offline_run_takes_the_latest_recording(self, tmp_path: Path) -> None:
        store = FixtureStore.of(tmp_path, "sites-a")
        later = REFERENCE_NOW + timedelta(hours=3)
        store.add(make_document(f"{URL}/1", fetched_at=REFERENCE_NOW))
        store.add(make_document(f"{URL}/2", fetched_at=later))
        assert run_time(offline=True, fixtures_dir=tmp_path) == later

    def test_an_offline_run_without_fixtures_cannot_start(self, tmp_path: Path) -> None:
        with pytest.raises(ClockError, match="offline mode needs recorded fixtures"):
            run_time(offline=True, fixtures_dir=tmp_path / "missing")
        with pytest.raises(ClockError):
            run_time(offline=True, fixtures_dir=tmp_path)
