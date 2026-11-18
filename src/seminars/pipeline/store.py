"""The committed state below data/: reading it, and the text of every file a run writes.

git is the database of this project, so a run reads the files of the last
run and writes new ones. Writing is split in two: the functions here build
the text of each file, which lets the output checks look at it first
(``seminars.pipeline.checks``), and ``write_files`` then puts the texts in
place.

A run writes its files in two sets: the data files (``data_files``), and,
after the feeds and the site data, the two records of the run
(``report_files``). ``write_files`` writes one set in two steps. Every
text goes to a temporary file next to its target first, flushed to disk;
a failure in this step (a full disk, a file that cannot be created)
changes none of the files and leaves no temporary file behind. Only when
all of them are on disk are they moved into place, one after the other,
data/last_run.json last of all. That file therefore says which run
finished last. A move is atomic, a set of them is not: a failure during
the moves (a target that another program holds open, on Windows) leaves
the files moved before it new and the others as they were, so
data/events.json can be new while data/uid_map.json, data/health.json or
the archive is old; the temporary files are removed and the run fails.

A run that stopped in between (a crash after the data files, a failure
during their moves, a machine that went down) leaves whatever of its data
files reached their place next to the records of the run before, and
possibly its outcome in the health history. So that repeating it does
not count it twice, ``load_state`` drops from the health history every
entry of a run that started after the last finished live run
(``HealthFile.until``). The events and the uid map such a run wrote stay:
the next run builds on them as on any other stored state.

Files (``seminars.config.ProjectPaths``):

* data/events.json: every event of the current window, one per line;
* data/uid_map.json: every source key ever seen with its uid;
* data/health.json: the run history per source and per LLM provider;
* data/deadlines.json: the deadlines of the registry of meetings, one per
  line (``seminars.pipeline.deadlines``); a missing file is no deadline;
* data/archive/<year>.jsonl: events that left the window, by the year of
  their start; a file only ever grows;
* data/last_run.json and data/run_report.json: the record of the run.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ValidationError

from seminars.config import ProjectPaths
from seminars.models import (
    Deadline,
    Event,
    HealthFile,
    LastRun,
    RunReport,
    UidMap,
    dump_deadlines,
    dump_event_lines,
    dump_events,
    dump_json,
    load_deadlines,
    load_event_lines,
    load_events,
    load_json,
)
from seminars.textio import read_text


class StoreError(Exception):
    """A file below data/ cannot be read or is not what it should be."""


@dataclass(frozen=True)
class State:
    """What the last run left behind; empty values when there was none."""

    events: tuple[Event, ...] = ()
    uid_map: UidMap = field(default_factory=UidMap)
    health: HealthFile = field(default_factory=HealthFile)
    last_run: LastRun | None = None
    deadlines: tuple[Deadline, ...] = ()


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


def read_deadlines_file(path: Path) -> tuple[Deadline, ...]:
    """The deadlines of data/deadlines.json; none when the file does not exist."""
    if not path.exists():
        return ()
    try:
        return tuple(load_deadlines(read_text(path)))
    except (OSError, ValueError) as error:
        raise _problem(path, error) from error


def finished_health(health: HealthFile, last_run: LastRun | None) -> HealthFile:
    """``health`` without the entries of runs that did not finish (see the module docstring).

    Only a finished live run gives the measure: without one, and after an
    offline run (whose time is that of the recordings), the history stays
    as it is.
    """
    if last_run is None or last_run.mode != "live":
        return health
    return health.until(last_run.started_at)


def load_state(paths: ProjectPaths) -> State:
    """Read what the last run stored; ``StoreError`` when a file is damaged."""
    last_run = _read_model(paths.last_run_file, LastRun)
    health = _read_model(paths.health_file, HealthFile) or HealthFile()
    return State(
        events=read_events_file(paths.events_file),
        uid_map=_read_model(paths.uid_map_file, UidMap) or UidMap(),
        health=finished_health(health, last_run),
        last_run=last_run,
        deadlines=read_deadlines_file(paths.deadlines_file),
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
    deadlines: Sequence[Deadline],
    archived: Sequence[Event] = (),
) -> dict[Path, str]:
    """The text of every data file of a run except the two records of the run itself.

    ``deadlines`` has no default: data/deadlines.json is written from it, and
    a call that left it out would empty the stored deadlines, so that the
    next run counted every one of them as new.
    """
    return {
        paths.events_file: dump_events(events),
        paths.uid_map_file: dump_json(uid_map),
        paths.health_file: dump_json(health),
        paths.deadlines_file: dump_deadlines(deadlines),
        **archive_files(paths, archived),
    }


def report_files(paths: ProjectPaths, report: RunReport) -> dict[Path, str]:
    """The text of data/run_report.json and data/last_run.json for ``report``, the latter last."""
    return {
        paths.run_report_file: dump_json(report),
        paths.last_run_file: dump_json(report.to_last_run()),
    }


def _stage(path: Path, text: str) -> Path:
    """Write ``text`` to a new temporary file next to ``path``, flushed to disk; returns it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return temp


def _sync_directory(directory: Path) -> None:
    """Make the moves into ``directory`` durable where the system allows it."""
    with contextlib.suppress(OSError):  # not every system opens a directory
        handle = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)


def write_files(files: Mapping[Path, str]) -> None:
    """Write every file of ``files`` as one set (see the module docstring).

    The files are moved into place in the order of ``files``, so the last
    one is the last to change. A failure before the first move changes no
    file; a failure during the moves leaves the files moved before it new
    and the others as they were. Either way no temporary file is left
    behind.
    """
    staged: list[tuple[Path, Path]] = []
    try:
        for path, text in files.items():
            staged.append((_stage(path, text), path))
        for temp, path in staged:
            os.replace(temp, path)
    except BaseException:
        for temp, _ in staged:
            temp.unlink(missing_ok=True)
        raise
    for directory in sorted({path.parent for _, path in staged}):
        _sync_directory(directory)


def state_files(paths: ProjectPaths) -> tuple[Path, ...]:
    """The files of the stored state that exist: events, uid map, health, deadlines, archive.

    The two records of the run and the LLM cache are not part of it.
    """
    archive = sorted(paths.archive_dir.glob("*.jsonl")) if paths.archive_dir.is_dir() else []
    found = [paths.events_file, paths.uid_map_file, paths.health_file, paths.deadlines_file]
    found += archive
    return tuple(path for path in found if path.is_file())


def remove_files(paths: Iterable[Path]) -> None:
    """Delete every file of ``paths``; one that is already gone is no error."""
    for path in paths:
        path.unlink(missing_ok=True)
