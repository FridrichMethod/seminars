"""Every interface that crosses a package boundary, in one place.

The packages of this project (pipeline, classify, llm, outputs, sources) never
import each other directly. They depend on the protocols defined here, and
the pipeline obtains the implementations lazily through
``load_implementation``. Each contract has a fake in tests/ (fakes.py,
fakes_llm.py, fakes_outputs.py) and a fixture in tests/conftest.py, so every
package can be tested on its own.

Import everything from ``seminars.contracts``. The definitions live in:

* llm.py       LLMClient, LLMChainClient, the LLMError hierarchy, LLMCache,
               LLMFieldTagger, PageExtractor and their factories
* classify.py  FieldTagger, EventFilter, PicksScorer and their factories
* outputs.py   the ICS, site data, digest and health functions, IssueClient
* tools.py     SecretScanner, SourceProber

Where each contract is implemented (``module:attribute``, also available as
``IMPLEMENTATIONS[key]``):

==================== ======================================================= ==================
key                  implementation                                          contract type
==================== ======================================================= ==================
llm_client           seminars.llm.resolve:build_llm_client                   LLMClientFactory
llm_probe            seminars.llm.resolve:probe_providers                    LLMProber
llm_cache            seminars.llm.cache:open_llm_cache                       LLMCacheFactory
field_tagger         seminars.classify.rules:build_field_tagger              FieldTaggerFactory
llm_tagger           seminars.classify.llm_tagger:build_llm_tagger           LLMTaggerFactory
event_filter         seminars.classify.filters:build_event_filter            EventFilterFactory
picks_scorer         seminars.classify.picks:build_picks_scorer              PicksScorerFactory
page_extractor       seminars.sources.generic.llm_page:build_page_extractor  PageExtractorFactory
ics_feeds            seminars.outputs.ics:write_ics_feeds                    IcsFeedWriter
ics_validator        seminars.outputs.ics:validate_ics                       IcsValidator
site_data            seminars.outputs.site_data:write_site_data              SiteDataWriter
digest_render        seminars.outputs.digest:render_digest                   DigestRenderer
digest_publish       seminars.outputs.digest:publish_digest                  DigestPublisher
health_update        seminars.outputs.health:update_health                   HealthUpdater
issue_client         seminars.github:build_issue_client                      IssueClientFactory
secret_scanner       seminars.pipeline.secret_scan:build_secret_scanner      SecretScannerFactory
source_probe         seminars.sources.probe:probe_source                     SourceProber
==================== ======================================================= ==================

Not in the table because they have their own homes: source adapters
(``seminars.sources.base``: protocol, registry, auto-discovery), the HTTP
client (``seminars.http``) and the command line (``seminars.cli:app``, a Typer
application).

How one run uses the contracts, in order::

    payload    = adapter.fetch(http, source, window=window)
    drafts     = adapter.parse(payload, source)        # PageExtractor.extract as fallback
    candidates = normalization of the drafts           # pipeline: Candidate(event, hints)
    filtered   = EventFilter.filter(candidates)        # exclusions by rule
    merged     = de-duplication of filtered.kept       # pipeline: events and hints merge
    decision   = FieldTagger.tag(candidate)            # source defaults and rules
    answers    = LLMFieldTagger.tag_batch(open cases)  # needs_llm or uncertain; optional
    scoped     = EventFilter.drop_out_of_scope(events) # events without any field
    result     = PicksScorer.score(event)
    ... persist, then: write_ics_feeds, write_site_data, update_health
    ... SecretScanner over data/ and public/ before anything is committed or deployed

Command line surface that workflows may rely on::

    seminars run [--offline] [--source ID ...] [--no-llm] [--llm PROVIDER]
                 [--dry-run] [--skip-if-done-today]
    seminars llm-smoke                  # probe every configured provider, print the result
    seminars sources list
    seminars sources check [ID ...]
    seminars add-source URL
    seminars validate
    seminars digest [--dry-run]
    seminars build-site                 # assemble public/

Exit status: 0 success (a skipped run included), 1 failure, 2 usage error,
3 the secret scan found something (nothing may be committed or deployed).

Environment read by implementations: the credentials in
``seminars.config.PROVIDER_CREDENTIAL_ENV``, plus GITHUB_TOKEN,
GITHUB_REPOSITORY (``owner/name``) and GITHUB_REPOSITORY_OWNER for issues.
"""

from __future__ import annotations

import importlib
from typing import Any, Final

