"""Tests for the shared fakes and fixtures, so that other test modules can trust them."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from factories import (
    REFERENCE_NOW,
    make_candidate,
    make_draft,
    make_event,
    make_payload,
    make_source,
    make_window,
)
from fakes import (
    FakeAdapter,
    FakeClock,
    FakeEventFilter,
    FakeFieldTagger,
    FakeHttpClient,
    FakeIssueClient,
    FakePicksScorer,
    FakeSecretScanner,
)
from fakes_llm import FakeLLMCache, FakeLLMClient, FakeLLMFieldTagger, FakePageExtractor, LLMCall
from fakes_outputs import FakeEmitters
from seminars.config import TAG_ONLY_PROVIDERS, AppConfig
from seminars.contracts import (
    LABEL_DIGEST,
    LLMAuthError,
    LLMInvalidOutputError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from seminars.http import FixtureNotFoundError, HttpRequestError, HttpStatusError
from seminars.models import (
    FEED_IDS,
    Event,
    HealthFile,
    LLMCacheStats,
    LLMTagResult,
    PublicEventsPayload,
    PublicStatusPayload,
    RunReport,
    SourceRunResult,
    TagDecision,
    read_model,
)
from seminars.sources import base
from seminars.sources.base import Adapter, AdapterRegistry, ParseError, UnknownAdapterError


class Fields(BaseModel):
    fields: list[str]


class TestFakeClock:
    def test_sleep_advances_time_without_waiting(self, fake_clock: FakeClock) -> None:
        start = fake_clock()
        fake_clock.sleep(30.0)
        fake_clock.advance(5.0)
        assert fake_clock() == start + 35.0
        assert fake_clock.sleeps == [30.0]
        assert fake_clock.now() == REFERENCE_NOW + timedelta(seconds=35)


class TestFakeHttpClient:
    def test_serves_pages_and_records_requests(self, fake_http: FakeHttpClient) -> None:
        fake_http.add("https://a.example.edu/x", "hello", content_type="text/plain")
        document = fake_http.get("https://a.example.edu/x")
        assert (document.text, document.content_type, document.status) == (
            "hello\n",
            "text/plain",
            200,
        )
        assert fake_http.requests == ["https://a.example.edu/x"]

    def test_text_has_the_same_form_as_live_and_recorded_documents(
        self, fake_http: FakeHttpClient
    ) -> None:
        fake_http.add("https://a.example.edu/feed.ics", "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
        assert fake_http.get("https://a.example.edu/feed.ics").text == (
            "BEGIN:VCALENDAR\nEND:VCALENDAR\n"
        )

    def test_unknown_url_and_error_status(self, fake_http: FakeHttpClient) -> None:
        fake_http.add("https://a.example.edu/gone", "gone", status=410)
        with pytest.raises(FixtureNotFoundError):
            fake_http.get("https://a.example.edu/other")
        with pytest.raises(HttpStatusError):
            fake_http.get("https://a.example.edu/gone")
        assert fake_http.get("https://a.example.edu/gone", allow_statuses=[410]).status == 410


class TestFakeAdapter:
    def test_fetch_does_no_io_and_parse_returns_the_drafts_of_the_source(
        self, fake_http: FakeHttpClient, fake_clock: FakeClock
    ) -> None:
        mine = make_draft(source_id="sites-a", native_id="1")
        other = make_draft(source_id="sites-b", native_id="2")
        adapter = FakeAdapter([mine, other], clock=fake_clock)
        source = make_source(id="sites-a", adapter="fake")
        window = make_window()
        fake_clock.advance(60.0)
        payload = adapter.fetch(fake_http, source, window=window)
        assert fake_http.requests == []
        assert (payload.source_id, payload.window) == ("sites-a", window)
        assert payload.fetched_at == fake_clock.now()
        assert [document.url for document in payload.documents] == [source.all_urls[0]]
        assert adapter.parse(payload, source) == [mine]
        assert adapter.parse(payload, make_source(id="sites-b", adapter="fake")) == [other]
        assert adapter.parse(payload, make_source(id="sites-c", adapter="fake")) == []
        assert adapter.calls == [
            ("fetch", "sites-a"),
            ("parse", "sites-a"),
            ("parse", "sites-b"),
            ("parse", "sites-c"),
        ]
        assert adapter.windows == [window]

    def test_fetch_and_parse_failures(self) -> None:
        source = make_source(adapter="fake")
        broken_fetch = FakeAdapter(fetch_error=HttpRequestError(source.all_urls[0], "down"))
        with pytest.raises(HttpRequestError, match="down"):
            broken_fetch.fetch(FakeHttpClient(), source, window=make_window())
        assert broken_fetch.calls == [("fetch", source.id)]
        broken_parse = FakeAdapter([make_draft()], parse_error=ParseError("layout changed"))
        payload = broken_parse.fetch(FakeHttpClient(), source, window=make_window())
        with pytest.raises(ParseError, match="layout changed"):
            broken_parse.parse(payload, source)

    def test_register_makes_the_registry_hand_out_this_instance(self) -> None:
        registry = AdapterRegistry(())
        adapter = FakeAdapter().register(registry, "fake_one")
        assert registry.names() == ["fake_one"]
        assert registry.create("fake_one") is adapter
        assert registry.create("fake_one") is adapter
        assert isinstance(adapter, Adapter)

    def test_fake_registry_replaces_the_process_wide_registry(
        self, fake_registry: AdapterRegistry
    ) -> None:
        assert base.REGISTRY is fake_registry
        assert base.adapter_names() == []
        assert base.discover_adapters() == {}
        with pytest.raises(UnknownAdapterError, match=r"registered: none"):
            base.get_adapter("rss")

    def test_fake_registry_lets_the_real_registry_discover_first(
        self, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
    ) -> None:
        """An adapter module first imported during the test must not land in the fake one."""
        real = AdapterRegistry(())  # stands in for the process-wide registry
        discoveries: list[bool] = []

        def discover(*, strict: bool = True) -> dict[str, str]:
            discoveries.append(strict)
            return {}

        monkeypatch.setattr(real, "discover", discover)
        monkeypatch.setattr(base, "REGISTRY", real)
        fake = request.getfixturevalue("fake_registry")
        assert discoveries == [False]
        assert base.REGISTRY is fake
        assert fake is not real

    def test_fake_adapter_fixture_is_registered_as_fake(
        self, fake_adapter: FakeAdapter, fake_registry: AdapterRegistry, fake_http: FakeHttpClient
    ) -> None:
        fake_adapter.drafts.append(make_draft())
        source = make_source(adapter="fake")
        assert base.adapter_names() == ["fake"]
        adapter = base.get_adapter(source.adapter)
        assert adapter is fake_adapter
        drafts = adapter.parse(adapter.fetch(fake_http, source, window=make_window()), source)
        assert [draft.native_id for draft in drafts] == ["1234"]
        assert fake_registry.create("fake") is fake_adapter


class TestFakeLLMClient:
    def test_scripted_replies_are_consumed_in_order(self, fake_llm: FakeLLMClient) -> None:
        fake_llm.script({"fields": ["bio"]}, Fields(fields=["chem"]))
        first = fake_llm.complete_json("system", "user 1", Fields, max_tokens=100)
        second = fake_llm.complete_json("system", "user 2", Fields, max_tokens=100)
        assert (first.fields, second.fields) == (["bio"], ["chem"])
        assert [call.user for call in fake_llm.calls] == ["user 1", "user 2"]
        assert fake_llm.calls[0] == LLMCall("system", "user 1", Fields, 100)

    def test_unscripted_call_is_a_test_bug(self, fake_llm: FakeLLMClient) -> None:
        with pytest.raises(AssertionError, match="no scripted reply"):
            fake_llm.complete_json("s", "u", Fields, max_tokens=1)

    def test_always_and_callable_replies(self) -> None:
        def echo(call: LLMCall) -> dict[str, list[str]]:
            return {"fields": [call.user]}

        llm = FakeLLMClient().always(echo)
        assert llm.complete_json("s", "math", Fields, max_tokens=1).fields == ["math"]
        assert llm.complete_json("s", "cs", Fields, max_tokens=1).fields == ["cs"]

    def test_failure_injection(self) -> None:
        llm = FakeLLMClient({"fields": ["bio"]}, name="claude-cli")
        llm.fail(LLMRateLimitError("slow down", provider="claude-cli"), times=2)
        for _ in range(2):
            with pytest.raises(LLMRateLimitError):
                llm.complete_json("s", "u", Fields, max_tokens=1)
        assert llm.complete_json("s", "u", Fields, max_tokens=1).fields == ["bio"]
        usage = llm.usage()
        assert (usage.calls, usage.provider) == (3, "claude-cli")
        assert usage.providers[0].failures == 2
        assert usage.providers[0].status == "ok"

    def test_invalid_reply_raises_invalid_output(self, fake_llm: FakeLLMClient) -> None:
        fake_llm.script({"wrong": 1})
        with pytest.raises(LLMInvalidOutputError, match="does not match Fields"):
            fake_llm.complete_json("s", "u", Fields, max_tokens=1)

    def test_usage_of_a_failing_or_unused_client(self) -> None:
        assert FakeLLMClient().usage().providers[0].status == "unused"
        llm = FakeLLMClient().always(LLMAuthError("expired", provider="fake-llm"))
        with pytest.raises(LLMAuthError):
            llm.complete_json("s", "u", Fields, max_tokens=1)
        result = llm.usage().providers[0]
        assert (result.status, result.failure_kind) == ("failed", "auth")
        assert llm.usage().provider is None

    def test_restricted_view_shares_script_calls_and_usage(self, fake_llm: FakeLLMClient) -> None:
        fake_llm.script({"fields": ["bio"]}, {"fields": ["chem"]})
        view = fake_llm.restricted(TAG_ONLY_PROVIDERS)
        assert view.name == fake_llm.name
        first = view.complete_json("s", "extract", Fields, max_tokens=1)
        second = fake_llm.complete_json("s", "tag", Fields, max_tokens=1)
        assert (first.fields, second.fields) == (["bio"], ["chem"])
        assert view.calls is fake_llm.calls
        assert [(call.user, call.excluded) for call in fake_llm.calls] == [
            ("extract", frozenset({"codex-cli"})),
            ("tag", frozenset()),
        ]
        assert fake_llm.restrictions == [frozenset({"codex-cli"})]
        assert view.usage() == fake_llm.usage()
        assert fake_llm.usage().calls == 2

    def test_restrictions_add_up_and_later_replies_reach_the_view(self) -> None:
        llm = FakeLLMClient()
        inner = llm.restricted(["codex-cli"]).restricted({"openai"})
        llm.always({"fields": ["math"]})
        assert inner.complete_json("s", "u", Fields, max_tokens=1).fields == ["math"]
        assert llm.calls[0].excluded == {"codex-cli", "openai"}
        assert llm.restrictions == [frozenset({"codex-cli"}), frozenset({"openai"})]

    def test_a_chain_without_any_allowed_provider_is_unavailable(self) -> None:
        llm = FakeLLMClient({"fields": ["bio"]}, name="codex-cli")
        view = llm.restricted(TAG_ONLY_PROVIDERS)
        with pytest.raises(LLMUnavailableError, match="no provider is left"):
            view.complete_json("s", "u", Fields, max_tokens=1)
        assert llm.calls == []
        assert llm.complete_json("s", "u", Fields, max_tokens=1).fields == ["bio"]


class TestClassificationFakes:
    def test_cache(self, fake_llm_cache: FakeLLMCache) -> None:
        assert fake_llm_cache.stats() == LLMCacheStats()
        assert fake_llm_cache.get("k") is None
        fake_llm_cache.put("k", {"fields": ["bio"]})
        assert fake_llm_cache.get("k") == {"fields": ["bio"]}
        assert (fake_llm_cache.hits, fake_llm_cache.misses, fake_llm_cache.writes) == (1, 1, 1)
        assert fake_llm_cache.stats() == LLMCacheStats(hits=1, misses=1, writes=1)
        assert fake_llm_cache.prune() == 0

    def test_field_tagger_uses_hints_and_overrides(self) -> None:
        with_hints = make_candidate()
        without = make_candidate(make_event(uid="no-hints", fields=[]), default_fields=[])
        override = TagDecision(fields=["math"], method="rules")
        tagger = FakeFieldTagger({"forced": override})
        assert tagger.tag(with_hints) == TagDecision(fields=["bio"], method="source")
        assert tagger.tag(without).needs_llm
        assert tagger.tag(make_candidate(make_event(uid="forced", fields=[]))) == override
        assert tagger.tagged == [with_hints.event.uid, "no-hints", "forced"]

    def test_llm_tagger_answers_only_known_uids(self) -> None:
        tagger = FakeLLMFieldTagger({"a": LLMTagResult(fields=["cs"])})
        batch = [make_candidate(make_event(uid=uid, fields=[])) for uid in ("a", "b")]
        assert tagger.tag_batch(batch) == {"a": LLMTagResult(fields=["cs"])}
        assert tagger.batches == [["a", "b"]]

    def test_event_filter(self) -> None:
        candidates = [make_candidate(make_event(uid=uid, fields=[])) for uid in ("a", "b", "c")]
        result = FakeEventFilter(exclude={"b": "office_hours"}, uncertain={"c"}).filter(candidates)
        assert [candidate.event.uid for candidate in result.kept] == ["a", "c"]
        assert [(item.reason, item.detail) for item in result.excluded] == [
            ("office_hours", "fake filter")
        ]
        assert result.uncertain == ["c"]

    def test_event_filter_applies_the_defense_switch(self) -> None:
        candidates = [
            make_candidate(make_event(uid="talk", fields=[])),
            make_candidate(make_event(uid="thesis", fields=[], event_type="defense")),
        ]
        assert len(FakeEventFilter().filter(candidates).kept) == 2
        result = FakeEventFilter(include_defenses=False).filter(candidates)
        assert [candidate.event.uid for candidate in result.kept] == ["talk"]
        assert [(item.title, item.reason) for item in result.excluded] == [
            (candidates[1].event.title, "defense")
        ]

    def test_out_of_scope_rule(self, fake_event_filter: FakeEventFilter) -> None:
        events = [make_event(uid="tagged"), make_event(uid="untagged", fields=[])]
        result = fake_event_filter.drop_out_of_scope(events)
        assert [event.uid for event in result.kept] == ["tagged"]
        assert [item.reason for item in result.excluded] == ["out_of_scope"]

    def test_picks_scorer(self) -> None:
        scorer = FakePicksScorer({"hot": 4.5}, threshold=3.0)
        assert scorer.score(make_event(uid="hot")).score == 4.5
        assert scorer.score(make_event(uid="cold")).reasons == []
        assert scorer.threshold == 3.0

    def test_page_extractor(self) -> None:
        draft = make_draft(extraction="llm", source_text="Talk by Jane Doe")
        extractor = FakePageExtractor([draft])
        assert extractor.extract(make_payload(), make_source(id="custom-x", group="other")) == [
            draft
        ]
        assert extractor.requested == ["custom-x"]


class TestFakeIssueClient:
    def test_open_find_update_close(
        self, fake_issues: FakeIssueClient, fake_clock: FakeClock
    ) -> None:
        fake_issues.ensure_label(LABEL_DIGEST)
        first = fake_issues.open("Seminars - week of 2026-11-09", "body", labels=["digest"])
        fake_clock.advance(7 * 86400)
        second = fake_issues.open("Seminars - week of 2026-11-16", "body", labels=["digest"])
        assert (first.number, second.number) == (1, 2)
        assert [issue.number for issue in fake_issues.find_by_label("digest")] == [2, 1]
        assert fake_issues.find_by_title("Seminars - week of 2026-11-09") == first
        assert fake_issues.find_by_title("missing") is None
        assert fake_issues.update(1, body="new body").body == "new body"
        fake_issues.close(1, comment="superseded")
        assert fake_issues.get(1).state == "closed"
        assert fake_issues.comments == {1: ["superseded"]}
        assert [issue.number for issue in fake_issues.find_by_label("digest")] == [2]
        assert [issue.number for issue in fake_issues.find_by_label("digest", state="all")] == [
            2,
            1,
        ]
        assert fake_issues.find_by_label("source-broken") == []
        assert [name for name, _ in fake_issues.calls][:3] == ["ensure_label", "open", "open"]
        assert fake_issues.labels == {"digest": LABEL_DIGEST}

    def test_seed_is_not_recorded_as_a_call(self, fake_issues: FakeIssueClient) -> None:
        issue = fake_issues.seed("Source broken: sites-x", labels=["source-broken"])
        assert fake_issues.calls == []
        assert issue.created_at == REFERENCE_NOW
        assert fake_issues.find_by_title("Source broken: sites-x", state="open") == issue


class TestFakeSecretScanner:
    def test_reports_lines_with_needles(self, tmp_path: Path) -> None:
        scanner = FakeSecretScanner(["sk-fake-123"])
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "a.json").write_text("ok\ntoken sk-fake-123\n", encoding="utf-8")
        findings = scanner.scan_paths([tmp_path])
        assert [(finding.line, finding.rule) for finding in findings] == [(2, "fake-needle")]
        assert findings[0].origin.endswith("a.json")
        assert scanner.scan_text("clean", origin="memory") == []


class TestFakeEmitters:
    def test_ics_feeds(self, fake_emitters: FakeEmitters, app_config: AppConfig) -> None:
        events = [make_event(uid="a", picks_score=9.0), make_event(uid="b", fields=["math", "cs"])]
        out_dir = app_config.paths.public_feeds_dir
        paths = fake_emitters.write_ics_feeds(events, out_dir, config=app_config)
        assert [path.name for path in paths] == [f"{feed}.ics" for feed in FEED_IDS]
        assert "X-FAKE-EVENTS:2" in (out_dir / "all.ics").read_text(encoding="utf-8")
        assert "X-FAKE-EVENTS:1" in (out_dir / "picks.ics").read_text(encoding="utf-8")
        assert "X-FAKE-EVENTS:1" in (out_dir / "math.ics").read_text(encoding="utf-8")
        assert fake_emitters.validate_ics(paths[0].read_text(encoding="utf-8")) == []
        assert fake_emitters.validate_ics("<html>") != []

    def test_site_data_is_schema_valid(
        self, fake_emitters: FakeEmitters, app_config: AppConfig
    ) -> None:
        events = [make_event(uid="b"), make_event(uid="a", missing_runs=2)]
        paths = fake_emitters.write_site_data(
            events,
            HealthFile(),
            None,
            app_config.paths.public_dir,
            config=app_config,
            generated_at=REFERENCE_NOW,
        )
        events_payload = read_model(paths[0], PublicEventsPayload)
        status_payload = read_model(paths[1], PublicStatusPayload)
        assert events_payload is not None
        assert status_payload is not None
        assert [event.uid for event in events_payload.events] == ["a", "b"]
        assert "missing_runs" not in paths[0].read_text(encoding="utf-8")
        assert status_payload.event_count == 2

    def test_digest_is_idempotent_and_closes_the_previous_issue(
        self, fake_emitters: FakeEmitters, fake_issues: FakeIssueClient, app_config: AppConfig
    ) -> None:
        previous = fake_issues.seed(
            "Seminars - week of 2026-11-02",
            labels=["digest"],
            created_at=REFERENCE_NOW - timedelta(days=8),
        )
        events = [make_event()]

        def publish(dry_run: bool = False) -> str:
            outcome = fake_emitters.publish_digest(
                events,
                HealthFile(),
                fake_issues,
                config=app_config,
                now=REFERENCE_NOW,
                owner="octocat",
                dry_run=dry_run,
            )
            assert outcome.document.title == "Seminars - week of 2026-11-09"
            assert "cc @octocat" in outcome.document.body
            return outcome.action

        assert publish(dry_run=True) == "dry_run"
        assert len(fake_issues.issues) == 1
        assert publish() == "created"
        assert fake_issues.get(previous.number).state == "closed"
        assert publish() == "exists"
        assert len(fake_issues.issues) == 2

    def test_health_update_and_probe(
        self, fake_emitters: FakeEmitters, fake_http: FakeHttpClient, app_config: AppConfig
    ) -> None:
        report = RunReport(
            started_at=REFERENCE_NOW,
            finished_at=REFERENCE_NOW,
            mode="offline",
            ok=True,
            sources=[SourceRunResult(source_id="sites-a", status="ok", events=3)],
        )
        health = fake_emitters.update_health(HealthFile(), report, None, config=app_config)
        assert health.sources["sites-a"].last_success_at == REFERENCE_NOW
        fake_http.add("https://a.example.edu/feed", "<rss/>", content_type="application/rss+xml")
        assert (
            fake_emitters.probe_source(fake_http, "https://a.example.edu/feed").format == "unknown"
        )
        assert [name for name, _ in fake_emitters.calls] == ["update_health", "probe_source"]


class TestFixtures:
    def test_app_config_is_a_scratch_project(self, app_config: AppConfig, tmp_path: Path) -> None:
        assert app_config.paths.root == tmp_path
        assert app_config.settings.uid_domain == "seminars.fridrichmethod.github.io"
        assert app_config.sources == ()
        assert not app_config.paths.data_dir.exists()

    @pytest.mark.browser
    def test_playwright_fixture_provides_the_sync_api(self, playwright_sync_api: Any) -> None:
        assert callable(playwright_sync_api.sync_playwright)

    def test_factory_fixtures(
        self, make_event: Callable[..., Event], make_source: Callable[..., object]
    ) -> None:
        assert make_event(uid="x").uid == "x"
        assert make_source() is not None
