"""Tests for the wiring of a run: which implementations are loaded and how they are built."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from factories import REFERENCE_NOW, make_document, make_source
from fakes import (
    FakeEventFilter,
    FakeFieldTagger,
    FakeHttpClient,
    FakeIssueClient,
    FakePicksScorer,
    FakeSecretScanner,
)
from fakes_llm import FakeLLMCache, FakeLLMClient, FakeLLMFieldTagger, FakePageExtractor
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig, LLMConfig
from seminars.contracts import ContractNotImplementedError
from seminars.fixtures import FixtureStore
from seminars.http import OfflineHttpClient, PoliteClient
from seminars.pipeline import services as wiring
from seminars.pipeline.secret_scan import build_secret_scanner

TOKEN_VALUE = "plain-looking-credential-value"


class Recorder:
    """Hands out fakes for the contract keys and remembers how the factories were called."""

    def __init__(self) -> None:
        self.emitters = FakeEmitters()
        self.chain: FakeLLMClient | None = None
        self.issues: FakeIssueClient | None = FakeIssueClient()
        self.calls: dict[str, dict[str, Any]] = {}
        self.missing: set[str] = set()

    def _record(self, key: str, result: Any) -> Callable[..., Any]:
        def factory(*args: Any, **kwargs: Any) -> Any:
            self.calls[key] = {"args": args, "kwargs": kwargs}
            if isinstance(result, Exception):
                raise result
            return result

        return factory

    def load(self, key: str) -> Any:
        if key in self.missing:
            raise ContractNotImplementedError(f"contract {key!r} is not implemented")
        table: dict[str, Any] = {
            "event_filter": self._record(key, FakeEventFilter()),
            "field_tagger": self._record(key, FakeFieldTagger()),
            "picks_scorer": self._record(key, FakePicksScorer()),
            "ics_feeds": self.emitters.write_ics_feeds,
            "site_data": self.emitters.write_site_data,
            "health_update": self.emitters.update_health,
            "secret_scanner": build_secret_scanner,
            "llm_client": self._record(key, self.chain),
            "llm_cache": self._record(key, FakeLLMCache()),
            "llm_tagger": self._record(key, FakeLLMFieldTagger()),
            "page_extractor": self._record(key, FakePageExtractor()),
            "issue_client": self._record(key, self.issues),
        }
        return table[key]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(wiring, "load", recorder.load)
    return recorder


def build(config: AppConfig, env: Mapping[str, str] | None = None, **options: Any) -> Any:
    return wiring.build_services(config, env or {}, now=REFERENCE_NOW, **options)


class TestParts:
    def test_the_classification_parts_are_built_from_the_config(
        self, app_config: AppConfig, recorder: Recorder
    ) -> None:
        bundle = build(app_config, offline=True)
        assert isinstance(bundle.event_filter, FakeEventFilter)
        assert recorder.calls["event_filter"]["args"] == (
            app_config.taxonomy,
            app_config.settings,
        )
        assert recorder.calls["field_tagger"]["args"] == (app_config.taxonomy,)
        assert recorder.calls["picks_scorer"]["args"] == (app_config.interests,)
        assert bundle.write_ics_feeds == recorder.emitters.write_ics_feeds

    def test_a_missing_package_stops_the_wiring(
        self, app_config: AppConfig, recorder: Recorder
    ) -> None:
        recorder.missing.add("ics_feeds")
        with pytest.raises(ContractNotImplementedError):
            build(app_config)

    def test_the_scanner_knows_the_credentials_of_the_environment(
        self, app_config: AppConfig, recorder: Recorder
    ) -> None:
        bundle = build(app_config, {"GITHUB_TOKEN": TOKEN_VALUE}, offline=True)
        found = bundle.secret_scanner.scan_text(f"x {TOKEN_VALUE}\n", origin="x")
        assert [finding.rule for finding in found] == ["known-credential"]
        assert TOKEN_VALUE not in repr(bundle.secret_scanner)

    def test_another_scanner_is_used_as_it_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeSecretScanner(["needle"])
        monkeypatch.setattr(wiring, "load", lambda key: lambda: fake)
        assert wiring.build_scanner({"GITHUB_TOKEN": TOKEN_VALUE}) is fake


class ClosingFakeHttpClient(FakeHttpClient):
    """A fake client that can be closed at the end of a run, like ``PoliteClient``."""

    def close(self) -> None:
        return None


class TestHttp:
    def test_offline_each_source_sees_only_its_own_recordings(
        self, app_config: AppConfig, recorder: Recorder
    ) -> None:
        url = "https://a.example.edu/events"
        FixtureStore.of(app_config.paths.fixtures_dir, "sites-a").add(make_document(url))
        bundle = build(app_config, offline=True)
        client = bundle.http_for(make_source(id="sites-a", url=url))
        assert isinstance(client, OfflineHttpClient)
        assert client.get(url).url == url
        other = bundle.http_for(make_source(id="sites-b", url=url))
        with pytest.raises(LookupError):
            other.get(url)
        bundle.close()

    def test_live_every_source_shares_one_polite_client(
        self, app_config: AppConfig, recorder: Recorder, tmp_path: Path
    ) -> None:
        env = {"SEMINARS_HTTP_CACHE_DIR": str(tmp_path / "http-cache")}
        bundle = build(app_config, env)
        first = bundle.http_for(make_source(id="sites-a"))
        second = bundle.http_for(make_source(id="sites-b"))
        assert isinstance(first, PoliteClient)
        assert first is second
        bundle.close()

    def test_live_a_source_with_ignore_robots_gets_its_view_of_that_client(
        self, app_config: AppConfig, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shared = ClosingFakeHttpClient()
        monkeypatch.setattr(wiring, "live_client", lambda config, env: shared)
        bundle = build(app_config)
        bundle.http_for(make_source(id="sites-a"))
        bundle.http_for(make_source(id="sites-b", ignore_robots=["bioengineering.stanford.edu"]))
        assert shared.robots_exemptions == [[], ["bioengineering.stanford.edu"]]
        bundle.close()


class TestLLM:
    def test_without_a_provider_only_the_stored_answers_help(
        self, app_config: AppConfig, recorder: Recorder
    ) -> None:
        # Both answer from the cache alone (tests/test_pipeline_services_cached_verdicts.py).
        bundle = build(app_config, offline=True)
        assert (bundle.llm, bundle.llm_cache) == (None, None)
        assert bundle.llm_tagger is not None
        assert bundle.page_extractor is not None
        for key in ("llm_tagger", "page_extractor"):
            (client,) = recorder.calls[key]["args"]
            assert isinstance(client, wiring.NoProvider)
            assert isinstance(recorder.calls[key]["kwargs"]["cache"], wiring.ReadOnlyCache)

    def test_a_chain_brings_cache_tagger_and_extractor(
        self, app_config: AppConfig, recorder: Recorder, tmp_path: Path
    ) -> None:
        recorder.chain = FakeLLMClient()
        bundle = build(app_config, offline=True, llm_cache_dir=tmp_path / "cache")
        assert bundle.llm is recorder.chain
        cache_call = recorder.calls["llm_cache"]
        assert cache_call["args"] == (tmp_path / "cache",)
        assert cache_call["kwargs"] == {
            "ttl_days": app_config.llm.cache_ttl_days,
            "now": REFERENCE_NOW,
        }
        for key in ("llm_tagger", "page_extractor"):
            assert recorder.calls[key]["args"] == (recorder.chain,)
            assert recorder.calls[key]["kwargs"] == {
                "config": app_config.llm,
                "cache": bundle.llm_cache,
            }
        assert isinstance(recorder.calls["llm_client"]["args"][0], LLMConfig)

    def test_the_cache_defaults_to_the_project(
        self, app_config: AppConfig, recorder: Recorder
    ) -> None:
        recorder.chain = FakeLLMClient()
        build(app_config, offline=True)
        assert recorder.calls["llm_cache"]["args"] == (app_config.paths.llm_cache_dir,)

    def test_no_llm_wanted_loads_nothing_of_it(
        self, app_config: AppConfig, recorder: Recorder
    ) -> None:
        recorder.chain = FakeLLMClient()
        bundle = build(app_config, offline=True, use_llm=False)
        assert bundle.llm is None
        assert "llm_client" not in recorder.calls

    def test_a_missing_llm_package_leaves_the_rules_alone(
        self, app_config: AppConfig, recorder: Recorder
    ) -> None:
        recorder.missing.add("llm_client")
        bundle = build(app_config, offline=True)
        assert bundle.llm is None
        assert bundle.warnings == (
            "the LLM is off: it could not be set up (ContractNotImplementedError)",
        )


class TestIssues:
    def test_issues_come_from_the_factory(self, app_config: AppConfig, recorder: Recorder) -> None:
        env = {"GITHUB_TOKEN": TOKEN_VALUE, "GITHUB_REPOSITORY": "o/r"}
        bundle = build(app_config, env, offline=True)
        assert bundle.issues is recorder.issues
        assert recorder.calls["issue_client"]["args"] == (env,)

    def test_no_issues_when_they_are_not_wanted(
        self, app_config: AppConfig, recorder: Recorder
    ) -> None:
        bundle = build(app_config, offline=True, with_issues=False)
        assert bundle.issues is None
        assert "issue_client" not in recorder.calls

    def test_a_broken_factory_turns_issues_off_with_a_warning(
        self, app_config: AppConfig, recorder: Recorder
    ) -> None:
        recorder.missing.add("issue_client")
        bundle = build(app_config, offline=True)
        assert bundle.issues is None
        assert bundle.warnings == (
            "GitHub issues are off: the client could not be built (ContractNotImplementedError)",
        )


class TestRedaction:
    def test_the_project_scanner_replaces_what_it_finds(self) -> None:
        redact, is_secret = wiring.redaction(build_secret_scanner())
        forged = "gh" + "p_" + "T" * 36
        assert redact(f"a {forged} b") == "a [redacted] b"
        assert is_secret(forged)

    def test_another_scanner_can_only_tell(self) -> None:
        redact, is_secret = wiring.redaction(FakeSecretScanner(["needle"]))
        assert redact("a needle") == "a needle"
        assert is_secret("a needle")
        assert not is_secret("hay")


def test_the_run_time_is_passed_on(app_config: AppConfig, recorder: Recorder) -> None:
    recorder.chain = FakeLLMClient()
    later = datetime(2027, 3, 14, 10, 0, tzinfo=REFERENCE_NOW.tzinfo)
    wiring.build_services(app_config, {}, now=later, offline=True)
    assert recorder.calls["llm_cache"]["kwargs"]["now"] == later
