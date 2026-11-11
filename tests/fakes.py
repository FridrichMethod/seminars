"""Fakes for every cross-node protocol, shared by all test modules.

Import them directly (``from fakes import FakeClock``) or use the fixtures
defined in conftest.py. Nothing in here touches the network or the clock.
"""

from __future__ import annotations

import threading
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from factories import REFERENCE_NOW
from seminars.http import FixtureNotFoundError, HttpClient, HttpStatusError
from seminars.models import (
    Candidate,
    Event,
    EventDraft,
    ExcludedEvent,
    ExclusionReason,
    FetchedDocument,
    FilterResult,
    Issue,
    IssueLabel,
    IssueState,
    IssueStateFilter,
    PicksResult,
    RawPayload,
    ScopeResult,
    SecretFinding,
    SourceConfig,
    TagDecision,
    TimeWindow,
)
from seminars.sources.base import AdapterRegistry


class FakeClock:
    """Deterministic time: calling it reads the time, ``sleep`` advances it.

    Pass the instance as ``clock`` and its ``sleep`` method as ``sleep`` to
    anything that takes injectable time, for example ``PoliteClient``.
    """

    def __init__(self, start: datetime = REFERENCE_NOW) -> None:
        self._now = start.timestamp()
        self._lock = threading.Lock()
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def now(self) -> datetime:
        """Current fake time as an aware UTC datetime."""
        return datetime.fromtimestamp(self(), tz=UTC)

    def sleep(self, seconds: float) -> None:
        """Record the request to sleep and jump ahead without waiting."""
        with self._lock:
            self.sleeps.append(seconds)
            self._now += seconds

    def advance(self, seconds: float) -> None:
        """Move time forward without recording a sleep."""
        with self._lock:
            self._now += seconds


class FakeHttpClient:
    """In-memory ``HttpClient``: serves the pages given to it and records requests.

    Use it when a test needs a few hand-written responses; use
    ``offline_client`` for recorded fixtures. Unknown URLs raise
    ``FixtureNotFoundError`` and error statuses behave like the live client.
    """

    def __init__(self, pages: Mapping[str, str] | None = None, *, clock: FakeClock | None = None):
        self._clock = clock or FakeClock()
        self._documents: dict[str, FetchedDocument] = {}
        self.requests: list[str] = []
        for url, text in (pages or {}).items():
            self.add(url, text)

    def add(
        self,
        url: str,
        text: str,
        *,
        status: int = 200,
        content_type: str | None = "text/html",
        final_url: str | None = None,
    ) -> None:
        """Register the response for ``url``."""
        self._documents[url] = FetchedDocument(
            url=url,
            final_url=final_url or url,
            status=status,
            content_type=content_type,
            text=text,
            fetched_at=self._clock.now(),
        )

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        allow_statuses: Collection[int] = (),
    ) -> FetchedDocument:
        self.requests.append(url)
        document = self._documents.get(url)
        if document is None:
            raise FixtureNotFoundError(url, "FakeHttpClient has no page for this URL")
        if not 200 <= document.status < 300 and document.status not in allow_statuses:
            raise HttpStatusError(url, document.status)
        return document.evolve(fetched_at=self._clock.now())


