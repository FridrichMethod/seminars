"""Assembling public/ from the committed data, and checking what is there.

``assemble_site`` backs ``seminars build-site``. It builds public/ from
scratch out of what is committed: the static site (site/), the locale
files (locales/), and the feeds and site data written from data/ by the
output writers. It does not depend on a run having happened in the same
job: a run that was skipped because the day's work was done leaves data/
as committed, and the site comes out the same. The time stamp of the site
data is the end of the last run, so the same data gives the same files.

``validate_project`` backs ``seminars validate``. It reads every file
below data/ against its schema, the site data in public/ against the
public payloads, every feed with the ICS validator (the feed of the
deadlines with its own rules as well), and runs the output checks of a run
(secret scan and the charset rule of data/). The files a published site
needs (data/events.json, public/events.json, public/status.json, every feed
and the feed of the deadlines) must exist: a project without them has
nothing to publish and does not pass, so "seminars validate passes" says
that a run and a build of the site happened. data/deadlines.json may be
missing, which is a project without deadlines.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from seminars.config import AppConfig, ProjectPaths
from seminars.contracts import IcsFeedWriter, IcsValidator, SecretScanner, SiteDataWriter
from seminars.models import (
    DEADLINES_FEED,
    FEED_IDS,
    PublicEventsPayload,
    PublicStatusPayload,
    load_event_lines,
    read_model,
)
from seminars.pipeline.checks import CheckResult, check_tree
from seminars.pipeline.store import StoreError, load_state
from seminars.textio import read_text
from seminars.textrules import ascii_printable


@dataclass(frozen=True)
class SiteBuild:
    """What ``assemble_site`` put into public/ (paths relative to it) and what is missing."""

    files: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()  # parts of the site that could not be assembled


@dataclass(frozen=True)
class Validation:
    """Outcome of ``validate_project``: schema problems and output check findings."""

    problems: tuple[str, ...] = ()
    checks: CheckResult = field(default_factory=CheckResult)
    checked: tuple[str, ...] = ()  # files that were looked at, relative to the project


def _copy_tree(source: Path, target: Path) -> list[Path]:
    """Copy the regular files below ``source`` to ``target``; symbolic links are skipped."""
    copied: list[Path] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        copied.append(destination)
    return copied


def assemble_site(
    config: AppConfig,
    *,
    write_ics_feeds: IcsFeedWriter,
    write_site_data: SiteDataWriter,
    fallback_time: datetime,
) -> SiteBuild:
    """Build public/ from scratch (see the module docstring).

    ``fallback_time`` stamps the site data when no run has been recorded
    yet. Raises ``StoreError`` when the stored state is damaged.
    """
    paths = config.paths
    state = load_state(paths)
    if paths.public_dir.exists():
        shutil.rmtree(paths.public_dir)
    paths.public_dir.mkdir(parents=True)
    missing: list[str] = []
    written: list[Path] = []
    for directory, target in (
        (paths.site_dir, paths.public_dir),
        (paths.locales_dir, paths.public_locales_dir),
    ):
        if directory.is_dir():
            written += _copy_tree(directory, target)
        else:
            missing.append(f"{directory.relative_to(paths.root).as_posix()}/")
    written += write_ics_feeds(
        state.events, paths.public_feeds_dir, config=config, deadlines=state.deadlines
    )
    generated_at = state.last_run.finished_at if state.last_run else fallback_time
    written += write_site_data(
        state.events,
        state.health,
        state.last_run,
        paths.public_dir,
        config=config,
        generated_at=generated_at,
    )
    names = sorted({path.relative_to(paths.public_dir).as_posix() for path in written})
    return SiteBuild(files=tuple(names), missing=tuple(missing))


def _relative(path: Path, root: Path) -> str:
    return ascii_printable(path.relative_to(root).as_posix())


def _model_problems(path: Path, model: type[BaseModel], root: Path) -> list[str]:
    try:
        read_model(path, model)
    except (OSError, ValueError, ValidationError) as error:
        detail = (
            f"{error.error_count()} value(s) do not fit the schema"
            if isinstance(error, ValidationError)
            else type(error).__name__
        )
        return [f"{_relative(path, root)}: {detail}"]
    return []


def _archive_problems(paths: ProjectPaths) -> list[str]:
    problems: list[str] = []
    for path in sorted(paths.archive_dir.glob("*.jsonl")) if paths.archive_dir.is_dir() else []:
        try:
            load_event_lines(read_text(path))
        except (OSError, ValueError) as error:
            problems.append(f"{_relative(path, paths.root)}: {type(error).__name__}")
    return problems


def _missing_problems(paths: ProjectPaths) -> list[str]:
    """The files a published site needs that are not there, with the command that makes them."""
    required = (
        (paths.events_file, "seminars run"),
        (paths.public_events_file, "seminars build-site"),
        (paths.public_status_file, "seminars build-site"),
    )
    return [
        f"{_relative(path, paths.root)}: missing (made by {command})"
        for path, command in required
        if not path.is_file()
    ]


def _feed_problems(paths: ProjectPaths, validator: IcsValidator | None) -> list[str]:
    feeds_dir = paths.public_feeds_dir
    problems = [
        f"{_relative(feeds_dir, paths.root)}/{feed}.ics: missing"
        for feed in (*FEED_IDS, DEADLINES_FEED)
        if not (feeds_dir / f"{feed}.ics").is_file()
    ]
    feeds = sorted(feeds_dir.glob("*.ics"))
    if validator is None:
        return [*problems, *(f"{_relative(f, paths.root)}: no ICS validator" for f in feeds)]
    for feed in feeds:
        text = feed.read_bytes().decode("utf-8", errors="replace")
        found = validator(text, deadlines=feed.stem == DEADLINES_FEED)
        problems += [f"{_relative(feed, paths.root)}: {ascii_printable(p)}" for p in found]
    return problems


def _files(paths: ProjectPaths, directories: Sequence[Path]) -> tuple[str, ...]:
    found = [
        _relative(path, paths.root)
        for directory in directories
        if directory.is_dir()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    ]
    return tuple(found)


def validate_project(
    paths: ProjectPaths, *, validator: IcsValidator | None, scanner: SecretScanner
) -> Validation:
    """Check data/ and public/ of the project (see the module docstring).

    ``validator`` is ``None`` when the ICS writer's package is missing; a
    feed then cannot be validated, which counts as a problem.
    """
    problems = _missing_problems(paths)
    try:
        load_state(paths)
    except StoreError as error:
        problems.append(ascii_printable(str(error)))
    problems += _archive_problems(paths)
    for path, model in (
        (paths.public_events_file, PublicEventsPayload),
        (paths.public_status_file, PublicStatusPayload),
    ):
        problems += _model_problems(path, model, paths.root)
    problems += _feed_problems(paths, validator)
    return Validation(
        problems=tuple(problems),
        checks=check_tree(paths, scanner),
        checked=_files(paths, [paths.data_dir, paths.public_dir]),
    )
