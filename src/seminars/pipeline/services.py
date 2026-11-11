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
* LLM: only when it is wanted and ``build_llm_client`` finds a provider
  with a credential. When the LLM package cannot be loaded or set up, the
  run goes on without it and says so in a warning. The cache is opened
  with the time of the run, and the tagger and the page extractor share
  it. Without a provider there is no cache, no tagger and no extractor:
  the rules work alone.
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

from seminars.config import AppConfig
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
from seminars.models import SourceConfig
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
    llm_cache: LLMCache | None = None
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


@dataclass(frozen=True)
class _LLMParts:
    chain: LLMChainClient | None = None
    cache: LLMCache | None = None
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
    if chain is None:
        return _LLMParts()
    open_cache: LLMCacheFactory = load("llm_cache")
    cache = open_cache(cache_dir, ttl_days=config.llm.cache_ttl_days, now=now)
    build_tagger: LLMTaggerFactory = load("llm_tagger")
    build_extractor: PageExtractorFactory = load("page_extractor")
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
    http_for, close = _http(config, env, offline)
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
        llm_tagger=llm.tagger,
        page_extractor=llm.extractor,
        issues=issues,
        warnings=(*llm_warnings, *issue_warnings),
        close=close,
    )
