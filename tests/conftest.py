"""Shared pytest fixtures: one fake per cross-package contract, plus factories.

The classes behind the fixtures live in importable modules next to this file
(tests/ is on ``sys.path``), so a test can also build its own instances:

* factories.py      make_event, make_draft, make_source, ... and hypothesis strategies
* fakes.py          FakeClock, FakeHttpClient, FakeFieldTagger, FakeEventFilter,
                    FakePicksScorer, FakeIssueClient, FakeSecretScanner
* fakes_llm.py      FakeLLMClient, FakeLLMCache, FakeLLMFieldTagger, FakePageExtractor
* fakes_outputs.py  FakeEmitters (ICS, site data, digest, health, source probe)

Contract -> fixture: HttpClient -> fake_http / offline_http; LLMClient and
LLMChainClient -> fake_llm; LLMCache -> fake_llm_cache; FieldTagger ->
fake_field_tagger; LLMFieldTagger -> fake_llm_tagger; EventFilter ->
fake_event_filter; PicksScorer -> fake_picks_scorer; PageExtractor ->
fake_page_extractor; IssueClient -> fake_issues; SecretScanner ->
fake_secret_scanner; the output functions and the source probe ->
fake_emitters; time -> fake_clock.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import respx

import factories
from fakes import (
    FakeClock,
    FakeEventFilter,
    FakeFieldTagger,
    FakeHttpClient,
    FakeIssueClient,
    FakePicksScorer,
    FakeSecretScanner,
)
from fakes_llm import FakeLLMCache, FakeLLMClient, FakeLLMFieldTagger, FakePageExtractor
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig, HttpSettings, load_config
from seminars.http import OfflineHttpClient, offline_client
from seminars.models import Candidate, Event, EventDraft, SourceConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"


# --- time and HTTP -------------------------------------------------------------------------


@pytest.fixture
def fake_clock() -> FakeClock:
    """A clock that starts at factories.REFERENCE_NOW and never really sleeps."""
    return FakeClock()


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


# --- LLM and classification ----------------------------------------------------------------


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    """Scripted LLM client; see FakeLLMClient for ``script``, ``always`` and ``fail``."""
    return FakeLLMClient()


@pytest.fixture
def fake_llm_cache() -> FakeLLMCache:
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
