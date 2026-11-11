"""Tests for the cross-package contracts and the conformance of their fakes."""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from pathlib import Path

import pytest

from fakes import (
    FakeAdapter,
    FakeEventFilter,
    FakeFieldTagger,
    FakeHttpClient,
    FakeIssueClient,
    FakePicksScorer,
    FakeSecretScanner,
)
from fakes_llm import FakeLLMCache, FakeLLMClient, FakeLLMFieldTagger, FakePageExtractor
from fakes_outputs import FakeEmitters
from seminars import contracts
from seminars.contracts import (
    IMPLEMENTATIONS,
    ISSUE_LABELS,
    ContractNotImplementedError,
    DigestPublisher,
    DigestRenderer,
    EventFilter,
    FieldTagger,
    HealthUpdater,
    IcsFeedWriter,
    IcsValidator,
    IssueClient,
    LLMAuthError,
    LLMBudgetExceededError,
    LLMCache,
    LLMChainClient,
    LLMClient,
    LLMError,
    LLMFieldTagger,
    LLMInvalidOutputError,
    LLMNetworkError,
    LLMProviderError,
    LLMQuotaError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnavailableError,
    PageExtractor,
    PicksScorer,
    SecretScanner,
    SiteDataWriter,
    SourceProber,
    llm_cache_key,
    load_implementation,
)
from seminars.http import HttpClient
from seminars.sources.base import Adapter

PROTOCOL_FAKES: list[tuple[type, object]] = [
    (HttpClient, FakeHttpClient()),
    (Adapter, FakeAdapter()),
    (LLMClient, FakeLLMClient()),
    (LLMChainClient, FakeLLMClient()),
    (LLMCache, FakeLLMCache()),
    (FieldTagger, FakeFieldTagger()),
    (LLMFieldTagger, FakeLLMFieldTagger()),
    (EventFilter, FakeEventFilter()),
    (PicksScorer, FakePicksScorer()),
    (PageExtractor, FakePageExtractor()),
    (IssueClient, FakeIssueClient()),
    (SecretScanner, FakeSecretScanner()),
]


def test_fakes_satisfy_the_protocols_statically() -> None:
    """Checked by pyright: each assignment only type-checks if the fake conforms."""
    emitters = FakeEmitters()
    http: HttpClient = FakeHttpClient()
    adapter: Adapter = FakeAdapter()
    llm: LLMClient = FakeLLMClient()
    chain: LLMChainClient = FakeLLMClient()
    cache: LLMCache = FakeLLMCache()
    tagger: FieldTagger = FakeFieldTagger()
    llm_tagger: LLMFieldTagger = FakeLLMFieldTagger()
    event_filter: EventFilter = FakeEventFilter()
    scorer: PicksScorer = FakePicksScorer()
    extractor: PageExtractor = FakePageExtractor()
    issues: IssueClient = FakeIssueClient()
    scanner: SecretScanner = FakeSecretScanner()
    write_feeds: IcsFeedWriter = emitters.write_ics_feeds
    validate: IcsValidator = emitters.validate_ics
    write_site: SiteDataWriter = emitters.write_site_data
    render: DigestRenderer = emitters.render_digest
    publish: DigestPublisher = emitters.publish_digest
    update: HealthUpdater = emitters.update_health
    probe: SourceProber = emitters.probe_source
    everything = [http, llm, chain, cache, tagger, llm_tagger, event_filter, scorer, extractor]
    everything += [issues, scanner, write_feeds, validate, write_site, render, publish, update]
    everything += [adapter]
    assert all(item is not None for item in [*everything, probe])


@pytest.mark.parametrize(("protocol", "fake"), PROTOCOL_FAKES, ids=lambda value: str(value)[:40])
def test_fakes_satisfy_the_protocols_at_run_time(protocol: type, fake: object) -> None:
    assert isinstance(fake, protocol)


def test_every_protocol_has_a_fake() -> None:
    protocols = {
        name
        for name, value in vars(contracts).items()
        if isinstance(value, type)
        and getattr(value, "_is_protocol", False)
        and value.__module__.startswith(contracts.__name__)
    }
    factories_and_reserved = {name for name in protocols if name.endswith("Factory")} | {
        "LLMProber",
        "PicksReranker",
    }
    callables = {
        "IcsFeedWriter",
        "IcsValidator",
        "SiteDataWriter",
        "DigestRenderer",
        "DigestPublisher",
        "HealthUpdater",
        "SourceProber",
    }
    with_object_fake = {protocol.__name__ for protocol, _ in PROTOCOL_FAKES}
    defined_elsewhere = {"HttpClient", "Adapter"}  # seminars.http and seminars.sources.base
    assert protocols - factories_and_reserved - callables == with_object_fake - defined_elsewhere


