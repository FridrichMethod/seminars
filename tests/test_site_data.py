"""Tests for the public payloads of the site: events.json and status.json."""

from __future__ import annotations

import dataclasses
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings

from factories import PACIFIC, REFERENCE_NOW, event_lists, make_event, make_source
from seminars.config import AppConfig, Interests
from seminars.contracts import SiteDataWriter, load_implementation
from seminars.models import (
    Event,
    HealthFile,
    LastRun,
    PublicEvent,
    PublicEventsPayload,
    PublicSourceStatus,
    PublicStatusPayload,
    SourceConfig,
    SourceHealthEntry,
    dump_json,
    read_model,
)
from seminars.models.public import PickReason
from seminars.outputs.site_data import write_site_data
from seminars.textio import read_text
from seminars.textrules import decorative_characters
from site_sample import GENERATED_AT, SAMPLE_DIR, sample_events

ROCKET = "\U0001f680"
DIRTY = f"Launch{ROCKET}\u00a0day\u200b \ufb01ne \u2728"  # emoji, odd spaces, a ligature
NAME = "Zo\u00e9 M\u00fcller"
RAN_AT = datetime(2026, 11, 10, 12, 23, tzinfo=UTC)
LAST_RUN = LastRun(
    started_at=RAN_AT,
    finished_at=RAN_AT + timedelta(minutes=6),
    ok=True,
    mode="live",
    llm_provider="claude-cli",
)


def write(
    config: AppConfig,
    out_dir: Path,
    events: list[Event] | None = None,
    health: HealthFile | None = None,
    last_run: LastRun | None = None,
    generated_at: datetime = GENERATED_AT,
) -> tuple[PublicEventsPayload, PublicStatusPayload]:
    """Write both files and read them back through the public models."""
    paths = write_site_data(
        events or [],
        health or HealthFile(),
        last_run,
        out_dir,
        config=config,
        generated_at=generated_at,
    )
    events_payload = read_model(paths[0], PublicEventsPayload)
    status_payload = read_model(paths[1], PublicStatusPayload)
    assert events_payload is not None
    assert status_payload is not None
    return events_payload, status_payload


def with_sources(config: AppConfig, *sources: SourceConfig) -> AppConfig:
    return dataclasses.replace(config, sources=tuple(sources))


def ran(health: HealthFile, source_id: str, *runs: tuple[int, str, int, str | None]) -> HealthFile:
    """``health`` after the given runs of a source: (days ago, status, events, error)."""
    for days_ago, status, events, error in runs:
        entry = SourceHealthEntry.model_validate(
            {
                "at": RAN_AT - timedelta(days=days_ago),
                "status": status,
                "events": events,
                "error": error,
            }
        )
        health = health.with_source_entry(source_id, entry)
    return health