class FakeAdapter:
    """Scripted source ``Adapter`` for pipeline tests; it performs no I/O.

    ``fetch`` never touches the HTTP client: it returns a payload with one
    empty document for the first URL of the source, stamped with the fake
    clock. ``parse`` returns those of ``drafts`` whose ``source_id`` is the
    id of the source, so one instance can serve several sources.
    ``fetch_error`` / ``parse_error`` make the respective step raise (for
    example an ``HttpError`` or a ``ParseError``). ``calls`` lists every call
    as ``(method name, source id)`` and ``windows`` the window of every fetch.

    The registry builds adapters through zero-argument factories; ``register``
    makes a registry hand out this very instance::

        adapter = FakeAdapter([make_draft(source_id="sites-x")])
        adapter.register(fake_registry, "fake")
        source = make_source(id="sites-x", adapter="fake")

    The ``fake_adapter`` fixture does exactly that with the name ``fake``.
    """

    def __init__(
        self,
        drafts: Sequence[EventDraft] = (),
        *,
        fetch_error: Exception | None = None,
        parse_error: Exception | None = None,
        clock: FakeClock | None = None,
    ) -> None:
        self.drafts = list(drafts)
        self.fetch_error = fetch_error
        self.parse_error = parse_error
        self._clock = clock or FakeClock()
        self.calls: list[tuple[str, str]] = []
        self.windows: list[TimeWindow] = []

    def register(self, registry: AdapterRegistry, name: str = "fake") -> Self:
        """Register this instance in ``registry`` under ``name`` and return it."""
        registry.register(name)(lambda: self)
        return self

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        self.calls.append(("fetch", source.id))
        self.windows.append(window)
        if self.fetch_error is not None:
            raise self.fetch_error
        now = self._clock.now()
        url = source.all_urls[0]
        document = FetchedDocument(
            url=url, final_url=url, status=200, content_type="text/plain", text="", fetched_at=now
        )
        return RawPayload(source_id=source.id, fetched_at=now, window=window, documents=[document])

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        self.calls.append(("parse", source.id))
        if self.parse_error is not None:
            raise self.parse_error
        return [draft for draft in self.drafts if draft.source_id == source.id]


class FakeFieldTagger:
    """``FieldTagger`` that trusts the hints.

    The decision is ``hints.suggested_fields`` with method ``"source"``, and
    ``needs_llm`` when there are none. ``decisions`` overrides this per uid.
    """

    def __init__(self, decisions: Mapping[str, TagDecision] | None = None) -> None:
        self.decisions = dict(decisions or {})
        self.tagged: list[str] = []

    def tag(self, candidate: Candidate) -> TagDecision:
        uid = candidate.event.uid
        self.tagged.append(uid)
        if uid in self.decisions:
            return self.decisions[uid]
        fields = candidate.hints.suggested_fields
        return TagDecision(fields=fields, method="source", needs_llm=not fields)


class FakeEventFilter:
    """``EventFilter`` driven by tables keyed by event uid.

    ``exclude`` maps uids to the reason they are dropped; ``uncertain`` uids
    are kept and flagged. Two real rules are applied as well: with
    ``include_defenses=False`` (the ``settings.events.include_defenses``
    switch) events of type ``defense`` are excluded with reason ``defense``,
    and ``drop_out_of_scope`` excludes an event without fields as
    ``out_of_scope``.
    """

    def __init__(
        self,
        exclude: Mapping[str, ExclusionReason] | None = None,
        uncertain: Collection[str] = (),
        *,
        include_defenses: bool = True,
    ) -> None:
        self.exclude: dict[str, ExclusionReason] = dict(exclude or {})
        self.uncertain = set(uncertain)
        self.include_defenses = include_defenses

    def _reason(self, candidate: Candidate) -> ExclusionReason | None:
        reason = self.exclude.get(candidate.event.uid)
        if reason is None and not self.include_defenses:
            return "defense" if candidate.event.event_type == "defense" else None
        return reason

    def filter(self, candidates: Sequence[Candidate]) -> FilterResult:
        kept: list[Candidate] = []
        excluded: list[ExcludedEvent] = []
        for candidate in candidates:
            reason = self._reason(candidate)
            if reason is None:
                kept.append(candidate)
            else:
                excluded.append(
                    ExcludedEvent.from_event(candidate.event, reason, detail="fake filter")
                )
        uncertain = [c.event.uid for c in kept if c.event.uid in self.uncertain]
        return FilterResult(kept=kept, excluded=excluded, uncertain=uncertain)

    def drop_out_of_scope(self, events: Sequence[Event]) -> ScopeResult:
        return ScopeResult(
            kept=[event for event in events if event.fields],
            excluded=[
                ExcludedEvent.from_event(event, "out_of_scope")
                for event in events
                if not event.fields
            ],
        )


