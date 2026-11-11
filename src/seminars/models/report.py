"""Run report, its parts, and the compact last-run record (data/last_run.json)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from seminars.models.base import ExtractionMethod, Model, NonEmptyStr, SourceId, Timestamp
from seminars.models.classify import ExcludedEvent

Count = Annotated[int, Field(ge=0)]

SourceStatus = Literal["ok", "empty", "failed"]
"""``ok``: fetched and at least one event; ``empty``: fetched, zero events; ``failed``: error."""

SourceErrorKind = Literal[
    "config",  # bad source entry, unknown adapter
    "robots",  # robots.txt disallows the URL or is unreachable
    "budget",  # per-host request budget exhausted
    "http",  # status error, timeout, network error after retries
    "parse",  # the adapter could not make sense of the response
    "internal",  # any other exception
]
RunMode = Literal["live", "offline"]
LLMFailureKind = Literal[
    "auth", "quota", "rate_limit", "timeout", "network", "invalid_output", "other"
]
LLMProviderStatus = Literal["ok", "failed", "unused"]


class SourceRunResult(Model):
    """What happened to one source during one run.

    ``error`` is a short ASCII message without scraped text or credentials.
    """

    source_id: SourceId
    status: SourceStatus
    duration_seconds: Annotated[float, Field(ge=0)] = 0.0
    requests: Count = 0  # HTTP requests issued for this source
    drafts: Count = 0  # drafts returned by the adapter
    events: Count = 0  # drafts that became events inside the window
    invalid: Count = 0  # drafts dropped by normalization
    outside_window: Count = 0  # events dropped because they start outside the window
    extraction: ExtractionMethod | None = None
    llm_fallback_used: bool = False
    error_kind: SourceErrorKind | None = None
    error: str | None = None
    warnings: list[str] = []


class RunCounts(Model):
    """Totals of one run."""

    sources: Count = 0
    sources_ok: Count = 0
    sources_empty: Count = 0
    sources_failed: Count = 0
    drafts: Count = 0
    excluded: Count = 0  # length of RunReport.excluded
    merged: Count = 0  # events folded into another one by de-duplication
    events: Count = 0  # events in data/events.json after the run
    added: Count = 0
    changed: Count = 0
    missing: Count = 0  # future events not seen in this run but not yet deleted
    removed: Count = 0  # deleted after too many missing runs
    cancelled: Count = 0
    archived: Count = 0  # past events moved to data/archive/
    picks: Count = 0


class LLMProviderResult(Model):
    """Outcome of one LLM provider during one run; feeds health and the llm-auth issues.

    ``error`` must never contain a credential or a credential prefix.
    """

    provider: NonEmptyStr
    model: str | None = None
    status: LLMProviderStatus
    calls: Count = 0
    failures: Count = 0
    failure_kind: LLMFailureKind | None = None
    error: str | None = None
    warnings: list[str] = []  # e.g. configured model unavailable, CLI version mismatch


class LLMCacheStats(Model):
    """Counters of one ``LLMCache`` since it was opened."""

    hits: Count = 0  # lookups answered from the cache
    misses: Count = 0  # lookups that found nothing
    writes: Count = 0  # entries stored


class LLMUsage(Model):
    """LLM activity of one run, as reported by the provider chain.

    The chain cannot see the result cache, which sits in front of it inside
    the tagger and the page extractor, so it always reports ``cache_hits``
    as 0. The pipeline, which opens the cache, fills the field in:
    ``chain.usage().evolve(cache_hits=cache.stats().hits)``.
    """

    enabled: bool = False  # False in rules-only mode
    provider: str | None = None  # provider that answered the most recent successful call
    calls: Count = 0  # requests sent to providers
    cache_hits: Count = 0  # answers served from data/llm_cache/; set by the pipeline
    skipped_call_cap: Count = 0  # requests skipped because max_calls_per_run was reached
    skipped_too_long: Count = 0  # requests skipped because the input exceeded max_input_chars
    input_tokens: Count = 0
    output_tokens: Count = 0
    cost_usd: Annotated[float, Field(ge=0)] = 0.0
    providers: list[LLMProviderResult] = []


class SecretFinding(Model):
    """One hit of the secret scanner. Never contains the matched text itself."""

    origin: str  # file path or a label of the scanned text
    line: Annotated[int, Field(ge=1)]
    rule: NonEmptyStr  # e.g. "anthropic-api-key", "jwt", "refresh-token-keyword"


class LastRun(Model):
    """Compact record written to data/last_run.json on every run."""

    schema_version: Literal[1] = 1
    started_at: Timestamp
    finished_at: Timestamp
    ok: bool
    mode: RunMode
    counts: RunCounts = RunCounts()
    llm_provider: str | None = None
    failed_sources: list[SourceId] = []


class RunReport(Model):
    """Full result of ``seminars run``; written to data/run_report.json.

    ``ok`` is false only when every source failed or the secret scan found
    something. ``excluded`` lists every event that was filtered out so that
    false positives can be reviewed.
    """

    schema_version: Literal[1] = 1
    started_at: Timestamp
    finished_at: Timestamp
    mode: RunMode
    dry_run: bool = False
    ok: bool
    sources: list[SourceRunResult] = []
    excluded: list[ExcludedEvent] = []
    counts: RunCounts = RunCounts()
    llm: LLMUsage = LLMUsage()
    secret_findings: list[SecretFinding] = []
    warnings: list[str] = []

    def to_last_run(self) -> LastRun:
        """Condense the report into the record stored in data/last_run.json."""
        return LastRun(
            started_at=self.started_at,
            finished_at=self.finished_at,
            ok=self.ok,
            mode=self.mode,
            counts=self.counts,
            llm_provider=self.llm.provider,
            failed_sources=sorted(s.source_id for s in self.sources if s.status == "failed"),
        )