class TestFiles:
    def test_the_contract_points_at_the_implementation(self) -> None:
        writer: SiteDataWriter = load_implementation("site_data")
        assert writer is write_site_data

    def test_both_files_are_written_with_the_shared_serializer(self, app_config: AppConfig) -> None:
        out_dir = app_config.paths.public_dir
        paths = write_site_data(
            [make_event()],
            HealthFile(),
            None,
            out_dir,
            config=app_config,
            generated_at=GENERATED_AT,
        )
        assert paths == [app_config.paths.public_events_file, app_config.paths.public_status_file]
        events_payload, status_payload = write(app_config, out_dir, [make_event()])
        assert read_text(paths[0]) == dump_json(events_payload)
        assert read_text(paths[1]) == dump_json(status_payload)

    def test_the_sample_payload_of_the_site_is_reproduced_byte_for_byte(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        """tests/fixtures/_site/events.json is what the site was built against."""
        shuffled = sample_events()
        random.Random(3).shuffle(shuffled)
        paths = write_site_data(
            shuffled, HealthFile(), None, tmp_path, config=app_config, generated_at=GENERATED_AT
        )
        assert read_text(paths[0]) == read_text(SAMPLE_DIR / "events.json")

    def test_the_same_input_gives_the_same_bytes(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        config = with_sources(app_config, make_source())
        health = ran(HealthFile(), "sites-bioengineering", (1, "failed", 0, "HTTP 503"))
        events = sample_events()
        before = [event.model_dump_json() for event in events]
        texts = []
        for name in ("a", "b"):
            paths = write_site_data(
                events, health, LAST_RUN, tmp_path / name, config=config, generated_at=GENERATED_AT
            )
            texts.append([read_text(path) for path in paths])
        assert texts[0] == texts[1]
        assert [event.model_dump_json() for event in events] == before

    def test_a_uid_that_appears_twice_is_an_error(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="duplicate event uid: 'same'"):
            write(app_config, tmp_path / "public", [make_event(uid="same")] * 2)
        assert not (tmp_path / "public").exists()


class TestEventsPayload:
    def test_header(self, app_config: AppConfig, tmp_path: Path) -> None:
        local = datetime(2026, 11, 10, 4, 30, 15, 999, tzinfo=PACIFIC)
        payload, _ = write(app_config, tmp_path, generated_at=local)
        assert payload.generated_at == datetime(2026, 11, 10, 12, 30, 15, tzinfo=UTC)
        assert payload.timezone == "America/Los_Angeles"
        assert payload.events == []
        berlin = dataclasses.replace(
            app_config, settings=app_config.settings.evolve(timezone="Europe/Berlin")
        )
        assert write(berlin, tmp_path)[0].timezone == "Europe/Berlin"

    def test_events_are_sorted_by_start_then_uid(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        early = datetime(2026, 11, 17, 23, 0, tzinfo=UTC)  # one hour before the default start
        events = [make_event(uid="c"), make_event(uid="a"), make_event(uid="b", start=early)]
        payload, status = write(app_config, tmp_path, events)
        assert [event.uid for event in payload.events] == ["b", "a", "c"]
        assert status.event_count == 3

    def test_every_event_is_published_whatever_its_state(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        events = [
            make_event(uid="past", start=REFERENCE_NOW - timedelta(days=9), end=None),
            make_event(uid="cancelled", status="cancelled"),
            make_event(uid="missing", missing_runs=2),
        ]
        payload, _ = write(app_config, tmp_path, events)
        assert {event.uid for event in payload.events} == {"past", "cancelled", "missing"}

    def test_an_event_is_projected_onto_the_public_schema(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        event = make_event(
            speakers=[{"name": NAME, "affiliation": "ETH", "url": "https://example.org/zm"}],
            is_online=True,
            stream_url="https://stanford.zoom.us/j/1234567890",
            picks_score=3.0,
            picks_reasons=["title: protein design"],
            fields=["bio", "ai-ml"],
            missing_runs=1,
            sequence=5,
        )
        payload, _ = write(app_config, tmp_path, [event])
        assert payload.events == [PublicEvent.from_event(event, picks_threshold=3.0)]
        assert payload.events[0].is_pick
        (raw,) = json.loads(read_text(tmp_path / "events.json"))["events"]
        assert set(raw) == set(PublicEvent.model_fields)
        internal = {"missing_runs", "sequence", "source_ids", "source_urls", "picks_score"}
        assert not internal & set(raw)
        assert raw["start"] == "2026-11-17T16:00:00-08:00"
        assert raw["speakers"][0]["name"] == NAME  # readable, not an escape

    def test_the_picks_threshold_comes_from_the_interests(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        events = [make_event(uid="a", picks_score=2.0), make_event(uid="b", picks_score=5.0)]
        payload, _ = write(app_config, tmp_path, events)
        assert [event.is_pick for event in payload.events] == [False, True]
        eager = dataclasses.replace(app_config, interests=Interests(threshold=1.5))
        assert [event.is_pick for event in write(eager, tmp_path, events)[0].events] == [True, True]


class TestStatusPayload:
    def test_without_a_run_or_sources(self, app_config: AppConfig, tmp_path: Path) -> None:
        _, status = write(app_config, tmp_path)
        assert status == PublicStatusPayload(generated_at=GENERATED_AT)
        assert (status.last_run_at, status.last_run_ok, status.llm_provider) == (None, None, None)

    def test_the_last_run_is_summarized(self, app_config: AppConfig, tmp_path: Path) -> None:
        _, status = write(app_config, tmp_path, [make_event()], last_run=LAST_RUN)
        assert status.last_run_at == LAST_RUN.finished_at
        assert status.last_run_ok is True
        assert status.llm_provider == "claude-cli"
        assert status.event_count == 1
        failed = LAST_RUN.evolve(ok=False, llm_provider=None)
        _, status = write(app_config, tmp_path, last_run=failed)
        assert (status.last_run_ok, status.llm_provider) == (False, None)

    def test_one_row_per_configured_source(self, app_config: AppConfig, tmp_path: Path) -> None:
        config = with_sources(
            app_config,
            make_source(id="sites-ok", homepage="https://chemistry.stanford.edu/events"),
            make_source(id="sites-broken", name="Broken <b>source</b>"),
            make_source(id="sites-empty"),
            make_source(id="sites-new", urls=["https://example.edu/a", "https://example.edu/b"]),
            make_source(id="sites-off", enabled=False),
        )
        health = ran(HealthFile(), "sites-ok", (1, "failed", 0, "HTTP 500"), (0, "ok", 12, None))
        health = ran(
            health,
            "sites-broken",
            *((7, "ok", 4, None), (2, "failed", 0, "HTTP 503")),
            *((1, "empty", 0, None), (0, "failed", 0, "HTTP 503: <img src=x>")),
        )
        health = ran(health, "sites-empty", (1, "ok", 3, None), (0, "empty", 0, None))
        health = ran(health, "sites-off", (9, "ok", 5, None), (8, "failed", 0, "gone"))
        health = ran(health, "sites-removed", (0, "ok", 1, None))
        _, status = write(config, tmp_path, health=health)
        feed = "https://bioengineering.stanford.edu/events/all/rss"
        base: dict[str, Any] = {"name": "Stanford Bioengineering", "group": "stanford-sites"}
        base["url"] = feed
        assert status.sources == [
            PublicSourceStatus(
                **{**base, "name": "Broken <b>source</b>"},
                id="sites-broken",
                state="failed",
                last_run_at=RAN_AT,
                last_success_at=RAN_AT - timedelta(days=7),
                consecutive_failures=1,
                error="HTTP 503: <img src=x>",
            ),
            PublicSourceStatus(
                **base,
                id="sites-empty",
                state="empty",
                last_run_at=RAN_AT,
                last_success_at=RAN_AT - timedelta(days=1),
            ),
            PublicSourceStatus(**base, id="sites-new", state="unknown"),
            PublicSourceStatus(
                **base,
                id="sites-off",
                state="disabled",
                last_run_at=RAN_AT - timedelta(days=8),
                last_success_at=RAN_AT - timedelta(days=9),
            ),
            PublicSourceStatus(
                **{**base, "url": "https://chemistry.stanford.edu/events"},
                id="sites-ok",
                state="ok",
                events=12,
                last_run_at=RAN_AT,
                last_success_at=RAN_AT,
            ),
        ]

    def test_failures_in_a_row_are_counted(self, app_config: AppConfig, tmp_path: Path) -> None:
        config = with_sources(app_config, make_source())
        runs = [(3, "ok", 2, None), *((days, "failed", 0, f"HTTP 50{days}") for days in (2, 1, 0))]
        _, status = write(config, tmp_path, health=ran(HealthFile(), "sites-bioengineering", *runs))
        (row,) = status.sources
        assert (row.state, row.consecutive_failures, row.error) == ("failed", 3, "HTTP 500")
        assert row.last_success_at == RAN_AT - timedelta(days=3)


class TestCharset:
    def test_dirty_text_is_cleaned_before_it_is_published(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        event = make_event(
            title=DIRTY,
            speakers=[{"name": DIRTY, "affiliation": DIRTY}, {"name": ROCKET, "affiliation": "x"}],
            series=DIRTY,
            host_org=DIRTY,
            location=DIRTY,
            abstract=DIRTY,
            picks_reasons=[DIRTY, ROCKET],
        )
        config = with_sources(app_config, make_source(name=DIRTY))
        health = ran(HealthFile(), "sites-bioengineering", (0, "failed", 0, DIRTY))
        payload, status = write(config, tmp_path, [event], health)
        for name in ("events.json", "status.json"):
            assert decorative_characters(read_text(tmp_path / name)) == []
        (public,) = payload.events
        clean = "Launch day fine "
        assert (public.title, public.series, public.host_org) == (clean, clean, clean)
        assert (public.location, public.abstract) == (clean, clean)
        assert [(speaker.name, speaker.affiliation) for speaker in public.speakers] == [
            (clean, clean)
        ]
        assert public.picks_reasons == [PickReason(kind="other", term=clean)]
        assert (status.sources[0].name, status.sources[0].error) == (clean, clean)

    def test_text_that_is_nothing_but_decoration_gets_a_stand_in(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        event = make_event(
            title=ROCKET, series=ROCKET, host_org=ROCKET, location=" ", abstract=ROCKET
        )
        config = with_sources(app_config, make_source(name=ROCKET))
        payload, status = write(config, tmp_path, [event])
        (public,) = payload.events
        assert (public.title, public.host_org) == ("TBA", "stanford")
        assert (public.series, public.location, public.abstract) == (None, None, None)
        assert status.sources[0].name == "sites-bioengineering"

    def test_clean_text_is_published_as_it_is(self, app_config: AppConfig, tmp_path: Path) -> None:
        text = f"  {NAME} \u2192 \u211d\u00b2,  \u86cb\u767d\u8d28\n<b>bold</b> &amp; "
        event = make_event(title=text, location=text, abstract=text)
        (public,) = write(app_config, tmp_path, [event])[0].events
        assert (public.title, public.location, public.abstract) == (text, text, text)

    @given(events=event_lists())
    @settings(
        max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    def test_any_events_give_files_that_fit_the_schema(
        self, events: list[Event], app_config: AppConfig, tmp_path: Path
    ) -> None:
        payload, status = write(app_config, tmp_path, events)
        assert sorted(event.uid for event in payload.events) == sorted(e.uid for e in events)
        keys = [(event.start, event.uid) for event in payload.events]
        assert keys == sorted(keys)
        assert status.event_count == len(events)
        assert decorative_characters(read_text(tmp_path / "events.json")) == []
        again, _ = write(app_config, tmp_path / "again", list(reversed(events)))
        assert again == payload
