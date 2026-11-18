"""Tests for writing the files of a run as one set, and for what a crashed run leaves behind.

A run writes several files below data/. A failure while they are staged
must leave all of them as they were (a failure while they are moved into
place leaves the ones moved before it), and a run that crashed after
data/health.json and before its records must not be counted twice when
it is repeated.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import seminars.pipeline.run as run_module
from factories import REFERENCE_NOW, make_source
from fakes import (
    FakeAdapter,
    FakeClock,
    FakeEventFilter,
    FakeFieldTagger,
    FakeHttpClient,
    FakePicksScorer,
)
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig, ProjectPaths
from seminars.models import (
    HealthFile,
    LLMHealthEntry,
    RunMode,
    RunReport,
    SourceHealthEntry,
    UidMap,
)
from seminars.outputs.health import update_health
from seminars.pipeline import store
from seminars.pipeline.run import RunOptions, run_pipeline
from seminars.pipeline.secret_scan import build_secret_scanner
from seminars.pipeline.services import Services
from seminars.pipeline.store import data_files, load_state, report_files, write_files
from seminars.sources.base import AdapterRegistry
from seminars.textio import read_text

URL = "https://a.example.edu/events"


@pytest.fixture
def paths(tmp_path: Path) -> ProjectPaths:
    return ProjectPaths(tmp_path)


def leftovers(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.rglob("*.tmp"))


def failing_for(name: str, original: Callable[[Path, str], Path]) -> Callable[[Path, str], Path]:
    def stage(path: Path, text: str) -> Path:
        if path.name == name:
            raise OSError(f"simulated crash while writing {name}")
        return original(path, text)

    return stage


class TestWriteFiles:
    def test_every_file_is_written(self, paths: ProjectPaths) -> None:
        files = {paths.events_file: "a\n", paths.archive_file(2026): "b\n"}
        write_files(files)
        assert [read_text(path) for path in files] == ["a\n", "b\n"]
        assert leftovers(paths.root) == []

    def test_a_failure_while_writing_leaves_every_file_as_it_was(
        self, paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_files({paths.events_file: "old events\n", paths.uid_map_file: "old map\n"})
        monkeypatch.setattr(store, "_stage", failing_for("uid_map.json", store._stage))
        new = {
            paths.events_file: "new events\n",
            paths.health_file: "new health\n",
            paths.uid_map_file: "new map\n",
        }
        with pytest.raises(OSError, match="simulated crash"):
            write_files(new)
        assert read_text(paths.events_file) == "old events\n"
        assert read_text(paths.uid_map_file) == "old map\n"
        assert not paths.health_file.exists()
        assert leftovers(paths.root) == []

    def test_a_failure_during_the_moves_leaves_the_files_moved_before_it(
        self, paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_files({paths.events_file: "old events\n", paths.uid_map_file: "old map\n"})
        real_replace = os.replace

        def replace(source: Any, target: Any) -> None:
            if Path(target).name == "uid_map.json":
                raise PermissionError("simulated: another program holds uid_map.json open")
            real_replace(source, target)

        monkeypatch.setattr(store.os, "replace", replace)
        new = {paths.events_file: "new events\n", paths.uid_map_file: "new map\n"}
        with pytest.raises(PermissionError, match="simulated"):
            write_files(new)
        assert read_text(paths.events_file) == "new events\n"
        assert read_text(paths.uid_map_file) == "old map\n"
        assert leftovers(paths.root) == []

    def test_the_last_file_is_moved_last(
        self, paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        moved: list[str] = []
        real_replace = os.replace

        def replace(source: Any, target: Any) -> None:
            moved.append(Path(target).name)
            real_replace(source, target)

        monkeypatch.setattr(store.os, "replace", replace)
        moment = REFERENCE_NOW
        report = RunReport(started_at=moment, finished_at=moment, mode="live", ok=True)
        write_files(report_files(paths, report))
        assert moved == ["run_report.json", "last_run.json"]

    def test_the_text_is_kept_exactly(self, paths: ProjectPaths) -> None:
        text = "line one\r\nPoincar\u00e9\n"
        write_files({paths.events_file: text})
        assert paths.events_file.read_bytes() == text.encode("utf-8")


def entry(moment: datetime, status: Any = "ok") -> SourceHealthEntry:
    return SourceHealthEntry(at=moment, status=status, events=1)


def written(
    paths: ProjectPaths, health: HealthFile, *, started: datetime | None, mode: RunMode = "live"
) -> None:
    files = data_files(paths, events=[], uid_map=UidMap(), health=health, deadlines=())
    if started is not None:
        report = RunReport(started_at=started, finished_at=started, mode=mode, ok=True)
        files = {**files, **report_files(paths, report)}
    write_files(files)


class TestHealthOfARunThatDidNotFinish:
    def test_entries_after_the_last_finished_run_are_dropped(self, paths: ProjectPaths) -> None:
        day = timedelta(days=1)
        health = HealthFile()
        for moment, status in ((REFERENCE_NOW, "ok"), (REFERENCE_NOW + day, "failed")):
            health = health.with_source_entry("sites-x", entry(moment, status))
        health = health.with_llm_entry(
            "claude-cli", LLMHealthEntry(at=REFERENCE_NOW + day, status="ok")
        )
        written(paths, health, started=REFERENCE_NOW)
        state = load_state(paths)
        history = state.health.sources["sites-x"]
        assert [item.at for item in history.history] == [REFERENCE_NOW]
        assert history.last_success_at == REFERENCE_NOW
        assert state.health.llm["claude-cli"].history == []
        assert state.health.llm["claude-cli"].last_success_at is None

    def test_the_last_success_goes_back_to_the_last_kept_one(self, paths: ProjectPaths) -> None:
        health = HealthFile()
        for days in (0, 1, 2):
            moment = REFERENCE_NOW + timedelta(days=days)
            health = health.with_source_entry("sites-x", entry(moment, "ok" if days else "failed"))
        written(paths, health, started=REFERENCE_NOW + timedelta(days=1))
        history = load_state(paths).health.sources["sites-x"]
        assert history.last_success_at == REFERENCE_NOW + timedelta(days=1)
        assert len(history.history) == 2

    def test_a_finished_run_keeps_its_entries(self, paths: ProjectPaths) -> None:
        health = HealthFile().with_source_entry("sites-x", entry(REFERENCE_NOW))
        written(paths, health, started=REFERENCE_NOW)
        assert load_state(paths).health == health

    @pytest.mark.parametrize("mode", ["offline", None])
    def test_without_a_finished_live_run_nothing_is_dropped(
        self, paths: ProjectPaths, mode: RunMode | None
    ) -> None:
        later = REFERENCE_NOW + timedelta(days=3)
        health = HealthFile().with_source_entry("sites-x", entry(later))
        started = None if mode is None else REFERENCE_NOW
        written(paths, health, started=started, mode=mode or "live")
        assert load_state(paths).health == health


def services(emitters: FakeEmitters, **overrides: Any) -> Services:
    values: dict[str, Any] = {
        "http_for": lambda chosen: FakeHttpClient(),
        "event_filter": FakeEventFilter(),
        "field_tagger": FakeFieldTagger(),
        "picks_scorer": FakePicksScorer(),
        "write_ics_feeds": emitters.write_ics_feeds,
        "write_site_data": emitters.write_site_data,
        "update_health": update_health,
        "secret_scanner": build_secret_scanner(),
    }
    return Services(**{**values, **overrides})


class TestRepeatedRun:
    def test_a_run_repeated_after_a_crash_is_counted_once(
        self,
        app_config: AppConfig,
        fake_registry: AdapterRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        FakeAdapter(fetch_error=RuntimeError("down")).register(fake_registry, "down")
        FakeAdapter().register(fake_registry, "fake")
        config = dataclasses.replace(
            app_config,
            sources=(
                make_source(id="sites-a", adapter="down", url=URL),
                make_source(id="sites-b", adapter="fake", url="https://b.example.edu/events"),
            ),
        )

        def at(hours: int) -> RunReport:
            return run_pipeline(
                config,
                services(FakeEmitters()),
                now=REFERENCE_NOW + timedelta(hours=hours),
                options=RunOptions(),
                timer=FakeClock(),
            )

        def crash(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("simulated crash after the data files")

        at(0)
        with monkeypatch.context() as patch:
            patch.setattr(run_module, "_store_report", crash)
            with pytest.raises(RuntimeError, match="simulated crash"):
                at(24)
        on_disk = HealthFile.model_validate_json(read_text(config.paths.health_file))
        assert len(on_disk.sources["sites-a"].history) == 2
        assert len(load_state(config.paths).health.sources["sites-a"].history) == 1
        at(26)
        history = load_state(config.paths).health.sources["sites-a"].history
        assert [item.at for item in history] == [REFERENCE_NOW, REFERENCE_NOW + timedelta(hours=26)]
