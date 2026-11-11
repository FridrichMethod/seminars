"""Contracts of the LLM layer: clients, errors, cache, LLM tagger and page extractor."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar, Final, Protocol, runtime_checkable

from pydantic import BaseModel

from seminars.config import LLMConfig
from seminars.models import (
    Candidate,
    EventDraft,
    LLMCacheStats,
    LLMFailureKind,
    LLMProviderResult,
    LLMTagResult,
    LLMUsage,
    RawPayload,
    SourceConfig,
)


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
    """One LLM backend."""

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
    """The provider chain seen as a single client.

    ``complete_json`` tries the providers in order, skips to the next one on
    ``LLMProviderError`` and marks the failed provider unavailable for the
    rest of the run. It raises ``LLMUnavailableError`` when none is left,
    ``LLMBudgetExceededError`` when ``max_calls_per_run`` or
    ``max_input_chars`` would be exceeded, and ``LLMInvalidOutputError`` when
    the answer does not validate.
    """

    def usage(self) -> LLMUsage:
        """Everything the run report and health need: counts, cost, per-provider results.

        ``cache_hits`` is always 0 here; see ``LLMUsage``.
        """
        ...

    def restricted(self, exclude: Collection[str]) -> LLMChainClient:
        """A view of this chain that never sends a request to the providers in ``exclude``.

        Everything else is shared with the chain it came from: the call cap
        of the run, the providers already marked unavailable and the usage
        (``usage()`` of the chain includes the calls made through the view).
        When no provider is left, ``complete_json`` of the view raises
        ``LLMUnavailableError``. Free-text extraction uses
        ``chain.restricted(TAG_ONLY_PROVIDERS)``; see ``PageExtractorFactory``.
        """
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


LLM_CACHE_REFRESH_CAP_DAYS: Final = 30
"""Upper limit of ``llm_cache_refresh_days``."""


def llm_cache_refresh_days(ttl_days: int) -> int:
    """How old the recorded date of a cache entry must be before a hit renews it.

    ``min(30, ttl_days // 2)`` days, called R in ``LLMCache``. The value is
    a trade: the larger it is, the fewer cache files a run rewrites (with 30
    an entry in daily use changes about once a month); it never exceeds half
    of the lifetime, so that an entry in use is renewed long before it could
    be pruned.
    """
    return min(LLM_CACHE_REFRESH_CAP_DAYS, ttl_days // 2)


def llm_cache_day(now: datetime) -> date:
    """The day a run counts as for the LLM cache: the UTC calendar date of ``now``.

    ``now`` is the time of the run and must be timezone-aware; a naive
    datetime raises ``ValueError``.
    """
    if now.utcoffset() is None:
        raise ValueError("the time of the run must be a timezone-aware datetime")
    return now.astimezone(UTC).date()


@runtime_checkable
class LLMCache(Protocol):
    """Result cache under data/llm_cache/. Keys come from ``llm_cache_key``.

    The cache directory is committed and checked by the charset gate like
    the rest of data/, while cached answers quote page text that was never
    normalized (no-break spaces, soft hyphens, zero width characters, emoji).
    An implementation must therefore write every file as ASCII-only JSON
    (``seminars.models.dump_ascii_json``, or ``json.dumps`` with
    ``ensure_ascii=True``) and must not clean the values: ``get`` returns
    exactly what ``put`` received. tests/test_llm_cache_contract.py checks
    this and the age rules below against the real implementation.

    The age of an entry
    -------------------

    Every entry records the day it last counted as used: a UTC calendar
    date, stored INSIDE the entry file. The time stamps of the file system
    are never used. git does not keep them: after a checkout in CI every
    file looks new, and a cache that looked at them would never prune
    anything there. "Today" is ``llm_cache_day(now)`` for the ``now`` the
    cache was opened with (``LLMCacheFactory``); nothing reads the wall
    clock.

    * ``put`` records today.
    * ``get``: a hit rewrites the entry only when its recorded date is at
      least R days before today, R = ``llm_cache_refresh_days(ttl_days)`` =
      ``min(30, ttl_days // 2)``; the entry then records today. Every other
      hit leaves the file as it is, byte for byte, and so does any hit on
      the recorded day itself (there is nothing to renew, also when R is
      0). The daily run commits data/, and without this rule each run would
      change every cache file it uses.
    * ``prune`` removes the entries whose recorded date is more than
      ``ttl_days`` days before today.
    * An entry dated after today (a run with an earlier ``now``, such as an
      offline run) is neither rewritten nor pruned.

    Guarantee: an entry that is hit at least once in every window of
    ``ttl_days - R`` days is never pruned. (The first hit that comes R or
    more days after the recorded date renews it, and with such use that hit
    comes at most ``ttl_days - 1`` days after that date.) With the defaults,
    ``ttl_days`` 180 and R 30, that is one hit in every 150 days.

    The other side of the trade: because a hit within R days of the
    recorded date does not move it, an entry that stops being used can be
    pruned up to R - 1 days sooner than ``ttl_days`` after its last hit.
    Once more than ``ttl_days`` have passed since its last hit, the next
    ``prune`` always removes it.
    """

    def get(self, key: str) -> dict[str, Any] | None:
        """The cached JSON object, or ``None``.

        A hit renews the recorded date of the entry when that date is at
        least R days old and changes nothing otherwise; see the class.
        """
        ...

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        """Store ``value`` (plain JSON data) under ``key`` in an ASCII-only file, dated today."""
        ...

    def prune(self) -> int:
        """Remove the entries recorded more than ``ttl_days`` days ago; return how many."""
        ...

    def stats(self) -> LLMCacheStats:
        """Hits, misses and writes since the cache was opened.

        ``writes`` counts ``put`` calls; a hit that renews a date is a hit.
        The pipeline copies ``hits`` into ``LLMUsage.cache_hits`` of the run report.
        """
        ...


class LLMCacheFactory(Protocol):
    """``seminars.llm.cache:open_llm_cache``."""

    def __call__(self, directory: Path, *, ttl_days: int, now: datetime) -> LLMCache:
        """Open the cache in ``directory`` (``paths.llm_cache_dir``), creating it when needed.

        ``ttl_days`` is ``LLMConfig.cache_ttl_days``. ``now`` is the time of
        the run as a timezone-aware datetime (``ValueError`` for a naive
        one) and the only source of time the cache has: its UTC date is
        "today" for everything ``LLMCache`` says about age. The pipeline
        passes the same moment it uses for the rest of the run, which in an
        offline run is ``seminars.fixtures.reference_time``; tests pass
        whatever day they need. The implementation must not read the wall
        clock or file times.

        Everything the cache writes below ``directory`` is an ASCII-only text
        file with LF line endings and a final newline; see ``LLMCache``.
        """
        ...


def llm_cache_key(prompt_version: str, task: str, payload: str) -> str:
    """Cache key of one LLM request: SHA-256 over prompt version, task and normalized input.

    Model and provider are deliberately not part of the key, so changing the
    credential, the plan or the model never causes repeated calls. Any
    string gives a key: a lone surrogate, which has no UTF-8 form, is hashed
    as the three bytes UTF-8 would use for its code point.
    """
    material = json.dumps([prompt_version, task, payload], ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8", "surrogatepass")).hexdigest()


# --- field tagging by LLM ------------------------------------------------------------------


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


# --- page extraction fallback --------------------------------------------------------------


@runtime_checkable
class PageExtractor(Protocol):
    """Extracts events from fetched pages with the LLM when rule-based parsing fails.

    The pipeline runs every source the same way, through the adapter
    registry: ``payload = adapter.fetch(...)``, then ``adapter.parse(payload,
    source)``. When the source has ``wants_llm_fallback`` and ``parse``
    returned no drafts or raised ``ParseError``, the pipeline calls
    ``extract`` with that same payload. Without an LLM client there is no
    extractor, and such a source simply yields no events.

    The pipeline never looks at the adapter name. Sources with
    ``adapter: llm_page`` take this path because the ``llm_page`` adapter
    parses nothing (see ``seminars.sources.base.GENERIC_ADAPTERS``).
    """

    def extract(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        """Drafts with ``extraction="llm"`` and ``source_text`` set.

        Every text field is grounded in the page text and every URL is a
        link of the page, made absolute with
        ``seminars.sources.base.absolute_url(document.final_url, link)``;
        ungrounded fields are dropped. Dates and all-day events follow the
        conventions of ``EventDraft``. Returns an empty list when the LLM is
        unavailable; never raises ``LLMError``.
        """
        ...


class PageExtractorFactory(Protocol):
    """``seminars.sources.generic.llm_page:build_page_extractor``.

    The same module must also register the adapter ``llm_page`` (``fetch``
    is ``seminars.sources.base.fetch_urls``, ``parse`` returns ``[]``), so
    that sources with ``adapter: llm_page`` resolve through the registry like
    any other source.

    Extraction requests must never be served by a provider listed in
    ``seminars.config.TAG_ONLY_PROVIDERS``. Enforcing that is the job of the
    extractor, not of its caller: the pipeline passes the provider chain as
    it is, and when ``client`` is an ``LLMChainClient`` the extractor sends
    every request through ``client.restricted(TAG_ONLY_PROVIDERS)``. A plain
    ``LLMClient`` is used as given.
    """

    def __call__(
        self, client: LLMClient, *, config: LLMConfig, cache: LLMCache | None = None
    ) -> PageExtractor: ...
