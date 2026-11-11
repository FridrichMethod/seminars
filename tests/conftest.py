"""Shared pytest fixtures: one fake per cross-package contract, plus factories.

The classes behind the fixtures live in importable modules next to this file
(tests/ is on ``sys.path``), so a test can also build its own instances:

* factories.py      make_event, make_draft, make_source, ... and hypothesis strategies
* fakes.py          FakeClock, StubResolver, FakeHttpClient, FakeAdapter, FakeFieldTagger,
                    FakeEventFilter, FakePicksScorer, FakeIssueClient, FakeSecretScanner
* fakes_llm.py      FakeLLMClient, FakeLLMCache, FakeLLMFieldTagger, FakePageExtractor
* fakes_outputs.py  FakeEmitters (ICS, site data, digest, health, source probe)

Contract -> fixture:

* HttpClient -> fake_http / offline_http
* Adapter and the adapter registry -> fake_adapter (a FakeAdapter registered
  as "fake") / fake_registry (an empty process-wide registry)
* LLMClient and LLMChainClient -> fake_llm; LLMCache -> fake_llm_cache
* FieldTagger -> fake_field_tagger; LLMFieldTagger -> fake_llm_tagger
* EventFilter -> fake_event_filter; PicksScorer -> fake_picks_scorer
* PageExtractor -> fake_page_extractor
* IssueClient -> fake_issues; SecretScanner -> fake_secret_scanner
* the output functions and the source probe -> fake_emitters
* time -> fake_clock; name resolution -> resolver

No test may look a host name up for real. Every ``PoliteClient`` a test
builds gets ``resolver=resolver`` (or its own ``StubResolver``), and the
``no_real_dns`` fixture, which is always active, makes ``socket.getaddrinfo``
fail the test that reaches it.

Browser tests (``@pytest.mark.browser``) get Playwright from the
``playwright_sync_api`` fixture and never import it themselves; the fixture
explains why.
"""

from __future__ import annotations

import shutil
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import respx

import factories
from fakes import (
    FakeAdapter,
    FakeClock,
    FakeEventFilter,
    FakeFieldTagger,
    FakeHttpClient,
    FakeIssueClient,
    FakePicksScorer,
    FakeSecretScanner,
    StubResolver,
)
from fakes_llm import FakeLLMCache, FakeLLMClient, FakeLLMFieldTagger, FakePageExtractor
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig, HttpSettings, load_config
from seminars.http import OfflineHttpClient, offline_client
from seminars.models import Candidate, Event, EventDraft, SourceConfig
from seminars.sources import base
from seminars.sources.base import AdapterRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"


# --- time and HTTP -------------------------------------------------------------------------


@pytest.fixture
def fake_clock() -> FakeClock:
    """A clock that starts at factories.REFERENCE_NOW and never really sleeps."""
    return FakeClock()


@pytest.fixture
def resolver() -> StubResolver:
    """Name resolution for a ``PoliteClient``: every host has one fixed public address.

    Change ``resolver.answers`` or call ``resolver.script`` to make a host
    resolve to something else or not at all; ``resolver.calls`` lists the
    lookups. See ``fakes.StubResolver``.
    """
    return StubResolver()


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that reaches the real resolver.

    ``socket.getaddrinfo`` is what the default resolver of the live client
    and every real connection use. The error is not an ``OSError``, so no
    retry logic can swallow it. Child processes are not covered; scripts a
    test starts must pass their own stub.
    """

    def refuse(host: object, *args: object, **kwargs: object) -> None:
        raise AssertionError(
            f"a test tried a real DNS lookup of {host!r}: pass resolver=... to PoliteClient"
        )

    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture
def http_settings() -> HttpSettings:
    """HTTP settings with the project defaults (1 s interval, 3 retries, budget 20)."""
    return HttpSettings(user_agent="seminars-bot/1.0 (+https://example.org/seminars/)")


@pytest.fixture
def respx_router() -> Iterator[respx.MockRouter]:
    """A respx router; unmatched requests fail, unused routes are fine."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def fake_http(fake_clock: FakeClock) -> FakeHttpClient:
    """In-memory HTTP client; add pages with ``fake_http.add(url, text)``."""
    return FakeHttpClient(clock=fake_clock)


@pytest.fixture
def offline_http() -> Callable[..., OfflineHttpClient]:
    """Factory for offline HTTP clients: ``offline_http("sites-chemistry")``.

    Serves the recordings in tests/fixtures/<source_id>/ of this repository;
    pass ``fixtures_dir=...`` to read from another directory (e.g. tmp_path).
    """

    def build(*source_ids: str, fixtures_dir: Path = FIXTURES_DIR) -> OfflineHttpClient:
        return offline_client(*source_ids, fixtures_dir=fixtures_dir)

    return build


# --- source adapters -----------------------------------------------------------------------


