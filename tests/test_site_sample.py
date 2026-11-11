"""The committed sample payloads for the web site match the models and the generator."""

from __future__ import annotations

import json

import pytest

from seminars.models import (
    FEED_IDS,
    FIELD_IDS,
    PublicEventsPayload,
    PublicStatusPayload,
    dump_json,
    load_json,
)
from site_sample import (
    GENERATED_AT,
    SAMPLE_DIR,
    XSS_ABSTRACT,
    XSS_TITLE,
    sample_events_payload,
    sample_files,
    sample_status_payload,
)

INTERNAL_KEYS = {
    "missing_runs",
    "sequence",
    "source_ids",
    "source_urls",
    "first_seen",
    "last_changed",
    "field_tag_method",
    "extraction",
    "picks_score",
}


@pytest.fixture(scope="module")
def events_payload() -> PublicEventsPayload:
    text = (SAMPLE_DIR / "events.json").read_text(encoding="utf-8")
    return load_json(text, PublicEventsPayload)


@pytest.fixture(scope="module")
def status_payload() -> PublicStatusPayload:
    text = (SAMPLE_DIR / "status.json").read_text(encoding="utf-8")
    return load_json(text, PublicStatusPayload)


def test_committed_files_match_the_generator() -> None:
    for path, expected in sample_files().items():
        assert path.read_text(encoding="utf-8") == expected, (
            f"{path.name} is stale; run: uv run python tests/site_sample.py"
        )


def test_files_round_trip_through_the_models(
    events_payload: PublicEventsPayload, status_payload: PublicStatusPayload
) -> None:
    assert events_payload == sample_events_payload()
    assert status_payload == sample_status_payload()
    assert dump_json(events_payload) == (SAMPLE_DIR / "events.json").read_text(encoding="utf-8")
    assert dump_json(status_payload) == (SAMPLE_DIR / "status.json").read_text(encoding="utf-8")


def test_events_payload_header(events_payload: PublicEventsPayload) -> None:
    assert events_payload.schema_version == 1
    assert events_payload.generated_at == GENERATED_AT
    assert events_payload.timezone == "America/Los_Angeles"
    assert events_payload.fields == list(FIELD_IDS)
    assert [(feed.id, feed.path) for feed in events_payload.feeds] == [
        (feed, f"feeds/{feed}.ics") for feed in FEED_IDS
    ]


def test_events_are_sorted_by_start_then_uid(events_payload: PublicEventsPayload) -> None:
    keys = [(event.start, event.uid) for event in events_payload.events]
    assert keys == sorted(keys)
    assert len({event.uid for event in events_payload.events}) == len(keys)


def test_sample_covers_the_required_cases(events_payload: PublicEventsPayload) -> None:
    by_uid = {event.uid: event for event in events_payload.events}

    injected = by_uid["sample-xss"]
    assert injected.title == XSS_TITLE
    assert injected.abstract == XSS_ABSTRACT
    assert "<script>" in injected.title
    assert "onerror=" in injected.abstract
    assert "<i>" in injected.speakers[0].name

    all_day = by_uid["sample-all-day"]
    assert all_day.all_day
    assert all_day.end is not None
    assert (all_day.end - all_day.start).days == 1

    assert by_uid["sample-cancelled"].status == "cancelled"
    assert by_uid["sample-tentative-physics"].status == "tentative"

    online = by_uid["sample-online"]
    assert online.is_online
    assert online.location is None
    assert str(online.stream_url) == "https://stanford.zoom.us/j/1234567890"
    hybrid = by_uid["sample-hybrid-med"]
    assert hybrid.is_online
    assert hybrid.location is not None
    assert hybrid.stream_url is None

    assert by_uid["sample-tba"].title == "TBA"
    assert by_uid["sample-tba"].end is None
    assert not by_uid["sample-tba"].speakers[0].name.isascii()
    assert len(by_uid["sample-hybrid-med"].speakers) == 2
    assert by_uid["sample-defense"].speakers[0].affiliation is None


def test_sample_spans_the_vocabularies(events_payload: PublicEventsPayload) -> None:
    events = events_payload.events
    assert {field for event in events for field in event.fields} == set(FIELD_IDS)
    assert {event.institution for event in events} == {"stanford", "slac", "arc", "biohub", "other"}
    assert {"seminar", "colloquium", "lecture", "conference", "workshop", "defense"} <= {
        event.event_type for event in events
    }
    picks = [event for event in events if event.is_pick]
    assert 2 <= len(picks) < len(events)
    assert all(event.picks_reasons for event in picks)
    assert any(event.start < events_payload.generated_at for event in events)
    assert any(event.start.year == 2027 for event in events)


def test_no_internal_state_is_published() -> None:
    document = json.loads((SAMPLE_DIR / "events.json").read_text(encoding="utf-8"))
    for event in document["events"]:
        assert not INTERNAL_KEYS & set(event)
        assert set(event) == set(document["events"][0])


def test_status_sample_covers_every_state(status_payload: PublicStatusPayload) -> None:
    assert status_payload.schema_version == 1
    states = {source.state for source in status_payload.sources}
    assert states == {"ok", "empty", "failed", "disabled", "unknown"}
    ids = [source.id for source in status_payload.sources]
    assert ids == sorted(ids)
    failed = next(source for source in status_payload.sources if source.state == "failed")
    assert failed.error is not None
    assert "<img" in failed.error
    assert failed.consecutive_failures == 3
    assert status_payload.event_count == len(sample_events_payload().events)
