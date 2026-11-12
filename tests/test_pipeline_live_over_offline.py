"""Tests for a live run that finds the result of an offline run below data/.

An offline run serves every source from its recordings, so whatever it
leaves below data/ (events, the uid map, the health history, the archive)
comes from the fixtures and must never be published. A live run that finds
it starts from nothing, says so, and then works as always.
"""

from __future__ import annotations

import json
from datetime import timedelta

from factories import REFERENCE_NOW, make_event
from fakes import FakeAdapter, FakeClock, FakeIssueClient, FakeSecretScanner
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig
from seminars.models import LastRun, RunReport, UidMap, dump_event_lines, dump_json, read_model
from seminars.outputs.health import update_health
from seminars.pipeline.run import (
    EXIT_OUTPUT_CHECK,
    LIVE_OVER_OFFLINE,
    NOT_PUBLISHABLE,
    REPORT_WITHHELD,
    RunOptions,
    exit_status,
    output_target,
    run_pipeline,
)
from seminars.pipeline.store import load_state, read_events_file
from test_pipeline_run import by_key, config, draft, run, services

__all__ = ["config"]  # the fixture of test_pipeline_run


def offline_result(config: AppConfig, fake_adapter: FakeAdapter) -> None:
    """What an offline run leaves behind, with an archived and a merged fixture talk."""
    fake_adapter.drafts = [
        draft("sites-a", "1", title="Fixture talk"),
        draft("sites-b", "2", title="Fixture talk too"),
    ]
    report = run(config, services(FakeEmitters(), update_health=update_health), offline=True)
    assert report.ok
    archived = make_event(uid="sites-a.old", start=REFERENCE_NOW - timedelta(days=400))
    archive = config.paths.archive_file(archived.start.year)
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(dump_event_lines([archived]), encoding="utf-8")
    uid_map = UidMap().with_aliases({"sites-b:merged": "sites-a.1"})
    config.paths.uid_map_file.write_text(dump_json(uid_map), encoding="utf-8")


class TestLiveOverOffline:
    def test_the_live_run_starts_from_no_stored_events(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        offline_result(config, fake_adapter)
        fake_adapter.drafts = [draft("sites-a", "3")]
        report = run(config, services(FakeEmitters(), update_health=update_health), days=1)
        assert report.ok
        assert report.mode == "live"
        assert report.warnings[0] == LIVE_OVER_OFFLINE
        assert sorted(by_key(config)) == ["sites-a:3"]
        counts = report.counts
        assert (counts.events, counts.added, counts.missing, counts.removed) == (1, 1, 0, 0)
        assert counts.archived == 0
        state = load_state(config.paths)
        assert sorted(state.uid_map.aliases) == ["sites-a:3"]
        histories = {key: len(value.history) for key, value in state.health.sources.items()}
        assert histories == {"sites-a": 1, "sites-b": 1}
        assert not config.paths.archive_dir.exists() or not any(config.paths.archive_dir.iterdir())
        last_run = read_model(config.paths.last_run_file, LastRun)
        assert last_run is not None
        assert last_run.mode == "live"

    def test_the_next_live_run_works_from_what_the_first_one_stored(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        offline_result(config, fake_adapter)
        fake_adapter.drafts = [draft("sites-a", "3"), draft("sites-a", "4")]
        run(config, services(FakeEmitters()), days=1)
        fake_adapter.drafts = [draft("sites-a", "3")]
        report = run(config, services(FakeEmitters()), days=2)
        assert LIVE_OVER_OFFLINE not in report.warnings
        assert by_key(config)["sites-a:4"].missing_runs == 1
        assert report.counts.missing == 1

    def test_an_offline_run_over_offline_data_keeps_it(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        offline_result(config, fake_adapter)
        report = run(config, services(FakeEmitters()), offline=True)
        assert LIVE_OVER_OFFLINE not in report.warnings
        assert report.counts.added == 0
        assert sorted(by_key(config)) == ["sites-a:1", "sites-b:2"]
        assert load_state(config.paths).uid_map.resolve("sites-b:merged") == "sites-a.1"

    def test_a_live_dry_run_says_so_and_changes_nothing(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        offline_result(config, fake_adapter)
        before = {
            path: path.read_bytes() for path in config.paths.data_dir.rglob("*") if path.is_file()
        }
        fake_adapter.drafts = [draft("sites-a", "3")]
        with output_target(config.paths, dry_run=True) as target:
            report = run_pipeline(
                config,
                services(FakeEmitters()),
                now=REFERENCE_NOW + timedelta(days=1),
                options=RunOptions(dry_run=True),
                target=target,
                timer=FakeClock(),
            )
            assert [event.uid for event in read_events_file(target.events_file)] == ["sites-a.3"]
        assert report.warnings[0] == LIVE_OVER_OFFLINE
        assert report.counts.events == 1
        after = {
            path: path.read_bytes() for path in config.paths.data_dir.rglob("*") if path.is_file()
        }
        assert after == before

    def test_the_fixture_data_goes_even_when_the_check_stops_the_new_files(
        self, config: AppConfig, fake_adapter: FakeAdapter, fake_issues: FakeIssueClient
    ) -> None:
        offline_result(config, fake_adapter)
        fake_adapter.drafts = [draft("sites-a", "3", title="The leaky-value talk")]
        leaky = services(FakeEmitters(), secret_scanner=FakeSecretScanner(["leaky-value"]))
        report = run(config, leaky, days=1)
        assert exit_status(report) == EXIT_OUTPUT_CHECK
        paths = config.paths
        for path in (paths.events_file, paths.uid_map_file, paths.health_file):
            assert not path.exists(), path
        assert not any(paths.archive_dir.glob("*.jsonl"))
        fake_adapter.drafts = [draft("sites-a", "5")]
        later = run(config, services(FakeEmitters(), issues=fake_issues), days=2)
        assert later.ok
        assert LIVE_OVER_OFFLINE not in later.warnings
        assert sorted(by_key(config)) == ["sites-a:5"]
        health = json.loads(paths.health_file.read_text(encoding="utf-8"))
        histories = {key: len(value["history"]) for key, value in health["sources"].items()}
        assert histories == {"sites-a": 1, "sites-b": 1}

    def test_the_warning_stays_when_the_report_loses_its_free_text(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        offline_result(config, fake_adapter)
        fake_adapter.drafts = [draft("sites-a", "3")]
        forged = "gh" + "p_" + "Q" * 36
        report = run(config, services(FakeEmitters(), warnings=(f"note {forged}",)), days=1)
        assert exit_status(report) == EXIT_OUTPUT_CHECK
        assert report.warnings == [LIVE_OVER_OFFLINE, REPORT_WITHHELD, NOT_PUBLISHABLE]
        assert read_model(config.paths.run_report_file, RunReport) == report
        last_run = read_model(config.paths.last_run_file, LastRun)
        assert last_run is not None
        assert last_run.mode == "live"
