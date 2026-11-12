"""The two payloads the static site reads: events.json and status.json.

``write_site_data`` projects the stored events onto the public schema
(``seminars.models.PublicEventsPayload``) and condenses the run history into
one row per configured source (``PublicStatusPayload``). Both files are
written with ``seminars.models.dump_json`` and depend on nothing but the
arguments: the "last updated" time is handed in, events are ordered by start
and uid and sources by id, so the same input gives the same bytes.

Every event that is given is published, past and cancelled ones included;
the site decides what to show. Internal state (source ids, counters, the
picks score) is not part of the public schema and never leaves data/. The
picks reasons are published taken apart
(``seminars.models.PickReason``), so that the site can say them in
the reader's language.

The site treats all text as untrusted and escapes it, so nothing is escaped
here. Text is only passed through ``seminars.textrules.clean_text`` once
more: events are clean already and come out unchanged, and should anything
else ever reach this writer, the published files still hold no emoji and no
decorative or invisible character.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from seminars.config import AppConfig
from seminars.models import (
    TBA_TITLE,
    Event,
    HealthFile,
    LastRun,
    PublicEvent,
    PublicEventsPayload,
    PublicSourceState,
    PublicSourceStatus,
    PublicStatusPayload,
    SourceConfig,
    SourceHealth,
    SourceHealthEntry,
    Speaker,
    write_model,
)
from seminars.textrules import clean_text


def _text(value: str | None) -> str | None:
    """``value`` without what the charset rule rejects; ``None`` when nothing is left."""
    cleaned = clean_text(value or "")
    return cleaned if cleaned.strip() else None


def _speakers(speakers: Sequence[Speaker]) -> list[Speaker]:
    return [
        Speaker(name=name, affiliation=_text(speaker.affiliation), url=speaker.url)
        for speaker in speakers
        if (name := _text(speaker.name)) is not None
    ]


def _public_event(event: Event, picks_threshold: float) -> PublicEvent:
    """The public record of one event; required text that cleans to nothing gets a stand-in."""
    public = PublicEvent.from_event(event, picks_threshold=picks_threshold)
    return public.evolve(
        title=_text(public.title) or TBA_TITLE,
        speakers=_speakers(public.speakers),
        series=_text(public.series),
        host_org=_text(public.host_org) or public.institution,
        location=_text(public.location),
        abstract=_text(public.abstract),
        picks_reasons=[
            reason.evolve(term=term)
            for reason in public.picks_reasons
            if (term := _text(reason.term)) is not None
        ],
    )


def _ordered(events: Sequence[Event]) -> list[Event]:
    """Events by start and uid; a uid that appears twice is an error."""
    repeated = sorted(
        uid for uid, count in Counter(event.uid for event in events).items() if count > 1
    )
    if repeated:
        raise ValueError(f"duplicate event uid: {repeated[0]!r}")
    return sorted(events, key=lambda event: (event.start.astimezone(UTC), event.uid))


def _completeness(last: SourceHealthEntry) -> dict[str, Any]:
    """What the status row says about how much of its listing the last run read."""
    judged = last.shrank_from is not None or last.resized_from is not None
    return {
        "read_in_full": last.read_in_full,
        "skipped_pages": last.skipped_pages,
        "listed": last.accepted if judged else None,
        "usual_size": last.shrank_from,
        "resized_from": last.resized_from,
    }


def _source_status(source: SourceConfig, health: HealthFile) -> PublicSourceStatus:
    """One row of the status page: what the last run of a source did.

    A disabled source keeps the times of its history but shows no result,
    and a source that never ran is ``unknown``. A run that is known to have
    missed events says so (``PublicSourceStatus.read_in_full``).
    """
    history = health.sources.get(source.id, SourceHealth())
    last = history.last
    state: PublicSourceState = "unknown"
    if not source.enabled:
        state = "disabled"
    elif last is not None:
        state = last.status
    counted = last if source.enabled else None
    return PublicSourceStatus(
        id=source.id,
        name=_text(source.name) or source.id,
        group=source.group,
        url=source.homepage or source.all_urls[0],
        state=state,
        events=counted.events if counted is not None else 0,
        last_run_at=last.at if last is not None else None,
        last_success_at=history.last_success_at,
        consecutive_failures=history.trailing("failed") if source.enabled else 0,
        error=_text(last.error) if last is not None and state == "failed" else None,
        **(_completeness(counted) if counted is not None else {}),
    )


def write_site_data(
    events: Sequence[Event],
    health: HealthFile,
    last_run: LastRun | None,
    out_dir: Path,
    *,
    config: AppConfig,
    generated_at: datetime,
) -> list[Path]:
    """Write ``<out_dir>/events.json`` and ``<out_dir>/status.json``; return the two paths.

    The contract is ``seminars.contracts.SiteDataWriter``. ``generated_at``
    is the timezone-aware "last updated" time of both files. Nothing is
    written when two events share a uid.
    """
    threshold = config.interests.threshold
    events_payload = PublicEventsPayload(
        generated_at=generated_at,
        timezone=config.settings.timezone,
        events=[_public_event(event, threshold) for event in _ordered(events)],
    )
    status_payload = PublicStatusPayload(
        generated_at=generated_at,
        last_run_at=last_run.finished_at if last_run is not None else None,
        last_run_ok=last_run.ok if last_run is not None else None,
        event_count=len(events_payload.events),
        llm_provider=last_run.llm_provider if last_run is not None else None,
        sources=[
            _source_status(source, health)
            for source in sorted(config.sources, key=lambda source: source.id)
        ],
    )
    paths = [
        out_dir / config.paths.public_events_file.name,
        out_dir / config.paths.public_status_file.name,
    ]
    write_model(paths[0], events_payload)
    write_model(paths[1], status_payload)
    return paths
