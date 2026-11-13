"""Every interface that crosses a package boundary, in one place.

The packages of this project (pipeline, classify, llm, outputs, sources) never
import each other directly. They depend on the protocols defined here, and
the pipeline obtains the implementations lazily through
``load_implementation``. Each contract has a fake in tests/ (fakes.py,
fakes_llm.py, fakes_outputs.py) and a fixture in tests/conftest.py, so every
package can be tested on its own.

Import everything from ``seminars.contracts``. The definitions live in:

* llm.py       LLMClient, LLMChainClient, the LLMError hierarchy, LLMCache
               (with the rules for the age of its entries), LLMFieldTagger,
               PageExtractor and their factories
* classify.py  FieldTagger, EventFilter, PicksScorer and their factories
* outputs.py   the ICS, site data, digest and health functions, IssueClient
* tools.py     SecretScanner, PrivateTextScrubber, SourceProber

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
private_scrubber     seminars.pipeline.privacy:scrub_private                 PrivateTextScrubber
source_probe         seminars.sources.probe:probe_source                     SourceProber
==================== ======================================================= ==================

Not in the table because they have their own homes: source adapters
(``seminars.sources.base``: protocol, registry, auto-discovery), the HTTP
client (``seminars.http``) and the command line (``seminars.cli:app``, a Typer
application).

How one run uses the contracts, in order::

    adapter    = get_adapter(source.adapter)           # every source, llm_page included
    payload    = adapter.fetch(http, source, window=window)
    drafts     = adapter.parse(payload, source)
    fallback   = PageExtractor.extract(payload, source)
                 # only when source.wants_llm_fallback and parse gave nothing or ParseError
    candidates = normalization of the drafts           # pipeline: Candidate(event, hints)
    filtered   = EventFilter.filter(candidates)        # exclusions by rule, defense switch
    merged     = de-duplication of filtered.kept       # pipeline: events and hints merge
    decision   = FieldTagger.tag(candidate)            # source defaults and rules
    answers    = LLMFieldTagger.tag_batch(open cases)  # needs_llm or uncertain; optional
    scoped     = EventFilter.drop_out_of_scope(events) # events without any field
    result     = PicksScorer.score(event)
    ... persist, then: write_ics_feeds, write_site_data, update_health
    ... output checks, before anything is committed or deployed:
        seminars.textrules.data_tree_problems(paths.data_dir)   # charset rule of data/
        SecretScanner over data/ and public/

The time of the run is one timezone-aware moment, fixed when the run starts:
the real time of a live run, ``seminars.fixtures.reference_time`` of an
offline one. Whatever needs "now" gets it handed in and never reads the
clock itself. That includes the LLM cache, which the pipeline opens as
``open_llm_cache(paths.llm_cache_dir, ttl_days=llm.cache_ttl_days, now=...)``
and whose entries age by the UTC date of that moment (see ``LLMCache``).

Who keeps data/ within the charset rule. Every file below data/ is committed
and checked by scripts/check_ascii.py (no emoji, no decorative or invisible
character, no raw control character), and the commits of the daily workflow
do not run the CI, so a violation has to be stopped before it is committed:

* normalization (pipeline) passes every text field of a draft through
  ``seminars.textrules.clean_text`` before it builds an ``Event``;
* ``ExcludedEvent`` cleans its own text and ``source_key`` escapes native
  ids, so the run report and the uid map are safe however they are filled;
* the LLM cache writes ASCII-only JSON (``seminars.models.dump_ascii_json``)
  and returns values unchanged; see ``LLMCache``;
* a lone surrogate, which decoded JSON can contain and no file can hold,
  never reaches a writer: models drop it from their strings and
  ``clean_text`` removes it (``seminars.textrules``, "Lone surrogates");
* ``seminars run`` verifies the result. After persisting, next to the secret
  scan, it calls ``data_tree_problems(paths.data_dir)``. Any problem is
  handled like a finding of the secret scan: it is stored in
  ``RunReport.charset_findings``, printed, and the command exits with
  status 3. ``seminars validate`` runs the same check and fails on it, so a
  workflow or a person can repeat it at any time.

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
3 an output check failed: the secret scan found something or a file below
data/ breaks the charset rule (nothing may be committed or deployed).
Status 1 of ``seminars run`` after it rewrote data/last_run.json with
``ok`` false (every source failed, or an output writer failed) means: the
update workflow commits only data/health.json, data/last_run.json and
data/run_report.json, never the events, deploys, and then fails the
workflow run. Any other non-zero status, and status 1 with any other
record or without a rewritten record (the run could not start, or an
error that nothing caught ended it), means: commit and deploy nothing,
even if files below data/ were written.

Two rules of ``seminars run`` that the update workflow relies on: a run
skipped by ``--skip-if-done-today`` writes nothing below data/, not even
data/last_run.json; every run that is neither skipped nor ``--dry-run``
rewrites data/last_run.json, also when nothing else changed. The workflow
decides from a change of that file whether to validate, commit, build and
deploy, so a skipped run that wrote it would deploy a second time.

Environment read by implementations: the credentials in
``seminars.config.PROVIDER_CREDENTIAL_ENV``, plus GITHUB_TOKEN,
GITHUB_REPOSITORY (``owner/name``) and GITHUB_REPOSITORY_OWNER for issues.
Whatever fetches from the network builds its client as
``PoliteClient(settings.http, cache_dir=paths.http_cache_dir())``, so that
processes which share that directory keep the crawl delay between them (see
``seminars.http``). The directory is ``.cache/http`` of the main checkout,
also when the command runs in a linked git worktree; SEMINARS_HTTP_CACHE_DIR
moves it.
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
    LLM_CACHE_REFRESH_CAP_DAYS,
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
    llm_cache_day,
    llm_cache_key,
    llm_cache_refresh_days,
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
    PrivateTextScrubber,
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
    "LLM_CACHE_REFRESH_CAP_DAYS",
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
    "PrivateTextScrubber",
    "SecretScanner",
    "SecretScannerFactory",
    "SiteDataWriter",
    "SourceProber",
    "llm_cache_day",
    "llm_cache_key",
    "llm_cache_refresh_days",
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
    "private_scrubber": "seminars.pipeline.privacy:scrub_private",
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
