"""Fakes for the output contracts (ICS, site data, digest, health) and the source probe.

``FakeEmitters`` bundles one stand-in per output function. Each records its
call in ``calls`` and does the simplest thing that honours the contract, so a
pipeline or command line test gets real files and real model objects without
the real implementations.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from seminars.config import AppConfig
from seminars.contracts import LABEL_DIGEST, IssueClient
from seminars.http import HttpClient
from seminars.models import (
    DEADLINES_FEED,
    FEED_IDS,
    Deadline,
    DigestDocument,
    DigestOutcome,
    Event,
    HealthFile,
    LastRun,
    LLMHealthEntry,
    ProbeResult,
    PublicEvent,
    PublicEventsPayload,
    PublicSourceStatus,
    PublicStatusPayload,
    RunReport,
    SourceHealthEntry,
    write_model,
)
from seminars.textio import write_text_atomic


class FakeEmitters:
    """One recording stand-in per output contract; pass the bound methods around."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def write_ics_feeds(
        self,
        events: Sequence[Event],
        out_dir: Path,
        *,
        config: AppConfig,
        deadlines: Sequence[Deadline] = (),
    ) -> list[Path]:
        """Write ten placeholder calendars that state how many events each feed has.

        The feed of the deadlines comes last and states how many deadlines it has.
        """
        self.calls.append(
            (
                "write_ics_feeds",
                {"events": len(events), "out_dir": out_dir, "deadlines": len(deadlines)},
            )
        )
        threshold = config.interests.threshold
        paths = []
        for feed in FEED_IDS:
            if feed == "all":
                count = len(events)
            elif feed == "picks":
                count = sum(event.is_pick(threshold) for event in events)
            else:
                count = sum(feed in event.fields for event in events)
            path = out_dir / f"{feed}.ics"
            write_text_atomic(
                path, f"BEGIN:VCALENDAR\nX-FAKE-FEED:{feed}\nX-FAKE-EVENTS:{count}\nEND:VCALENDAR\n"
            )
            paths.append(path)
        path = out_dir / f"{DEADLINES_FEED}.ics"
        write_text_atomic(
            path,
            f"BEGIN:VCALENDAR\nX-FAKE-FEED:{DEADLINES_FEED}\n"
            f"X-FAKE-DEADLINES:{len(deadlines)}\nEND:VCALENDAR\n",
        )
        paths.append(path)
        return paths

    def validate_ics(self, text: str, *, deadlines: bool = False) -> list[str]:
        self.calls.append(("validate_ics", {"length": len(text), "deadlines": deadlines}))
        return [] if text.startswith("BEGIN:VCALENDAR") else ["does not start with BEGIN:VCALENDAR"]

    def write_site_data(
        self,
        events: Sequence[Event],
        health: HealthFile,
        last_run: LastRun | None,
        out_dir: Path,
        *,
        config: AppConfig,
        generated_at: datetime,
    ) -> list[Path]:
        """Write schema-valid events.json and status.json."""
        self.calls.append(("write_site_data", {"events": len(events), "out_dir": out_dir}))
        ordered = sorted(events, key=lambda event: (event.start, event.uid))
        threshold = config.interests.threshold
        events_payload = PublicEventsPayload(
            generated_at=generated_at,
            timezone=config.settings.timezone,
            events=[PublicEvent.from_event(event, picks_threshold=threshold) for event in ordered],
        )
        rows = []
        for source in config.sources:
            last = health.sources[source.id].last if source.id in health.sources else None
            rows.append(
                PublicSourceStatus(
                    id=source.id,
                    name=source.name,
                    group=source.group,
                    url=source.homepage or source.all_urls[0],
                    state="disabled"
                    if not source.enabled
                    else (last.status if last else "unknown"),
                    events=last.events if last else 0,
                    last_run_at=last.at if last else None,
                    error=last.error if last else None,
                )
            )
        status_payload = PublicStatusPayload(
            generated_at=generated_at,
            last_run_at=last_run.finished_at if last_run else None,
            last_run_ok=last_run.ok if last_run else None,
            event_count=len(ordered),
            llm_provider=last_run.llm_provider if last_run else None,
            sources=rows,
        )
        paths = [out_dir / "events.json", out_dir / "status.json"]
        write_model(paths[0], events_payload)
        write_model(paths[1], status_payload)
        return paths

    def render_digest(
        self,
        events: Sequence[Event],
        health: HealthFile,
        *,
        config: AppConfig,
        week_start: date,
        previous_issue_at: datetime | None,
        owner: str | None = None,
    ) -> DigestDocument:
        self.calls.append(
            ("render_digest", {"events": len(events), "previous_issue_at": previous_issue_at})
        )
        new = [
            event
            for event in events
            if previous_issue_at is not None and event.first_seen > previous_issue_at
        ]
        body = f"events: {len(events)}\nnew: {len(new) if previous_issue_at else 'first issue'}\n"
        if owner:
            body += f"cc @{owner}\n"
        return DigestDocument(
            week_start=week_start, title=f"Seminars - week of {week_start.isoformat()}", body=body
        )

    def publish_digest(
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
        """Idempotent per week, closes the previous digest, like the real publisher."""
        self.calls.append(("publish_digest", {"dry_run": dry_run, "owner": owner}))
        today = now.astimezone(ZoneInfo(config.settings.timezone)).date()
        week_start = today - timedelta(days=today.weekday())
        previous = issues.find_by_label(LABEL_DIGEST.name, state="all") if issues else []
        document = self.render_digest(
            events,
            health,
            config=config,
            week_start=week_start,
            previous_issue_at=previous[0].created_at if previous else None,
            owner=owner,
        )
        if dry_run or issues is None:
            return DigestOutcome(action="dry_run", document=document)
        existing = issues.find_by_title(document.title)
        if existing is not None:
            return DigestOutcome(action="exists", document=document, issue_number=existing.number)
        issues.ensure_label(LABEL_DIGEST)
        created = issues.open(document.title, document.body, labels=[LABEL_DIGEST.name])
        closed = [issue.number for issue in previous if issue.state == "open"]
        for number in closed:
            issues.close(number)
        return DigestOutcome(
            action="created", document=document, issue_number=created.number, closed_issues=closed
        )

    def update_health(
        self,
        health: HealthFile,
        report: RunReport,
        issues: IssueClient | None,
        *,
        config: AppConfig,
    ) -> HealthFile:
        """Append the run to the history; opens no issues."""
        self.calls.append(("update_health", {"sources": len(report.sources)}))
        for result in report.sources:
            entry = SourceHealthEntry(
                at=report.started_at, status=result.status, events=result.events, error=result.error
            )
            health = health.with_source_entry(result.source_id, entry)
        for provider in report.llm.providers:
            llm_entry = LLMHealthEntry(
                at=report.started_at,
                status=provider.status,
                calls=provider.calls,
                failure_kind=provider.failure_kind,
                error=provider.error,
            )
            health = health.with_llm_entry(provider.provider, llm_entry)
        return health

    def probe_source(self, http: HttpClient, url: str) -> ProbeResult:
        self.calls.append(("probe_source", {"url": url}))
        document = http.get(url)
        return ProbeResult(
            url=url,
            format="unknown",
            content_type=document.content_type,
            suggested_yaml=f"sources:\n  - url: {url}\n",
        )
