"""Tests for reading and writing the state below data/."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from factories import PACIFIC, REFERENCE_NOW, make_event
from seminars.config import ProjectPaths
from seminars.models import (
    HealthFile,
    LastRun,
    RunReport,
    SourceHealthEntry,
    UidMap,
    load_event_lines,
    read_events,
    read_model,
)
from seminars.pipeline.store import (
    State,
    StoreError,
    archive_files,
    data_files,
    load_state,
    read_events_file,
    report_files,
    write_files,
)
from seminars.textio import read_text


@pytest.fixture
def paths(tmp_path: Path) -> ProjectPaths:
    return ProjectPaths(tmp_path)


def report(ok: bool = True) -> RunReport:
    return RunReport(started_at=REFERENCE_NOW, finished_at=REFERENCE_NOW, mode="offline", ok=ok)


class TestLoad:
    def test_a_project_without_data_has_an_empty_state(self, paths: ProjectPaths) -> None:
        assert load_state(paths) == State()
        assert read_events_file(paths.events_file) == ()

    def test_a_round_trip(self, paths: ProjectPaths) -> None:
        events = [make_event(uid="b"), make_event(uid="a", title="Poincar\u00e9 duality")]
        uid_map = UidMap(aliases={"sites-x:1": "a"})
        entry = SourceHealthEntry(at=REFERENCE_NOW, status="ok", events=2)
        health = HealthFile().with_source_entry("sites-x", entry)
        files = data_files(paths, events=events, uid_map=uid_map, health=health, deadlines=())
        write_files({**files, **report_files(paths, report())})
        state = load_state(paths)
        assert [event.uid for event in state.events] == ["a", "b"]
        assert state.events[0].title == "Poincar\u00e9 duality"
        assert (state.uid_map, state.health) == (uid_map, health)
        assert state.last_run == report().to_last_run()
        assert read_model(paths.run_report_file, RunReport) == report()

    def test_writing_the_same_state_twice_gives_the_same_bytes(self, paths: ProjectPaths) -> None:
        events = [make_event(uid="b"), make_event(uid="a")]
        files = data_files(
            paths, events=events, uid_map=UidMap(), health=HealthFile(), deadlines=()
        )
        again = data_files(
            paths, events=events[::-1], uid_map=UidMap(), health=HealthFile(), deadlines=()
        )
        assert files == again
        write_files(files)
        assert {path: read_text(path) for path in files} == files

    @pytest.mark.parametrize("name", ["events.json", "uid_map.json", "health.json"])
    @pytest.mark.parametrize("content", ["{not json", '{"schema_version": 99}', "[]"])
    def test_a_damaged_file_is_an_error_that_names_the_file_only(
        self, paths: ProjectPaths, name: str, content: str
    ) -> None:
        paths.data_dir.mkdir()
        (paths.data_dir / name).write_text(content, encoding="utf-8")
        with pytest.raises(StoreError) as caught:
            load_state(paths)
        assert name in str(caught.value)
        assert content not in str(caught.value)

    def test_a_damaged_last_run_is_an_error(self, paths: ProjectPaths) -> None:
        paths.data_dir.mkdir()
        paths.last_run_file.write_text('{"ok": "perhaps"}', encoding="utf-8")
        with pytest.raises(StoreError, match=r"last_run\.json"):
            load_state(paths)


class TestArchive:
    def test_events_go_to_the_file_of_the_year_they_start_in(self, paths: ProjectPaths) -> None:
        new_year = make_event(uid="ny", start=datetime(2027, 1, 1, 0, 30, tzinfo=PACIFIC), end=None)
        files = archive_files(paths, [make_event(uid="b"), make_event(uid="a"), new_year])
        assert sorted(path.name for path in files) == ["2026.jsonl", "2027.jsonl"]
        this_year = load_event_lines(files[paths.archive_file(2026)])
        assert [event.uid for event in this_year] == ["a", "b"]

    def test_the_year_is_the_local_year(self, paths: ProjectPaths) -> None:
        """2027-01-01 07:30 UTC is still New Year's Eve in California."""
        eve = make_event(uid="eve", start=datetime(2026, 12, 31, 23, 30, tzinfo=PACIFIC), end=None)
        assert list(archive_files(paths, [eve])) == [paths.archive_file(2026)]

    def test_an_archive_only_grows_and_a_repeated_uid_is_replaced(
        self, paths: ProjectPaths
    ) -> None:
        write_files(archive_files(paths, [make_event(uid="a"), make_event(uid="b")]))
        write_files(archive_files(paths, [make_event(uid="c"), make_event(uid="a", title="New")]))
        events = load_event_lines(read_text(paths.archive_file(2026)))
        assert [(event.uid, event.title == "New") for event in events] == [
            ("a", True),
            ("b", False),
            ("c", False),
        ]

    def test_nothing_to_archive_touches_no_file(self, paths: ProjectPaths) -> None:
        assert archive_files(paths, []) == {}
        files = data_files(paths, events=[], uid_map=UidMap(), health=HealthFile(), deadlines=())
        names = sorted(path.name for path in files)
        assert names == ["deadlines.json", "events.json", "health.json", "uid_map.json"]

    def test_a_damaged_archive_is_an_error(self, paths: ProjectPaths) -> None:
        paths.archive_dir.mkdir(parents=True)
        paths.archive_file(2026).write_text("{broken\n", encoding="utf-8")
        with pytest.raises(StoreError, match=r"2026\.jsonl"):
            archive_files(paths, [make_event()])


class TestReportFiles:
    def test_the_two_records_of_a_run(self, paths: ProjectPaths) -> None:
        write_files(report_files(paths, report(ok=False)))
        last = read_model(paths.last_run_file, LastRun)
        assert last is not None
        assert (last.ok, last.mode) == (False, "offline")
        assert read_events(paths.events_file) == []
        assert read_text(paths.run_report_file).endswith("}\n")