class TestImplementationTable:
    def test_paths_are_well_formed(self) -> None:
        for key, path in IMPLEMENTATIONS.items():
            assert re.fullmatch(r"seminars(\.[a-z_]+)+:[a-z_]+", path), key

    def test_docstring_table_matches_the_mapping(self) -> None:
        rows = re.findall(
            r"^([a-z_]+) +(seminars\.[\w.]+:\w+) +\w+$", contracts.__doc__ or "", re.M
        )
        assert dict(rows) == IMPLEMENTATIONS

    def test_paths_follow_the_file_ownership(self) -> None:
        modules = {path.split(":")[0] for path in IMPLEMENTATIONS.values()}
        assert modules == {
            "seminars.llm.resolve",
            "seminars.llm.cache",
            "seminars.classify.rules",
            "seminars.classify.llm_tagger",
            "seminars.classify.filters",
            "seminars.classify.picks",
            "seminars.sources.generic.llm_page",
            "seminars.outputs.ics",
            "seminars.outputs.site_data",
            "seminars.outputs.digest",
            "seminars.outputs.health",
            "seminars.github",
            "seminars.pipeline.secret_scan",
            "seminars.sources.probe",
        }

    @pytest.mark.parametrize("key", sorted(IMPLEMENTATIONS))
    def test_modules_that_exist_expose_their_attribute(self, key: str) -> None:
        module_name = IMPLEMENTATIONS[key].split(":")[0]
        if importlib.util.find_spec(module_name) is None:
            pytest.skip(f"{module_name} is not implemented yet")
        assert callable(load_implementation(key))


class TestLoadImplementation:
    def test_unknown_key(self) -> None:
        with pytest.raises(KeyError, match="unknown contract 'nope'"):
            load_implementation("nope")

    def test_missing_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(IMPLEMENTATIONS, "demo", "seminars.outputs.not_written_yet:run")
        with pytest.raises(ContractNotImplementedError) as caught:
            load_implementation("demo")
        assert "module seminars.outputs.not_written_yet does not exist" in str(caught.value)
        assert isinstance(caught.value, ImportError)

    def test_missing_attribute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(IMPLEMENTATIONS, "demo", "seminars.models:not_there")
        with pytest.raises(ContractNotImplementedError, match="has no attribute 'not_there'"):
            load_implementation("demo")

    def test_present_implementation_is_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(IMPLEMENTATIONS, "demo", "seminars.models:dump_events")
        from seminars.models import dump_events

        assert load_implementation("demo") is dump_events

    def test_broken_import_inside_an_existing_module_is_not_masked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "half_written.py").write_text(
            "import missing_dependency_xyz\n", encoding="utf-8"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        importlib.invalidate_caches()
        monkeypatch.setitem(IMPLEMENTATIONS, "demo", "half_written:run")
        with pytest.raises(ModuleNotFoundError) as caught:
            load_implementation("demo")
        assert not isinstance(caught.value, ContractNotImplementedError)
        assert caught.value.name == "missing_dependency_xyz"
        sys.modules.pop("half_written", None)


class TestLLMErrors:
    @pytest.mark.parametrize(
        ("error_type", "kind"),
        [
            (LLMAuthError, "auth"),
            (LLMQuotaError, "quota"),
            (LLMRateLimitError, "rate_limit"),
            (LLMTimeoutError, "timeout"),
            (LLMNetworkError, "network"),
        ],
    )
    def test_provider_failures_trigger_failover(
        self, error_type: type[LLMError], kind: str
    ) -> None:
        error = error_type("no luck", provider="claude-cli")
        assert isinstance(error, LLMProviderError)
        assert error.failure_kind == kind
        assert error.provider == "claude-cli"
        assert str(error) == "claude-cli: no luck"

    @pytest.mark.parametrize(
        "error_type", [LLMInvalidOutputError, LLMBudgetExceededError, LLMUnavailableError]
    )
    def test_other_errors_do_not_trigger_failover(self, error_type: type[LLMError]) -> None:
        error = error_type("stop")
        assert isinstance(error, LLMError)
        assert not isinstance(error, LLMProviderError)
        assert error.provider is None
        assert str(error) == "stop"

    def test_failure_kinds_are_distinct(self) -> None:
        kinds = [
            error_type.failure_kind
            for error_type in (
                LLMAuthError,
                LLMQuotaError,
                LLMRateLimitError,
                LLMTimeoutError,
                LLMNetworkError,
                LLMInvalidOutputError,
            )
        ]
        assert len(set(kinds)) == 6


class TestHelpers:
    def test_cache_key_is_stable_and_separates_its_parts(self) -> None:
        key = llm_cache_key("v1", "tag", "some talk")
        assert key == llm_cache_key("v1", "tag", "some talk")
        assert re.fullmatch(r"[0-9a-f]{64}", key)
        assert key != llm_cache_key("v2", "tag", "some talk")
        assert key != llm_cache_key("v1", "extract", "some talk")
        assert llm_cache_key("v1", "ta", "gsome talk") != key

    def test_issue_labels(self) -> None:
        assert [label.name for label in ISSUE_LABELS] == ["digest", "source-broken", "llm-auth"]
