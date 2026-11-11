"""Every interface that crosses a package boundary, in one place.

The packages of this project (pipeline, classify, llm, outputs, sources) never
import each other directly. They depend on the protocols below, and the
pipeline obtains the implementations lazily through ``load_implementation``.
Each contract has a fake in tests/fakes.py (and a fixture in
tests/conftest.py), so every package can be tested on its own.

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

Command line surface that workflows may rely on (SPEC sections 7.3, 8, 9)::

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

import hashlib
import importlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, ClassVar, Final, Protocol, runtime_checkable

from pydantic import BaseModel

from seminars.config import AppConfig, Interests, LLMConfig, Settings, Taxonomy
from seminars.httpbase import HttpClient
from seminars.models import (
    Candidate,
    DigestDocument,
    DigestOutcome,
    Event,
    EventDraft,
    FilterResult,
    HealthFile,
    Issue,
    IssueLabel,
    IssueStateFilter,
    LastRun,
    LLMFailureKind,
    LLMProviderResult,
    LLMTagResult,
    LLMUsage,
    PicksResult,
    ProbeResult,
    RawPayload,
    RunReport,
    ScopeResult,
    SecretFinding,
    SourceConfig,
    TagDecision,
)

# --- LLM layer (SPEC section 7) ------------------------------------------------------------


class LLMError(Exception):
    """Base class of LLM errors. Messages never contain a credential or a prefix of one."""

    failure_kind: ClassVar[LLMFailureKind] = "other"

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        self.provider = provider
        super().__init__(f"{provider}: {message}" if provider else message)


class LLMProviderError(LLMError):
    """The provider could not serve the request; the chain moves on to the next provider."""


class LLMAuthError(LLMProviderError):
    """Credential missing, invalid or expired."""

    failure_kind = "auth"


class LLMQuotaError(LLMProviderError):
    """Subscription allowance or prepaid credit is used up."""

    failure_kind = "quota"


class LLMRateLimitError(LLMProviderError):
    """Temporarily throttled by the provider."""

    failure_kind = "rate_limit"


class LLMTimeoutError(LLMProviderError):
    """No answer within the configured timeout."""

    failure_kind = "timeout"


class LLMNetworkError(LLMProviderError):
    """Connection problem, or a CLI provider that could not be started."""

    failure_kind = "network"


class LLMInvalidOutputError(LLMError):
    """The provider answered, but not with data that validates against the schema.

    The answer is discarded. This is not a reason to switch providers.
    """

    failure_kind = "invalid_output"


class LLMBudgetExceededError(LLMError):
    """The request was not sent: call cap of the run reached, or the input is too long."""


class LLMUnavailableError(LLMError):
    """Every provider of the chain failed; callers continue without the LLM."""


@runtime_checkable
class LLMClient(Protocol):
    """One LLM backend (SPEC section 7.1)."""

    @property
    def name(self) -> str:
        """Provider id, e.g. ``claude-cli``."""
        ...

    def complete_json[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> T:
        """Return one answer validated against ``schema``; raise an ``LLMError`` otherwise."""
        ...


@runtime_checkable
class LLMChainClient(LLMClient, Protocol):
    """The provider chain seen as a single client (SPEC section 7.4).

    ``complete_json`` tries the providers in order, skips to the next one on
    ``LLMProviderError`` and marks the failed provider unavailable for the
    rest of the run. It raises ``LLMUnavailableError`` when none is left,
    ``LLMBudgetExceededError`` when ``max_calls_per_run`` or
    ``max_input_chars`` would be exceeded, and ``LLMInvalidOutputError`` when
    the answer does not validate.
    """

    def usage(self) -> LLMUsage:
        """Everything the run report and health need: counts, cost, per-provider results."""
        ...


class LLMClientFactory(Protocol):
    """``seminars.llm.resolve:build_llm_client``."""

    def __call__(self, config: LLMConfig, env: Mapping[str, str]) -> LLMChainClient | None:
        """Build the chain from ``config.providers`` (environment overrides already applied).

        Only providers whose credential is present in ``env`` join the chain.
        Returns ``None`` when the chain is empty (rules-only mode). Must not
        raise for a missing or broken provider.
        """
        ...


class LLMProber(Protocol):
    """``seminars.llm.resolve:probe_providers`` (backs ``seminars llm-smoke``)."""

    def __call__(self, config: LLMConfig, env: Mapping[str, str]) -> list[LLMProviderResult]:
        """Send one cheap request to every configured provider and report each outcome."""
        ...


@runtime_checkable
class LLMCache(Protocol):
    """Result cache under data/llm_cache/ (SPEC section 7.6). Keys come from ``llm_cache_key``."""

    def get(self, key: str) -> dict[str, Any] | None:
        """The cached JSON object, or ``None``. A hit refreshes the entry's age."""
        ...

    def put(self, key: str, value: Mapping[str, Any]) -> None: ...

    def prune(self) -> int:
        """Drop entries that were not hit for the configured number of days; return the count."""
        ...


