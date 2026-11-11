"""The source stage: fetch, parse, LLM fallback and normalization of every source.

Each source runs on its own. Whatever goes wrong with one of them (an
unknown adapter, a forbidden host, a used-up request budget, a time limit,
a page that cannot be parsed, a bug in an adapter) is recorded as the
failure of that source and nothing else; the run goes on with the others.

Fetching is the slow part: hosts ask for pauses of up to half a minute
between requests. The sources are therefore fetched side by side, one
worker thread each (at most ``MAX_FETCH_WORKERS`` at a time). The HTTP
client keeps requests to one host serial and paced whichever thread they
come from. Everything after the fetch (parsing, the LLM fallback,
normalization) happens on the calling thread, one source after the other
and in the order of the sources, so the result does not depend on which
fetch finished first and the LLM is never used from two threads.

Time limits. The live client bounds one exchange and one ``get``. On top of
that the fetch of one source may take ``SOURCE_FETCH_LIMIT_SECONDS`` in
all: the limit is checked before every request, and a source that is over
it fails with ``SourceTimeLimitError``. One slow site can thus delay a run
by that much plus one last request, never stall it.

Error messages go into the run report, health issues and the status page.
They are one line of printable ASCII (``seminars.textrules.ascii_printable``),
credential-shaped parts are replaced, and they are cut to
``MAX_ERROR_CHARS``.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Final

from pydantic import ValidationError

from seminars.contracts import PageExtractor
from seminars.http import (
    CrawlDelayTooLongError,
    HostBudgetExceededError,
    HttpClient,
    HttpError,
    RobotsDisallowedError,
    RobotsUnavailableError,
)
from seminars.models import (
    Candidate,
    EventDraft,
    ExcludedEvent,
    ExtractionMethod,
    FetchedDocument,
    RawPayload,
    SourceConfig,
    SourceErrorKind,
    SourceRunResult,
    TimeWindow,
)
from seminars.pipeline.normalize import NormalizeContext, normalize_drafts
from seminars.sources.base import (
    ForeignHostError,
    ParseError,
    UnknownAdapterError,
    get_adapter,
)
from seminars.textrules import ascii_printable

logger = logging.getLogger(__name__)

SOURCE_FETCH_LIMIT_SECONDS: Final = 900.0
"""How long the fetch of one source may take in all (15 minutes)."""

MAX_FETCH_WORKERS: Final = 8
MAX_ERROR_CHARS: Final = 300

Timer = Callable[[], float]
"""A monotonic clock in seconds; only differences are used."""

HttpFactory = Callable[[SourceConfig], HttpClient]
"""Gives the HTTP client a source is fetched with."""


class SourceTimeLimitError(HttpError):
    """The fetch of one source took longer than ``SOURCE_FETCH_LIMIT_SECONDS``."""

    def __init__(self, url: str, limit: float) -> None:
        super().__init__(url, f"the source used up its {limit:g} seconds; not requested")


class BoundedHttpClient:
    """``HttpClient`` of one source: counts its requests and enforces its time limit."""

    def __init__(self, inner: HttpClient, *, limit: float, timer: Timer) -> None:
        self._inner = inner
        self._limit = limit
        self._timer = timer
        self._started = timer()
        self.requests = 0

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        allow_statuses: Collection[int] = (),
    ) -> FetchedDocument:
        if self._timer() - self._started > self._limit:
            raise SourceTimeLimitError(url, self._limit)
        self.requests += 1
        return self._inner.get(url, headers=headers, allow_statuses=allow_statuses)


@dataclass(frozen=True)
class Fetched:
    """What fetching one source gave: a payload or the error that stopped it."""

    source: SourceConfig
    payload: RawPayload | None = None
    error: Exception | None = None
    requests: int = 0
    seconds: float = 0.0


@dataclass(frozen=True)
class SourceOutcome:
    """One source after the whole stage."""

    result: SourceRunResult
    candidates: tuple[Candidate, ...] = ()
    invalid: tuple[ExcludedEvent, ...] = ()


def error_kind(error: Exception) -> SourceErrorKind:
    """The category of a source failure for the run report."""
    if isinstance(error, UnknownAdapterError | ForeignHostError):
        return "config"
    if isinstance(error, RobotsDisallowedError | RobotsUnavailableError | CrawlDelayTooLongError):
        return "robots"
    if isinstance(error, HostBudgetExceededError):
        return "budget"
    if isinstance(error, HttpError):
        return "http"
    if isinstance(error, ParseError | ValidationError):
        return "parse"
    return "internal"


def describe_error(error: Exception, redact: Callable[[str], str]) -> str:
    """One printable ASCII line about ``error`` that is safe to publish."""
    if isinstance(error, ValidationError):
        detail = f"{error.error_count()} value(s) do not fit the schema"
    else:
        detail = redact(str(error))
    text = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
    return ascii_printable(text)[:MAX_ERROR_CHARS]


def _fetch(
    source: SourceConfig, http_for: HttpFactory, window: TimeWindow, timer: Timer, limit: float
) -> Fetched:
    started = timer()
    client: BoundedHttpClient | None = None
    try:
        adapter = get_adapter(source.adapter)
        client = BoundedHttpClient(http_for(source), limit=limit, timer=timer)
        payload = adapter.fetch(client, source, window=window)
        if not isinstance(payload, RawPayload) or payload.source_id != source.id:
            raise TypeError("the adapter did not return a payload of its source")
        return Fetched(source, payload, None, client.requests, timer() - started)
    except Exception as error:  # one source must never stop the others
        requests = client.requests if client is not None else 0
        return Fetched(source, None, error, requests, timer() - started)


def fetch_sources(
    sources: Sequence[SourceConfig],
    http_for: HttpFactory,
    *,
    window: TimeWindow,
    timer: Timer,
    limit: float = SOURCE_FETCH_LIMIT_SECONDS,
    workers: int = MAX_FETCH_WORKERS,
) -> list[Fetched]:
    """Fetch every source, several at a time; the result is in the order of ``sources``."""
    if not sources:
        return []

    def fetch(source: SourceConfig) -> Fetched:
        return _fetch(source, http_for, window, timer, limit)

    with ThreadPoolExecutor(
        max_workers=max(1, min(workers, len(sources))), thread_name_prefix="fetch"
    ) as pool:
        return list(pool.map(fetch, sources))


def _drafts(
    payload: RawPayload, source: SourceConfig, extractor: PageExtractor | None
) -> tuple[list[EventDraft], bool, list[str]]:
    """Drafts of a payload, whether the LLM fallback was used, and warnings."""
    adapter = get_adapter(source.adapter)
    failure: ParseError | None = None
    try:
        drafts = list(adapter.parse(payload, source))
    except ParseError as error:
        drafts, failure = [], error
    if drafts or not source.wants_llm_fallback:
        if failure is not None:
            raise failure
        return drafts, False, []
    if extractor is None:
        if failure is not None:
            raise failure
        return [], False, ["no LLM is available for the page extraction of this source"]
    extracted = list(extractor.extract(payload, source))
    if failure is not None and not extracted:
        raise failure
    warnings = ["rule-based parsing failed; the LLM extraction was used"] if failure else []
    return extracted, True, warnings


def _extraction(candidates: Sequence[Candidate]) -> ExtractionMethod | None:
    """The extraction method most events of a source came from."""
    counts: Counter[ExtractionMethod] = Counter(c.event.extraction for c in candidates)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ranked[0][0] if ranked else None


def _failed(
    fetched: Fetched, error: Exception, seconds: float, redact: Callable[[str], str]
) -> SourceOutcome:
    logger.warning("source %s failed (%s)", fetched.source.id, type(error).__name__)
    return SourceOutcome(
        result=SourceRunResult(
            source_id=fetched.source.id,
            status="failed",
            duration_seconds=round(max(seconds, 0.0), 3),
            requests=fetched.requests,
            error_kind=error_kind(error),
            error=describe_error(error, redact),
        )
    )


def digest_source(
    fetched: Fetched,
    *,
    context: NormalizeContext,
    extractor: PageExtractor | None,
    timer: Timer,
) -> SourceOutcome:
    """Parse and normalize what was fetched for one source; never raises for its problems."""
    started = timer()
    source = fetched.source
    if fetched.error is not None or fetched.payload is None:
        error = fetched.error or TypeError("the fetch gave neither a payload nor an error")
        return _failed(fetched, error, fetched.seconds, context.redact)
    try:
        drafts, fallback_used, warnings = _drafts(fetched.payload, source, extractor)
        normalized = normalize_drafts(drafts, source, context)
    except Exception as error:  # one source must never stop the others
        return _failed(fetched, error, fetched.seconds + timer() - started, context.redact)
    if normalized.redactions:
        warnings.append(
            f"{normalized.redactions} text(s) or address(es) held a credential or a meeting "
            "password and were withheld"
        )
    result = SourceRunResult(
        source_id=source.id,
        status="ok" if normalized.candidates else "empty",
        duration_seconds=round(max(fetched.seconds + timer() - started, 0.0), 3),
        requests=fetched.requests,
        drafts=len(drafts),
        events=len(normalized.candidates),
        invalid=len(normalized.invalid),
        outside_window=normalized.outside_window,
        extraction=_extraction(normalized.candidates),
        llm_fallback_used=fallback_used,
        warnings=warnings,
    )
    return SourceOutcome(result, normalized.candidates, normalized.invalid)


def collect_sources(
    sources: Sequence[SourceConfig],
    http_for: HttpFactory,
    *,
    context: NormalizeContext,
    extractor: PageExtractor | None,
    timer: Timer,
    limit: float = SOURCE_FETCH_LIMIT_SECONDS,
) -> list[SourceOutcome]:
    """Run the whole stage for ``sources``; one outcome per source, in their order."""
    fetched = fetch_sources(sources, http_for, window=context.window, timer=timer, limit=limit)
    return [
        digest_source(item, context=context, extractor=extractor, timer=timer) for item in fetched
    ]
