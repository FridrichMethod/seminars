"""Tests for one whole run of the pipeline, with every other package replaced by a fake."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from factories import PACIFIC, REFERENCE_NOW, make_draft, make_source
from fakes import (
    FakeAdapter,
    FakeClock,
    FakeEventFilter,
    FakeFieldTagger,
    FakeHttpClient,
    FakeIssueClient,
    FakePicksScorer,
    FakeSecretScanner,
)
from fakes_llm import FakeLLMCache, FakeLLMClient
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig, ProjectPaths
from seminars.http import HttpStatusError
from seminars.models import (
    Event,
    EventDraft,
    LastRun,
    RunReport,
    SourceConfig,
    UidMap,
    read_model,
)
from seminars.outputs.health import update_health
from seminars.pipeline.checks import CHECK_ISSUE_TITLE
from seminars.pipeline.run import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_OUTPUT_CHECK,
    RunError,
    RunOptions,
    SourceSelectionError,
    already_done_today,
    exit_status,
    listing_urls,
    output_target,
    run_pipeline,
    select_sources,
)
from seminars.pipeline.secret_scan import build_secret_scanner
from seminars.pipeline.services import Services
from seminars.pipeline.store import read_events_file
from seminars.sources.base import AdapterRegistry

A_URL = "https://a.example.edu/events"
B_URL = "https://b.example.edu/events"


def source(source_id: str, url: str, **overrides: Any) -> SourceConfig:
    values: dict[str, Any] = {"id": source_id, "adapter": "fake", "url": url}
    return make_source(**{**values, **overrides})


SOURCES = (source("sites-a", A_URL), source("sites-b", B_URL))
MORNING = datetime(2026, 11, 17, 8, 0, tzinfo=PACIFIC)


def draft(source_id: str, native_id: str, **overrides: Any) -> EventDraft:
    base = A_URL if source_id == "sites-a" else B_URL
    values: dict[str, Any] = {
        "source_id": source_id,
        "native_id": native_id,
        "url": f"{base}/{native_id}",
        "title": f"Talk number {native_id} on protein folding",
        "speaker_text": f"Speaker {native_id.upper()} Person",
        "start": MORNING + timedelta(hours=int(native_id[0])),
        "end": None,
    }
    return make_draft(**{**values, **overrides})


@pytest.fixture
def config(app_config: AppConfig) -> AppConfig:
    return dataclasses.replace(app_config, sources=SOURCES)


def services(emitters: FakeEmitters, **overrides: Any) -> Services:
    values: dict[str, Any] = {
        "http_for": lambda chosen: FakeHttpClient(),
        "event_filter": FakeEventFilter(),
        "field_tagger": FakeFieldTagger(),
        "picks_scorer": FakePicksScorer(),
        "write_ics_feeds": emitters.write_ics_feeds,
        "write_site_data": emitters.write_site_data,
        "update_health": emitters.update_health,
        "secret_scanner": build_secret_scanner(),
    }
    return Services(**{**values, **overrides})


def run(config: AppConfig, bundle: Services, *, days: int = 0, **options: Any) -> RunReport:
    return run_pipeline(
        config,
        bundle,
        now=REFERENCE_NOW + timedelta(days=days),
        options=RunOptions(**options),
        timer=FakeClock(),
    )


def stored(config: AppConfig) -> dict[str, Event]:
    return {event.uid: event for event in read_events_file(config.paths.events_file)}


def by_key(config: AppConfig) -> dict[str, Event]:
    return {key: event for event in stored(config).values() for key in event.source_ids}


class TestWholeRun:
    def test_a_run_writes_data_feeds_site_and_records(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1"), draft("sites-b", "2")]
        emitters = FakeEmitters()
        report = run(config, services(emitters))
        paths = config.paths
        assert report.ok
        assert exit_status(report) == EXIT_OK
        assert (report.counts.sources_ok, report.counts.events, report.counts.added) == (2, 2, 2)
        for path in (
            paths.events_file,
            paths.uid_map_file,
            paths.health_file,
            paths.last_run_file,
            paths.run_report_file,
            paths.public_feeds_dir / "all.ics",
            paths.public_events_file,
            paths.public_status_file,
        ):
            assert path.is_file(), path
        assert read_model(paths.run_report_file, RunReport) == report
        last = read_model(paths.last_run_file, LastRun)
        assert last is not None
        assert (last.ok, last.mode, last.started_at) == (True, "live", REFERENCE_NOW)
        assert set(by_key(config)) == {"sites-a:1", "sites-b:2"}
        assert all(event.first_seen == REFERENCE_NOW for event in stored(config).values())

    def test_one_failing_source_does_not_fail_the_run(
        self, config: AppConfig, fake_registry: AdapterRegistry
    ) -> None:
        FakeAdapter([draft("sites-b", "2")]).register(fake_registry, "fake")
        FakeAdapter(fetch_error=HttpStatusError(A_URL, 500)).register(fake_registry, "down")
        broken = dataclasses.replace(
            config, sources=(source("sites-a", A_URL, adapter="down"), SOURCES[1])
        )
        report = run(broken, services(FakeEmitters()))
        assert report.ok
        assert exit_status(report) == EXIT_OK
        assert report.counts.sources_failed == 1
        assert report.to_last_run().failed_sources == ["sites-a"]
        assert set(by_key(broken)) == {"sites-b:2"}

    def test_a_run_fails_only_when_every_source_failed(
        self, config: AppConfig, fake_registry: AdapterRegistry
    ) -> None:
        FakeAdapter(fetch_error=HttpStatusError(A_URL, 500)).register(fake_registry, "fake")
        report = run(config, services(FakeEmitters()))
        assert not report.ok
        assert exit_status(report) == EXIT_FAILED
        assert config.paths.last_run_file.is_file()

    def test_listings_of_one_talk_become_one_event(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        same = {
            "title": "Generative models for enzyme design",
            "speaker_text": "Ada Lovelace",
            "start": MORNING,
        }
        fake_adapter.drafts = [draft("sites-a", "1", **same), draft("sites-b", "9", **same)]
        report = run(config, services(FakeEmitters()))
        assert (report.counts.events, report.counts.merged) == (1, 1)
        [event] = stored(config).values()
        assert sorted(event.source_ids) == ["sites-a:1", "sites-b:9"]
        aliases = json.loads(config.paths.uid_map_file.read_text(encoding="utf-8"))["aliases"]
        assert aliases == {"sites-a:1": event.uid, "sites-b:9": event.uid}

    def test_excluded_events_are_reported_and_removed(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1"), draft("sites-a", "2")]
        run(config, services(FakeEmitters()))
        uid = by_key(config)["sites-a:2"].uid
        bundle = services(FakeEmitters(), event_filter=FakeEventFilter({uid: "course"}))
        report = run(config, bundle, days=1)
        assert [(item.reason, item.source_ids) for item in report.excluded] == [
            ("course", ["sites-a:2"])
        ]
        assert set(by_key(config)) == {"sites-a:1"}
        assert report.counts.removed == 1

    def test_events_without_fields_are_out_of_scope(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1")]
        scoped = dataclasses.replace(config, sources=(source("sites-a", A_URL, default_fields=[]),))
        report = run(scoped, services(FakeEmitters()))
        assert [item.reason for item in report.excluded] == ["out_of_scope"]
        assert stored(scoped) == {}

    def test_picks_are_scored_and_counted(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1")]
        run(config, services(FakeEmitters()))
        uid = by_key(config)["sites-a:1"].uid
        scorer = FakePicksScorer({uid: 2.5}, threshold=1.0)
        report = run(config, services(FakeEmitters(), picks_scorer=scorer), days=1)
        event = stored(config)[uid]
        assert (event.picks_score, event.picks_reasons) == (2.5, ["fake score 2.5"])
        assert report.counts.picks == 1

    def test_a_score_that_is_not_a_number_counts_as_zero(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1")]
        run(config, services(FakeEmitters()))
        uid = by_key(config)["sites-a:1"].uid
        scorer = FakePicksScorer({uid: float("nan")})
        run(config, services(FakeEmitters(), picks_scorer=scorer), days=1)
        assert stored(config)[uid].picks_score == 0.0


class TestStability:
    def test_the_same_input_gives_the_same_files(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1"), draft("sites-b", "2")]
        run(config, services(FakeEmitters()))
        first = config.paths.events_file.read_bytes()
        uid_map = config.paths.uid_map_file.read_bytes()
        report = run(config, services(FakeEmitters()), days=1)
        assert config.paths.events_file.read_bytes() == first
        assert config.paths.uid_map_file.read_bytes() == uid_map
        assert (report.counts.added, report.counts.changed) == (0, 0)

    def test_a_new_title_keeps_the_uid_and_raises_the_sequence(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1", title="TBA")]
        run(config, services(FakeEmitters()))
        before = by_key(config)["sites-a:1"]
        fake_adapter.drafts = [draft("sites-a", "1", title="Folding proteins in the cell")]
        report = run(config, services(FakeEmitters()), days=1)
        after = by_key(config)["sites-a:1"]
        assert after.uid == before.uid
        assert (after.sequence, after.title) == (1, "Folding proteins in the cell")
        assert after.last_changed == REFERENCE_NOW + timedelta(days=1)
        assert after.first_seen == REFERENCE_NOW
        assert report.counts.changed == 1

    def test_a_new_key_of_the_same_talk_keeps_the_uid(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1")]
        run(config, services(FakeEmitters()))
        uid = by_key(config)["sites-a:1"].uid
        fake_adapter.drafts = [draft("sites-a", "1b", title="Talk number 1 on protein folding")]
        run(config, services(FakeEmitters()), days=1)
        assert list(stored(config)) == [uid]
        assert by_key(config)["sites-a:1b"].uid == uid

    def test_a_tba_slot_keeps_its_uid_through_title_and_speaker(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        def slot(title: str, speaker: str | None = None) -> EventDraft:
            return make_draft(
                source_id="sites-a",
                native_id=None,
                url=None,
                title=title,
                speaker_text=speaker,
                start=MORNING,
                end=None,
            )

        fake_adapter.drafts = [slot("TBA")]
        run(config, services(FakeEmitters()))
        (uid,) = stored(config)
        fake_adapter.drafts = [slot("Folding proteins in the cell")]
        first = run(config, services(FakeEmitters()), days=1)
        assert list(stored(config)) == [uid]
        assert stored(config)[uid].title == "Folding proteins in the cell"
        fake_adapter.drafts = [slot("Folding proteins in the cell", "Ada Lovelace")]
        second = run(config, services(FakeEmitters()), days=2)
        (event,) = stored(config).values()
        assert event.uid == uid
        assert [speaker.name for speaker in event.speakers] == ["Ada Lovelace"]
        assert event.sequence == 2
        uid_map = read_model(config.paths.uid_map_file, UidMap)
        assert uid_map is not None
        keys = ["t0800", "folding-proteins-in-the-cell", "ada-lovelace"]
        assert {uid_map.resolve(f"sites-a:day:2026-11-17:{key}") for key in keys} == {uid}
        assert (first.counts.added, first.counts.changed) == (0, 1)
        assert (second.counts.added, second.counts.changed) == (0, 1)


class TestAbsence:
    def test_a_missing_event_is_removed_after_three_healthy_runs(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1"), draft("sites-a", "2")]
        run(config, services(FakeEmitters()))
        fake_adapter.drafts = [draft("sites-a", "1")]
        seen = []
        for day in (1, 2):
            report = run(config, services(FakeEmitters()), days=day)
            seen.append((by_key(config)["sites-a:2"].missing_runs, report.counts.missing))
        assert seen == [(1, 1), (2, 1)]
        report = run(config, services(FakeEmitters()), days=3)
        assert "sites-a:2" not in by_key(config)
        assert report.counts.removed == 1

    def test_an_event_that_returns_starts_counting_again(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1"), draft("sites-a", "2")]
        run(config, services(FakeEmitters()))
        fake_adapter.drafts = [draft("sites-a", "1")]
        run(config, services(FakeEmitters()), days=1)
        fake_adapter.drafts = [draft("sites-a", "1"), draft("sites-a", "2")]
        run(config, services(FakeEmitters()), days=2)
        assert by_key(config)["sites-a:2"].missing_runs == 0

    def test_a_failed_or_empty_source_changes_nothing(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1"), draft("sites-b", "2")]
        run(config, services(FakeEmitters()))
        fake_adapter.drafts = [draft("sites-b", "2")]  # sites-a is empty now
        run(config, services(FakeEmitters()), days=1)
        assert by_key(config)["sites-a:1"].missing_runs == 0
        fake_adapter.fetch_error = HttpStatusError(A_URL, 503)
        for day in (2, 3, 4):
            run(config, services(FakeEmitters()), days=day)
        assert by_key(config)["sites-a:1"].missing_runs == 0

    def test_a_source_left_out_of_the_run_changes_nothing(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1"), draft("sites-b", "2")]
        run(config, services(FakeEmitters()))
        for day in (1, 2, 3):
            run(config, services(FakeEmitters()), days=day, source_ids=("sites-b",))
        assert by_key(config)["sites-a:1"].missing_runs == 0

    def test_a_cancelled_talk_stays_until_its_time_has_passed(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [
            draft("sites-a", "1", title="Cancelled: Talk number 1 on protein folding"),
            draft("sites-a", "2"),
        ]
        report = run(config, services(FakeEmitters()))
        event = by_key(config)["sites-a:1"]
        assert event.status == "cancelled"
        assert report.counts.cancelled == 1
        fake_adapter.drafts = [draft("sites-a", "2")]
        for day in (1, 2, 3, 4):
            run(config, services(FakeEmitters()), days=day)
        assert by_key(config)["sites-a:1"] == event
        run(config, services(FakeEmitters()), days=30)
        assert "sites-a:1" not in by_key(config)
        archive = config.paths.archive_file(event.start.year).read_text(encoding="utf-8")
        assert event.uid in archive


class LeakyFeeds:
    """An ICS writer that puts a credential-shaped value into a feed."""

    def __init__(self, emitters: FakeEmitters, text: str) -> None:
        self._emitters = emitters
        self._text = text

    def __call__(self, events: Sequence[Event], out_dir: Path, *, config: AppConfig) -> list[Path]:
        paths = self._emitters.write_ics_feeds(events, out_dir, config=config)
        paths[0].write_text(f"X-LEAK:{self._text}\n", encoding="utf-8")
        return paths


class BrokenFeeds:
    def __call__(self, events: Sequence[Event], out_dir: Path, *, config: AppConfig) -> list[Path]:
        raise RuntimeError("disk full")


class TestOutputChecks:
    def test_a_secret_in_the_data_stops_every_data_file(
        self, config: AppConfig, fake_adapter: FakeAdapter, fake_issues: FakeIssueClient
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1", title="The leaky-value talk")]
        bundle = services(
            FakeEmitters(), secret_scanner=FakeSecretScanner(["leaky-value"]), issues=fake_issues
        )
        report = run(config, bundle)
        assert exit_status(report) == EXIT_OUTPUT_CHECK
        assert not report.ok
        assert {finding.origin for finding in report.secret_findings} == {"data/events.json"}
        assert not config.paths.events_file.exists()
        assert not config.paths.public_feeds_dir.exists()
        assert read_model(config.paths.run_report_file, RunReport) == report
        assert "the data files were not written" in report.warnings[-1]
        assert [issue.title for issue in fake_issues.issues] == [CHECK_ISSUE_TITLE]

    def test_a_secret_in_a_feed_is_found_on_disk(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1")]
        emitters = FakeEmitters()
        forged = "gh" + "p_" + "R" * 36
        report = run(config, services(emitters, write_ics_feeds=LeakyFeeds(emitters, forged)))
        assert exit_status(report) == EXIT_OUTPUT_CHECK
        assert [(f.origin, f.rule) for f in report.secret_findings] == [
            ("public/feeds/all.ics", "github-token")
        ]
        assert forged not in config.paths.run_report_file.read_text(encoding="utf-8")

    def test_a_charset_problem_below_data_fails_the_check(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1")]
        stray = config.paths.llm_cache_dir / "stray.json"
        stray.parent.mkdir(parents=True)
        stray.write_text("{}", encoding="utf-8")  # no final newline
        report = run(config, services(FakeEmitters()))
        assert exit_status(report) == EXIT_OUTPUT_CHECK
        assert report.charset_findings == ["data/llm_cache/stray.json:missing final newline"]

    def test_a_passing_run_closes_the_check_issue(
        self, config: AppConfig, fake_adapter: FakeAdapter, fake_issues: FakeIssueClient
    ) -> None:
        fake_issues.seed(CHECK_ISSUE_TITLE, body="old findings")
        fake_adapter.drafts = [draft("sites-a", "1")]
        run(config, services(FakeEmitters(), issues=fake_issues))
        assert fake_issues.get(1).state == "closed"

    def test_a_broken_writer_fails_the_run_but_keeps_the_data(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1")]
        report = run(config, services(FakeEmitters(), write_ics_feeds=BrokenFeeds()))
        assert exit_status(report) == EXIT_FAILED
        assert report.warnings == ["writing the feeds and the site data failed (RuntimeError)"]
        assert config.paths.events_file.is_file()


class TestHealthAndLLM:
    def test_the_health_history_grows_with_every_run(
        self, config: AppConfig, fake_adapter: FakeAdapter, fake_issues: FakeIssueClient
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1")]
        bundle = services(FakeEmitters(), update_health=update_health, issues=fake_issues)
        for day in (0, 1):
            run(config, bundle, days=day)
        health = json.loads(config.paths.health_file.read_text(encoding="utf-8"))
        statuses = [entry["status"] for entry in health["sources"]["sites-b"]["history"]]
        assert statuses == ["empty", "empty"]

    def test_the_llm_usage_and_the_cache_hits_are_reported(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1")]
        cache = FakeLLMCache({"k": {"v": 1}})
        cache.get("k")
        report = run(config, services(FakeEmitters(), llm=FakeLLMClient(), llm_cache=cache))
        assert (report.llm.enabled, report.llm.cache_hits) == (True, 1)
        assert cache.prunes == 1

    def test_rules_only_mode_is_reported(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        report = run(config, services(FakeEmitters(), warnings=("no LLM",)))
        assert not report.llm.enabled
        assert report.warnings == ["no LLM"]


class TestDryRun:
    def test_a_dry_run_changes_nothing_in_the_project(
        self, config: AppConfig, fake_adapter: FakeAdapter, fake_issues: FakeIssueClient
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1")]
        cache_entry = config.paths.llm_cache_dir / "entry.json"
        cache_entry.parent.mkdir(parents=True)
        cache_entry.write_text("{}\n", encoding="utf-8")
        with output_target(config.paths, dry_run=True) as target:
            assert (target.llm_cache_dir / "entry.json").is_file()
            report = run_pipeline(
                config,
                services(FakeEmitters(), issues=fake_issues),
                now=REFERENCE_NOW,
                options=RunOptions(dry_run=True),
                target=target,
                timer=FakeClock(),
            )
            assert target.events_file.is_file()
            scratch = target.root
        assert not scratch.exists()
        assert report.dry_run
        assert report.ok
        assert sorted(p.name for p in config.paths.data_dir.rglob("*")) == [
            "entry.json",
            "llm_cache",
        ]
        assert fake_issues.calls == []

    def test_a_real_run_writes_into_the_project(self, tmp_path: Path) -> None:
        paths = ProjectPaths(tmp_path)
        with output_target(paths, dry_run=False) as target:
            assert target is paths


class TestSelection:
    def test_every_enabled_source_by_default(self, config: AppConfig) -> None:
        disabled = source("sites-c", "https://c.example.edu/events", enabled=False)
        chosen = dataclasses.replace(config, sources=(*SOURCES, disabled))
        assert [s.id for s in select_sources(chosen)] == ["sites-a", "sites-b"]

    def test_named_sources_only(self, config: AppConfig) -> None:
        assert [s.id for s in select_sources(config, ["sites-b"])] == ["sites-b"]

    def test_unknown_and_disabled_sources_are_refused(self, config: AppConfig) -> None:
        disabled = source("sites-c", "https://c.example.edu/events", enabled=False)
        chosen = dataclasses.replace(config, sources=(*SOURCES, disabled))
        with pytest.raises(SourceSelectionError, match="unknown source id"):
            select_sources(chosen, ["sites-x"])
        with pytest.raises(SourceSelectionError, match="disabled source"):
            select_sources(chosen, ["sites-c"])

    def test_a_project_without_sources_cannot_run(self, app_config: AppConfig) -> None:
        with pytest.raises(RunError, match="no source is configured"):
            select_sources(app_config)

    def test_listing_urls_hold_the_fetched_and_home_pages(self) -> None:
        home = source("sites-h", A_URL, homepage="https://a.example.edu/")
        assert listing_urls([home]) >= {"https://a.example.edu/events"}
        assert len(listing_urls([home])) == 2


class TestSkipIfDone:
    def record(self, **overrides: Any) -> LastRun:
        values: dict[str, Any] = {
            "started_at": REFERENCE_NOW,
            "finished_at": REFERENCE_NOW,
            "ok": True,
            "mode": "live",
        }
        return LastRun(**{**values, **overrides})

    def test_a_successful_run_on_the_same_local_day_counts(self) -> None:
        later = REFERENCE_NOW + timedelta(hours=6)
        assert already_done_today(
            self.record(), now=later, timezone="America/Los_Angeles", mode="live"
        )

    def test_the_local_day_decides_not_the_utc_day(self) -> None:
        # 20:00 UTC is noon in California; 09:00 UTC the next day is 01:00 local, a new day.
        next_local_day = REFERENCE_NOW + timedelta(hours=13)
        assert not already_done_today(
            self.record(), now=next_local_day, timezone="America/Los_Angeles", mode="live"
        )
        same_utc_day_earlier = self.record(started_at=REFERENCE_NOW - timedelta(hours=13))
        assert not already_done_today(
            same_utc_day_earlier, now=REFERENCE_NOW, timezone="America/Los_Angeles", mode="live"
        )

    @pytest.mark.parametrize(
        "record", [None, {"ok": False}, {"mode": "offline"}], ids=["none", "failed", "offline"]
    )
    def test_other_runs_do_not_count(self, record: dict[str, Any] | None) -> None:
        last = None if record is None else self.record(**record)
        assert not already_done_today(
            last, now=REFERENCE_NOW, timezone="America/Los_Angeles", mode="live"
        )