class LLMCacheFactory(Protocol):
    """``seminars.llm.cache:open_llm_cache``."""

    def __call__(self, directory: Path, *, ttl_days: int) -> LLMCache: ...


def llm_cache_key(prompt_version: str, task: str, payload: str) -> str:
    """Cache key of one LLM request: SHA-256 over prompt version, task and normalized input.

    Model and provider are deliberately not part of the key, so changing the
    credential, the plan or the model never causes repeated calls.
    """
    material = json.dumps([prompt_version, task, payload], ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# --- classification (SPEC sections 5.5 to 5.7) ---------------------------------------------


@runtime_checkable
class FieldTagger(Protocol):
    """Rule-based field tagging: source defaults first, then keyword and series rules."""

    def tag(self, candidate: Candidate) -> TagDecision:
        """Decide the fields of one candidate; set ``needs_llm`` when the rules cannot."""
        ...


class FieldTaggerFactory(Protocol):
    """``seminars.classify.rules:build_field_tagger``."""

    def __call__(self, taxonomy: Taxonomy) -> FieldTagger: ...


@runtime_checkable
class LLMFieldTagger(Protocol):
    """Field tagging by LLM, batched and cached."""

    def tag_batch(self, candidates: Sequence[Candidate]) -> dict[str, LLMTagResult]:
        """Answers keyed by event uid.

        A uid is missing from the result when no answer could be obtained
        (provider failure, call cap, input too long); the rule decision then
        stands. This method never raises ``LLMError``.
        """
        ...


class LLMTaggerFactory(Protocol):
    """``seminars.classify.llm_tagger:build_llm_tagger``."""

    def __call__(
        self, client: LLMClient, *, config: LLMConfig, cache: LLMCache | None = None
    ) -> LLMFieldTagger: ...


@runtime_checkable
class EventFilter(Protocol):
    """Removes what is not an academic talk, and later what is out of scope."""

    def filter(self, candidates: Sequence[Candidate]) -> FilterResult:
        """Apply the exclusion rules of SPEC section 5.5 before de-duplication."""
        ...

    def drop_out_of_scope(self, events: Sequence[Event]) -> ScopeResult:
        """After classification: exclude events without any field (``out_of_scope``)."""
        ...


class EventFilterFactory(Protocol):
    """``seminars.classify.filters:build_event_filter``."""

    def __call__(self, taxonomy: Taxonomy, settings: Settings) -> EventFilter: ...


@runtime_checkable
class PicksScorer(Protocol):
    """Scores events against config/interests.yaml."""

    @property
    def threshold(self) -> float:
        """An event is a pick when its score is at least this value."""
        ...

    def score(self, event: Event) -> PicksResult: ...


class PicksScorerFactory(Protocol):
    """``seminars.classify.picks:build_picks_scorer``."""

    def __call__(self, interests: Interests) -> PicksScorer: ...


@runtime_checkable
class PicksReranker(Protocol):
    """Reserved for ``interests.llm_rerank`` (off by default, not implemented yet)."""

    def rerank(self, events: Sequence[Event]) -> dict[str, PicksResult]:
        """New scores keyed by event uid; events left out keep their rule-based score."""
        ...


# --- LLM page extraction fallback (SPEC sections 2 and 7.7) --------------------------------


@runtime_checkable
class PageExtractor(Protocol):
    """Extracts events from fetched pages with the LLM when rule-based parsing fails.

    The pipeline calls it only for sources with ``wants_llm_fallback`` whose
    adapter returned no drafts or raised ``ParseError``.
    """

    def extract(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        """Drafts with ``extraction="llm"`` and ``source_text`` set.

        Every text field is grounded in the page text and every URL occurs in
        the page; ungrounded fields are dropped. Returns an empty list when
        the LLM is unavailable; never raises ``LLMError``.
        """
        ...


class PageExtractorFactory(Protocol):
    """``seminars.sources.generic.llm_page:build_page_extractor``.

    Extraction requests must never be served by a provider listed in
    ``seminars.config.TAG_ONLY_PROVIDERS``.
    """

    def __call__(
        self, client: LLMClient, *, config: LLMConfig, cache: LLMCache | None = None
    ) -> PageExtractor: ...


# --- outputs (SPEC section 6) --------------------------------------------------------------


class IcsFeedWriter(Protocol):
    """``seminars.outputs.ics:write_ics_feeds``."""

    def __call__(self, events: Sequence[Event], out_dir: Path, *, config: AppConfig) -> list[Path]:
        """Write ``<out_dir>/<feed id>.ics`` for all nine FEED_IDS; return the paths.

        Output is a pure function of the arguments: the same events give the
        same bytes.
        """
        ...


class IcsValidator(Protocol):
    """``seminars.outputs.ics:validate_ics`` (backs ``seminars validate``)."""

    def __call__(self, text: str) -> list[str]:
        """Problems found in one ICS document; an empty list means valid."""
        ...


class SiteDataWriter(Protocol):
    """``seminars.outputs.site_data:write_site_data``."""

    def __call__(
        self,
        events: Sequence[Event],
        health: HealthFile,
        last_run: LastRun | None,
        out_dir: Path,
        *,
        config: AppConfig,
        generated_at: datetime,
    ) -> list[Path]:
        """Write ``<out_dir>/events.json`` (PublicEventsPayload) and
        ``<out_dir>/status.json`` (PublicStatusPayload) with ``dump_json``; return the paths."""
        ...


class DigestRenderer(Protocol):
    """``seminars.outputs.digest:render_digest``."""

    def __call__(
        self,
        events: Sequence[Event],
        health: HealthFile,
        *,
        config: AppConfig,
        week_start: date,
        previous_issue_at: datetime | None,
    ) -> DigestDocument:
        """Render the digest for the week starting on ``week_start`` (a Monday).

        Events first seen after ``previous_issue_at`` form the "new" section;
        ``None`` means this is the first issue.
        """
        ...


class DigestPublisher(Protocol):
    """``seminars.outputs.digest:publish_digest`` (backs ``seminars digest``)."""

    def __call__(
        self,
        events: Sequence[Event],
        health: HealthFile,
        issues: IssueClient | None,
        *,
        config: AppConfig,
        now: datetime,
        owner: str | None = None,
        dry_run: bool = False,
    ) -> DigestOutcome:
        """Open this week's digest issue and close the previous one.

        Idempotent per week: if an issue with this week's title exists,
        nothing is changed. ``owner`` is mentioned in the body. With
        ``dry_run`` (or without an issue client) nothing is written.
        """
        ...


class HealthUpdater(Protocol):
    """``seminars.outputs.health:update_health``."""

    def __call__(
        self,
        health: HealthFile,
        report: RunReport,
        issues: IssueClient | None,
        *,
        config: AppConfig,
    ) -> HealthFile:
        """Append the run to the history and open, update or close health issues."""
        ...


# --- GitHub issues -------------------------------------------------------------------------

LABEL_DIGEST: Final = IssueLabel(name="digest", color="0e8a16", description="Weekly seminar digest")
LABEL_SOURCE_BROKEN: Final = IssueLabel(
    name="source-broken", color="d93f0b", description="A data source keeps failing"
)
LABEL_LLM_AUTH: Final = IssueLabel(
    name="llm-auth", color="fbca04", description="An LLM provider needs a new credential"
)
ISSUE_LABELS: Final = (LABEL_DIGEST, LABEL_SOURCE_BROKEN, LABEL_LLM_AUTH)


@runtime_checkable
class IssueClient(Protocol):
    """The GitHub issue operations used by health tracking and the digest."""

    def ensure_label(self, label: IssueLabel) -> None:
        """Create the label if the repository does not have it yet."""
        ...

    def find_by_label(self, label: str, *, state: IssueStateFilter = "open") -> list[Issue]:
        """Issues carrying ``label``, newest first."""
        ...

    def find_by_title(self, title: str, *, state: IssueStateFilter = "all") -> Issue | None:
        """The newest issue whose title equals ``title`` exactly."""
        ...

    def open(self, title: str, body: str, *, labels: Sequence[str] = ()) -> Issue: ...

    def update(self, number: int, *, title: str | None = None, body: str | None = None) -> Issue:
        """Change title and/or body of an issue; ``None`` leaves a part unchanged."""
        ...

    def close(self, number: int, *, comment: str | None = None) -> None:
        """Close an issue, optionally leaving a final comment."""
        ...


class IssueClientFactory(Protocol):
    """``seminars.github:build_issue_client``."""

    def __call__(self, env: Mapping[str, str]) -> IssueClient | None:
        """A client for GITHUB_REPOSITORY using GITHUB_TOKEN; ``None`` when either is unset."""
        ...


# --- secret scan and source probe ----------------------------------------------------------


@runtime_checkable
class SecretScanner(Protocol):
    """Finds credential-like strings before anything is written to data/ or public/."""

    def scan_text(self, text: str, *, origin: str) -> list[SecretFinding]: ...

    def scan_paths(self, paths: Iterable[Path]) -> list[SecretFinding]:
        """Scan files; directories are scanned recursively."""
        ...


class SecretScannerFactory(Protocol):
    """``seminars.pipeline.secret_scan:build_secret_scanner``."""

    def __call__(self) -> SecretScanner: ...


class SourceProber(Protocol):
    """``seminars.sources.probe:probe_source`` (backs ``seminars add-source``)."""

    def __call__(self, http: HttpClient, url: str) -> ProbeResult:
        """Fetch ``url`` once, recognise its format cheaply and suggest a YAML entry."""
        ...


# --- lazy access to the implementations ----------------------------------------------------

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