class FakePicksScorer:
    """``PicksScorer`` with a fixed score per event uid (0.0 for unknown uids)."""

    def __init__(self, scores: Mapping[str, float] | None = None, *, threshold: float = 1.0):
        self.scores = dict(scores or {})
        self.threshold = threshold

    def score(self, event: Event) -> PicksResult:
        score = self.scores.get(event.uid, 0.0)
        return PicksResult(score=score, reasons=[f"fake score {score:g}"] if score else [])


class FakeIssueClient:
    """In-memory ``IssueClient``.

    ``issues`` holds every issue in creation order, ``calls`` every protocol
    call as ``(method name, arguments)``, ``comments`` the closing comments.
    Use ``seed`` to put an already existing issue in place.
    """

    def __init__(self, clock: FakeClock | None = None) -> None:
        self._clock = clock or FakeClock()
        self.issues: list[Issue] = []
        self.labels: dict[str, IssueLabel] = {}
        self.comments: dict[int, list[str]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def seed(
        self,
        title: str,
        *,
        body: str = "",
        labels: Sequence[str] = (),
        state: IssueState = "open",
        created_at: datetime | None = None,
    ) -> Issue:
        """Add an issue as if it had been created earlier (not recorded in ``calls``)."""
        number = len(self.issues) + 1
        issue = Issue(
            number=number,
            title=title,
            body=body,
            state=state,
            labels=list(labels),
            created_at=created_at or self._clock.now(),
            url=f"https://github.com/example/seminars/issues/{number}",
        )
        self.issues.append(issue)
        return issue

    def get(self, number: int) -> Issue:
        """The current state of issue ``number``."""
        return self.issues[number - 1]

    def _matching(self, state: IssueStateFilter) -> list[Issue]:
        chosen = [issue for issue in self.issues if state in ("all", issue.state)]
        return sorted(chosen, key=lambda issue: (issue.created_at, issue.number), reverse=True)

    def ensure_label(self, label: IssueLabel) -> None:
        self.calls.append(("ensure_label", {"label": label}))
        self.labels.setdefault(label.name, label)

    def find_by_label(self, label: str, *, state: IssueStateFilter = "open") -> list[Issue]:
        self.calls.append(("find_by_label", {"label": label, "state": state}))
        return [issue for issue in self._matching(state) if label in issue.labels]

    def find_by_title(self, title: str, *, state: IssueStateFilter = "all") -> Issue | None:
        self.calls.append(("find_by_title", {"title": title, "state": state}))
        return next((issue for issue in self._matching(state) if issue.title == title), None)

    def open(self, title: str, body: str, *, labels: Sequence[str] = ()) -> Issue:
        self.calls.append(("open", {"title": title, "body": body, "labels": list(labels)}))
        return self.seed(title, body=body, labels=labels)

    def update(self, number: int, *, title: str | None = None, body: str | None = None) -> Issue:
        self.calls.append(("update", {"number": number, "title": title, "body": body}))
        current = self.get(number)
        changed = current.evolve(
            title=current.title if title is None else title,
            body=current.body if body is None else body,
        )
        self.issues[number - 1] = changed
        return changed

    def close(self, number: int, *, comment: str | None = None) -> None:
        self.calls.append(("close", {"number": number, "comment": comment}))
        self.issues[number - 1] = self.get(number).evolve(state="closed")
        if comment is not None:
            self.comments.setdefault(number, []).append(comment)


class FakeSecretScanner:
    """``SecretScanner`` that reports every line containing one of ``needles``."""

    def __init__(self, needles: Sequence[str] = ()) -> None:
        self.needles = list(needles)
        self.scanned: list[str] = []

    def scan_text(self, text: str, *, origin: str) -> list[SecretFinding]:
        self.scanned.append(origin)
        return [
            SecretFinding(origin=origin, line=number, rule="fake-needle")
            for number, line in enumerate(text.splitlines(), start=1)
            if any(needle in line for needle in self.needles)
        ]

    def scan_paths(self, paths: Iterable[Path]) -> list[SecretFinding]:
        findings: list[SecretFinding] = []
        for path in paths:
            files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
            for file in files:
                text = file.read_text(encoding="utf-8", errors="replace")
                findings.extend(self.scan_text(text, origin=str(file)))
        return findings
