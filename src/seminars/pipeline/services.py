"""Everything a run works with that another package provides, in one bundle.

The pipeline never imports the classify, llm, outputs or sources packages.
It asks ``seminars.contracts.load_implementation`` for each implementation
and uses it through its protocol. This module is the only place where that
happens: ``build_services`` loads and wires the real implementations for a
command, and a test builds a ``Services`` from fakes instead.

How the parts are chosen:

* HTTP: an offline run gives every source a client that only sees the
  recorded fixtures of that source. A live run shares one polite client,
  built as ``PoliteClient(settings.http, cache_dir=paths.http_cache_dir())``,
  so that the pause per host and the request budgets hold for the whole
  run and for every other process that uses the same cache directory.
* LLM: only when it is wanted (not ``--no-llm``). The cache is opened
  with the time of the run. When ``build_llm_client`` finds a provider
  with a credential, the tagger and the page extractor share the chain
  and the cache. Without a provider the answers stored in earlier runs
  still apply, so that a missing credential gives the same data as a
  refused one: the tagger and the page extractor answer from the cache
  alone, through a client that never sends anything (``NoProvider``) and
  a view of the cache that stores nothing (``ReadOnlyCache``). Nothing new
  is asked, and the run does not prune that cache (``Services.llm_cache``
  stays ``None``); it only counts the stored answers it used
  (``Services.read_only_cache``, see ``LLMUsage.stored_answers_only``).
  When the LLM package cannot be loaded or set up, the run goes on
  without it and says so in a warning.
* GitHub issues: only when they are wanted (never offline, never in a dry
  run) and the environment names a repository and a token.
* The secret scanner also learns the exact values of the credentials in
  the environment, so that a leak is found whatever the value looks like.

The environment (``env``) holds credentials. Nothing in here logs it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from seminars.config import NO_PROVIDER, AppConfig
from seminars.contracts import (
    EventFilter,
    EventFilterFactory,
    FieldTagger,
    FieldTaggerFactory,
    HealthUpdater,
    IcsFeedWriter,
    IssueClient,
    IssueClientFactory,
    LLMCache,
    LLMCacheFactory,
    LLMChainClient,
    LLMClientFactory,
    LLMFieldTagger,
    LLMTaggerFactory,
    LLMUnavailableError,
    PageExtractor,
    PageExtractorFactory,
    PicksScorer,
    PicksScorerFactory,
    SecretScanner,
    SecretScannerFactory,
    SiteDataWriter,
    load_implementation,
)
from seminars.http import HttpClient, PoliteClient, offline_client
from seminars.models import LLMCacheStats, SourceConfig
from seminars.pipeline.secret_scan import PatternSecretScanner, known_credentials

logger = logging.getLogger(__name__)


def _nothing() -> None:
    return None


@dataclass(frozen=True)
class Services:
    """The collaborators of one run. ``close`` releases what they hold (connections)."""

    http_for: Callable[[SourceConfig], HttpClient]
    event_filter: EventFilter
    field_tagger: FieldTagger
    picks_scorer: PicksScorer
    write_ics_feeds: IcsFeedWriter
    write_site_data: SiteDataWriter
    update_health: HealthUpdater
    secret_scanner: SecretScanner
    llm: LLMChainClient | None = None
    llm_cache: LLMCache | None = None  # with a provider: counted and pruned by the run
    read_only_cache: LLMCache | None = None  # without one: only its hits are counted
    llm_tagger: LLMFieldTagger | None = None
    page_extractor: PageExtractor | None = None
    issues: IssueClient | None = None
    warnings: tuple[str, ...] = ()
    close: Callable[[], None] = _nothing


def load(key: str) -> Any:
    """``load_implementation``; the one name tests replace to hand out fakes."""
    return load_implementation(key)


def build_scanner(env: Mapping[str, str]) -> SecretScanner:
    """The secret scanner, taught the credentials of ``env`` when it can learn them."""
    factory: SecretScannerFactory = load("secret_scanner")
    scanner = factory()
    if isinstance(scanner, PatternSecretScanner):
        return scanner.knowing(known_credentials(env))
    return scanner


def redaction(scanner: SecretScanner) -> tuple[Callable[[str], str], Callable[[str], bool]]:
    """``(redact, is_secret)`` for normalization, backed by ``scanner``.

    The project's own scanner can replace what it finds. Any other
    ``SecretScanner`` can only say whether there is something; its
    ``redact`` then leaves the text alone and the final scan has the last
    word.
    """
    if isinstance(scanner, PatternSecretScanner):
        return scanner.redact, scanner.has_secret

    def is_secret(text: str) -> bool:
        return bool(scanner.scan_text(text, origin="text"))

    return (lambda text: text), is_secret


def live_client(config: AppConfig, env: Mapping[str, str]) -> PoliteClient:
    """The polite client of the project on the shared cache directory."""
    return PoliteClient(config.settings.http, cache_dir=config.paths.http_cache_dir(env))


def _http(
    config: AppConfig, env: Mapping[str, str], offline: bool
) -> tuple[Callable[[SourceConfig], HttpClient], Callable[[], None]]:
    if offline:
        fixtures_dir = config.paths.fixtures_dir

        def recorded(source: SourceConfig) -> HttpClient:
            return offline_client(source.id, fixtures_dir=fixtures_dir)

        return recorded, _nothing
    client = live_client(config, env)

    def shared(source: SourceConfig) -> HttpClient:
        return client

    return shared, client.close


class NoProvider:
    """The ``LLMClient`` of a run without a provider: it never sends anything.

    Every request fails at once with ``LLMUnavailableError``, so the LLM
    tagger and the page extractor answer from their cache alone and ask
    nothing new.
    """

    @property
    def name(self) -> str:
        return NO_PROVIDER

    def complete_json[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> T:
        raise LLMUnavailableError("no LLM provider is configured; only stored answers are used")


@dataclass(frozen=True)
class ReadOnlyCache:
    """An ``LLMCache`` that answers from ``cache`` and stores and prunes nothing.

    A hit still renews the recorded date of an old entry, as every hit of
    the cache does (``seminars.contracts.LLMCache``), so that the verdicts
    in use outlive a time without a provider.
    """

    cache: LLMCache

    def get(self, key: str) -> dict[str, Any] | None:
        return self.cache.get(key)

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        return None

    def prune(self) -> int:
        return 0

    def stats(self) -> LLMCacheStats:
        return self.cache.stats()


@dataclass(frozen=True)
class _LLMParts:
    chain: LLMChainClient | None = None
    cache: LLMCache | None = None
    read_only_cache: LLMCache | None = None
    tagger: LLMFieldTagger | None = None
    extractor: PageExtractor | None = None


def _llm(
    config: AppConfig, env: Mapping[str, str], now: datetime, cache_dir: Path
) -> tuple[_LLMParts, tuple[str, ...]]:
    """The LLM parts, or none with a warning when they cannot be set up.

    The LLM is optional by design: a run without it is a rules-only run,
    and that holds as well when its package is missing or broken.
    """
    try:
        return _llm_parts(config, env, now, cache_dir), ()
    except Exception as error:  # no LLM is no reason for a run to fail
        logger.warning("the LLM is off (%s)", type(error).__name__)
        return _LLMParts(), (f"the LLM is off: it could not be set up ({type(error).__name__})",)


def _llm_parts(
    config: AppConfig, env: Mapping[str, str], now: datetime, cache_dir: Path
) -> _LLMParts:
    build_chain: LLMClientFactory = load("llm_client")
    chain = build_chain(config.llm, env)
    open_cache: LLMCacheFactory = load("llm_cache")
    cache = open_cache(cache_dir, ttl_days=config.llm.cache_ttl_days, now=now)
    build_tagger: LLMTaggerFactory = load("llm_tagger")
    build_extractor: PageExtractorFactory = load("page_extractor")
    if chain is None:
        stored = ReadOnlyCache(cache)
        return _LLMParts(
            read_only_cache=stored,
            tagger=build_tagger(NoProvider(), config=config.llm, cache=stored),
            extractor=build_extractor(NoProvider(), config=config.llm, cache=stored),
        )
    return _LLMParts(
        chain=chain,
        cache=cache,
        tagger=build_tagger(chain, config=config.llm, cache=cache),
        extractor=build_extractor(chain, config=config.llm, cache=cache),
    )


def build_issues(env: Mapping[str, str]) -> tuple[IssueClient | None, tuple[str, ...]]:
    """The issue client of the environment, or ``None`` with a warning when it cannot be built."""
    try:
        factory: IssueClientFactory = load("issue_client")
        return factory(env), ()
    except Exception as error:  # issues are optional; a run goes on without them
        logger.warning("no issue client (%s)", type(error).__name__)
        return None, (
            f"GitHub issues are off: the client could not be built ({type(error).__name__})",
        )


def release_issues(issues: IssueClient | None) -> None:
    """Let go of the connections of an issue client that holds any (``release``)."""
    release = getattr(issues, "release", None)
    if callable(release):
        release()


def build_services(
    config: AppConfig,
    env: Mapping[str, str],
    *,
    now: datetime,
    offline: bool = False,
    use_llm: bool = True,
    with_issues: bool = True,
    llm_cache_dir: Path | None = None,
) -> Services:
    """Load and wire the implementations for one run (see the module docstring).

    ``llm_cache_dir`` defaults to data/llm_cache of the project; a dry run
    passes a scratch copy. Raises ``ContractNotImplementedError`` when a
    package this run needs is not there.
    """
    build_filter: EventFilterFactory = load("event_filter")
    build_tagger: FieldTaggerFactory = load("field_tagger")
    build_scorer: PicksScorerFactory = load("picks_scorer")
    write_feeds: IcsFeedWriter = load("ics_feeds")
    write_site: SiteDataWriter = load("site_data")
    update: HealthUpdater = load("health_update")
    scanner = build_scanner(env)
    cache_dir = llm_cache_dir or config.paths.llm_cache_dir
    llm, llm_warnings = _llm(config, env, now, cache_dir) if use_llm else (_LLMParts(), ())
    issues, issue_warnings = build_issues(env) if with_issues else (None, ())
    http_for, close_http = _http(config, env, offline)

    def close() -> None:
        close_http()
        release_issues(issues)

    return Services(
        http_for=http_for,
        event_filter=build_filter(config.taxonomy, config.settings),
        field_tagger=build_tagger(config.taxonomy),
        picks_scorer=build_scorer(config.interests),
        write_ics_feeds=write_feeds,
        write_site_data=write_site,
        update_health=update,
        secret_scanner=scanner,
        llm=llm.chain,
        llm_cache=llm.cache,
        read_only_cache=llm.read_only_cache,
        llm_tagger=llm.tagger,
        page_extractor=llm.extractor,
        issues=issues,
        warnings=(*llm_warnings, *issue_warnings),
        close=close,
    )