@pytest.fixture
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> AdapterRegistry:
    """An empty adapter registry installed as the process-wide one for this test.

    It has no packages to discover, so ``get_adapter`` and ``adapter_names``
    only know what the test registers (``fake_registry.register(name)`` or
    ``FakeAdapter.register``). Code under test must go through those
    functions of ``seminars.sources.base``, not through a copy of ``REGISTRY``.

    The real registry discovers its adapters first. An adapter module that is
    imported for the first time during the test (``llm_page`` through
    ``load_implementation("page_extractor")``, for example) would otherwise
    register in this registry, and which adapters the test sees would depend
    on which tests ran before it.
    """
    base.REGISTRY.discover(strict=False)
    registry = AdapterRegistry(())
    monkeypatch.setattr(base, "REGISTRY", registry)
    return registry


@pytest.fixture
def fake_adapter(fake_registry: AdapterRegistry, fake_clock: FakeClock) -> FakeAdapter:
    """A ``FakeAdapter`` registered as ``fake`` in ``fake_registry``.

    Sources select it with ``adapter="fake"``; fill ``fake_adapter.drafts``
    and set ``fetch_error`` / ``parse_error`` as the test needs.
    """
    return FakeAdapter(clock=fake_clock).register(fake_registry, "fake")


# --- LLM and classification ----------------------------------------------------------------


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    """Scripted LLM client; see FakeLLMClient for ``script``, ``always`` and ``fail``."""
    return FakeLLMClient()


@pytest.fixture
def fake_llm_cache() -> FakeLLMCache:
    """An empty in-memory LLM cache opened on factories.REFERENCE_NOW (ttl 180 days).

    It follows the age rules of ``LLMCache``; move ``fake_llm_cache.today``
    to play a later run.
    """
    return FakeLLMCache()


@pytest.fixture
def fake_field_tagger() -> FakeFieldTagger:
    return FakeFieldTagger()


@pytest.fixture
def fake_llm_tagger() -> FakeLLMFieldTagger:
    return FakeLLMFieldTagger()


@pytest.fixture
def fake_event_filter() -> FakeEventFilter:
    return FakeEventFilter()


@pytest.fixture
def fake_picks_scorer() -> FakePicksScorer:
    return FakePicksScorer()


@pytest.fixture
def fake_page_extractor() -> FakePageExtractor:
    return FakePageExtractor()


# --- GitHub, secret scan, outputs ----------------------------------------------------------


@pytest.fixture
def fake_issues(fake_clock: FakeClock) -> FakeIssueClient:
    """In-memory GitHub issues sharing the fake clock for ``created_at``."""
    return FakeIssueClient(fake_clock)


@pytest.fixture
def fake_secret_scanner() -> FakeSecretScanner:
    return FakeSecretScanner()


@pytest.fixture
def fake_emitters() -> FakeEmitters:
    return FakeEmitters()


# --- browser tests -------------------------------------------------------------------------


@pytest.fixture(scope="session")
def playwright_sync_api() -> Any:
    """The ``playwright.sync_api`` module; the test is skipped when it is not installed.

    Playwright is in the optional ``browser`` extra, and the type checker has
    to pass both with and without it. A static ``import playwright`` fails
    without the extra (unresolved import), and an ignore comment on that
    import fails with the extra (unnecessary ignore comment). Browser tests
    therefore never import Playwright; they take the module from here::

        @pytest.mark.browser
        def test_home_page(playwright_sync_api):
            with playwright_sync_api.sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                ...

    tests/test_project_config.py fails when a module imports it statically.
    """
    return pytest.importorskip(
        "playwright.sync_api",
        reason="Playwright is not installed; run: uv sync --locked --extra browser",
    )


# --- configuration and model factories -----------------------------------------------------


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    """Configuration of a scratch project below ``tmp_path``.

    It has the repository's config/settings.yaml and locales/ and nothing
    else: default LLM settings, empty taxonomy and interests, no sources.
    All paths (data/, public/, ...) point into ``tmp_path``.
    """
    (tmp_path / "config").mkdir()
    shutil.copy(REPO_ROOT / "config" / "settings.yaml", tmp_path / "config" / "settings.yaml")
    if (REPO_ROOT / "locales").is_dir():
        shutil.copytree(REPO_ROOT / "locales", tmp_path / "locales")
    return load_config(tmp_path, env={})


@pytest.fixture
def make_event() -> Callable[..., Event]:
    """``factories.make_event``: a valid event, any field overridable by keyword."""
    return factories.make_event


@pytest.fixture
def make_draft() -> Callable[..., EventDraft]:
    """``factories.make_draft``."""
    return factories.make_draft


@pytest.fixture
def make_source() -> Callable[..., SourceConfig]:
    """``factories.make_source``."""
    return factories.make_source


@pytest.fixture
def make_candidate() -> Callable[..., Candidate]:
    """``factories.make_candidate``."""
    return factories.make_candidate