from seminars.contracts.classify import (
    EventFilter,
    EventFilterFactory,
    FieldTagger,
    FieldTaggerFactory,
    PicksReranker,
    PicksScorer,
    PicksScorerFactory,
)
from seminars.contracts.llm import (
    LLMAuthError,
    LLMBudgetExceededError,
    LLMCache,
    LLMCacheFactory,
    LLMChainClient,
    LLMClient,
    LLMClientFactory,
    LLMError,
    LLMFieldTagger,
    LLMInvalidOutputError,
    LLMNetworkError,
    LLMProber,
    LLMProviderError,
    LLMQuotaError,
    LLMRateLimitError,
    LLMTaggerFactory,
    LLMTimeoutError,
    LLMUnavailableError,
    PageExtractor,
    PageExtractorFactory,
    llm_cache_key,
)
from seminars.contracts.outputs import (
    ISSUE_LABELS,
    LABEL_DIGEST,
    LABEL_LLM_AUTH,
    LABEL_SOURCE_BROKEN,
    DigestPublisher,
    DigestRenderer,
    HealthUpdater,
    IcsFeedWriter,
    IcsValidator,
    IssueClient,
    IssueClientFactory,
    SiteDataWriter,
)
from seminars.contracts.tools import (
    SecretScanner,
    SecretScannerFactory,
    SourceProber,
)

__all__ = [
    "IMPLEMENTATIONS",
    "ISSUE_LABELS",
    "LABEL_DIGEST",
    "LABEL_LLM_AUTH",
    "LABEL_SOURCE_BROKEN",
    "ContractNotImplementedError",
    "DigestPublisher",
    "DigestRenderer",
    "EventFilter",
    "EventFilterFactory",
    "FieldTagger",
    "FieldTaggerFactory",
    "HealthUpdater",
    "IcsFeedWriter",
    "IcsValidator",
    "IssueClient",
    "IssueClientFactory",
    "LLMAuthError",
    "LLMBudgetExceededError",
    "LLMCache",
    "LLMCacheFactory",
    "LLMChainClient",
    "LLMClient",
    "LLMClientFactory",
    "LLMError",
    "LLMFieldTagger",
    "LLMInvalidOutputError",
    "LLMNetworkError",
    "LLMProber",
    "LLMProviderError",
    "LLMQuotaError",
    "LLMRateLimitError",
    "LLMTaggerFactory",
    "LLMTimeoutError",
    "LLMUnavailableError",
    "PageExtractor",
    "PageExtractorFactory",
    "PicksReranker",
    "PicksScorer",
    "PicksScorerFactory",
    "SecretScanner",
    "SecretScannerFactory",
    "SiteDataWriter",
    "SourceProber",
    "llm_cache_key",
    "load_implementation",
]

IMPLEMENTATIONS: Final[dict[str, str]] = {
    "llm_client": "seminars.llm.resolve:build_llm_client",
    "llm_probe": "seminars.llm.resolve:probe_providers",
    "llm_cache": "seminars.llm.cache:open_llm_cache",
    "field_tagger": "seminars.classify.rules:build_field_tagger",
    "llm_tagger": "seminars.classify.llm_tagger:build_llm_tagger",
    "event_filter": "seminars.classify.filters:build_event_filter",
    "picks_scorer": "seminars.classify.picks:build_picks_scorer",
    "page_extractor": "seminars.sources.generic.llm_page:build_page_extractor",
    "ics_feeds": "seminars.outputs.ics:write_ics_feeds",
    "ics_validator": "seminars.outputs.ics:validate_ics",
    "site_data": "seminars.outputs.site_data:write_site_data",
    "digest_render": "seminars.outputs.digest:render_digest",
    "digest_publish": "seminars.outputs.digest:publish_digest",
    "health_update": "seminars.outputs.health:update_health",
    "issue_client": "seminars.github:build_issue_client",
    "secret_scanner": "seminars.pipeline.secret_scan:build_secret_scanner",
    "source_probe": "seminars.sources.probe:probe_source",
}
"""Contract key -> ``module:attribute`` of its implementation (see the module docstring)."""


class ContractNotImplementedError(ImportError):
    """The module or attribute a contract points to does not exist (yet)."""


def load_implementation(key: str) -> Any:
    """Import and return the implementation registered under ``key``.

    The import happens at call time, so a package is only needed when it is
    actually used. Annotate the result with the contract type, for example
    ``build: FieldTaggerFactory = load_implementation("field_tagger")``.
    """
    try:
        module_name, attribute = IMPLEMENTATIONS[key].split(":")
    except KeyError:
        known = ", ".join(sorted(IMPLEMENTATIONS))
        raise KeyError(f"unknown contract {key!r} (known: {known})") from None
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name is None or not f"{module_name}.".startswith(f"{error.name}."):
            raise  # the module exists but one of its own imports is missing
        raise ContractNotImplementedError(
            f"contract {key!r} is not implemented: module {module_name} does not exist"
        ) from error
    try:
        return getattr(module, attribute)
    except AttributeError as error:
        raise ContractNotImplementedError(
            f"contract {key!r} is not implemented: {module_name} has no attribute {attribute!r}"
        ) from error
