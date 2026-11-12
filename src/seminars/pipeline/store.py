"""The committed state below data/: reading it, and the text of every file a run writes.

git is the database of this project, so a run reads the files of the last
run and writes new ones. Writing is split in two: the functions here build
the text of each file, which lets the output checks look at it first
(``seminars.pipeline.checks``), and ``write_files`` then puts the texts in
place, each file atomically.

Files (``seminars.config.ProjectPaths``):

* data/events.json: every event of the current window, one per line;
* data/uid_map.json: every source key ever seen with its uid;
* data/health.json: the run history per source and per LLM provider;
* data/archive/<year>.jsonl: events that left the window, by the year of
  their start; a file only ever grows;
* data/last_run.json and data/run_report.json: the record of the run.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ValidationError

from seminars.config import ProjectPaths
from seminars.models import (
    Event,
    HealthFile,
    LastRun,
    RunReport,
    UidMap,
    dump_event_lines,
    dump_events,
    dump_json,
    load_event_lines,
    load_events,
    load_json,
)
from seminars.textio import read_text, write_text_atomic


class StoreError(Exception):
    """A file below data/ cannot be read or is not what it should be."""


@dataclass(frozen=True)
class State:
    """What the last run left behind; empty values when there was none."""

    events: tuple[Event, ...] = ()
    uid_map: UidMap = field(default_factory=UidMap)
    health: HealthFile = field(default_factory=HealthFile)
    last_run: LastRun | None = None


def _problem(path: Path, error: Exception) -> StoreError:
    """A message that names the file and the kind of problem, never the content."""
    if isinstance(error, ValidationError):
        reason = f"{error.error_count()} value(s) do not fit the schema"
    else:
        reason = type(error).__name__
    return StoreError(f"{path}: cannot be read ({reason})")


def _read_model[T: BaseModel](path: Path, model: type[T]) -> T | None:
    if not path.exists():
        return None
    try:
        return load_json(read_text(path), model)
    except (OSError, ValueError) as error:  # ValidationError and JSON errors are ValueErrors
        raise _problem(path, error) from error


def read_events_file(path: Path) -> tuple[Event, ...]:
    """The events of data/events.json; none when the file does not exist."""
    if not path.exists():
        return ()
    try:
        return tuple(load_events(read_text(path)))
    except (OSError, ValueError) as error:
        raise _problem(path, error) from error


def load_state(paths: ProjectPaths) -> State:
    """Read what the last run stored; ``StoreError`` when a file is damaged."""
    return State(
        events=read_events_file(paths.events_file),
        uid_map=_read_model(paths.uid_map_file, UidMap) or UidMap(),
        health=_read_model(paths.health_file, HealthFile) or HealthFile(),
        last_run=_read_model(paths.last_run_file, LastRun),
    )


def archive_files(paths: ProjectPaths, archived: Sequence[Event]) -> dict[Path, str]:
    """The archive files that change when ``archived`` is added, with their new text.

    An event goes to the file of the year its start falls in. A uid that
    is archived again replaces its earlier line.
    """
    by_year: dict[int, list[Event]] = {}
    for event in archived:
        by_year.setdefault(event.start.year, []).append(event)
    files: dict[Path, str] = {}
    for year, events in sorted(by_year.items()):
        path = paths.archive_file(year)
        try:
            existing = load_event_lines(read_text(path)) if path.exists() else []
        except (OSError, ValueError) as error:
            raise _problem(path, error) from error
        merged = {event.uid: event for event in [*existing, *events]}
        files[path] = dump_event_lines(merged.values())
    return files


def data_files(
    paths: ProjectPaths,
    *,
    events: Sequence[Event],
    uid_map: UidMap,
    health: HealthFile,
    archived: Sequence[Event] = (),
) -> dict[Path, str]:
    """The text of every data file of a run except the two records of the run itself."""
    return {
        paths.events_file: dump_events(events),
        paths.uid_map_file: dump_json(uid_map),
        paths.health_file: dump_json(health),
        **archive_files(paths, archived),
    }


def report_files(paths: ProjectPaths, report: RunReport) -> dict[Path, str]:
    """The text of data/run_report.json and data/last_run.json for ``report``."""
    return {
        paths.run_report_file: dump_json(report),
        paths.last_run_file: dump_json(report.to_last_run()),
    }


def write_files(files: Mapping[Path, str]) -> None:
    """Write every file of ``files``, each one atomically, in the order of their paths."""
    for path, text in sorted(files.items()):
        write_text_atomic(path, text)


def state_files(paths: ProjectPaths) -> tuple[Path, ...]:
    """The files of the stored state that exist: events, uid map, health and the archive.

    The two records of the run and the LLM cache are not part of it.
    """
    archive = sorted(paths.archive_dir.glob("*.jsonl")) if paths.archive_dir.is_dir() else []
    found = [paths.events_file, paths.uid_map_file, paths.health_file, *archive]
    return tuple(path for path in found if path.is_file())


def remove_files(paths: Iterable[Path]) -> None:
    """Delete every file of ``paths``; one that is already gone is no error."""
    for path in paths:
        path.unlink(missing_ok=True)
