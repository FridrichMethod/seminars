"""Tests for what status.json says about a source that was not read in full.

A run that skipped pages or found a listing far smaller than usual still
delivers events, so the state of the source stays what its run gave. The
row says on top of that that the run was not read in full, and why; a run
that took a smaller listing as the new usual size says that instead.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from pathlib import Path
from typing import Any

from factories import make_source
from seminars.config import AppConfig
from seminars.models import HealthFile, PublicSourceStatus, SourceHealthEntry
from test_site_data import RAN_AT, write

SOURCE_ID = "sites-bioengineering"


def last_run(**values: Any) -> HealthFile:
    """A health history whose last run of the source has ``values``."""
    entry = SourceHealthEntry.model_validate({"at": RAN_AT, "status": "ok", **values})
    return HealthFile().with_source_entry(SOURCE_ID, entry)


def row(config: AppConfig, tmp_path: Path, health: HealthFile) -> PublicSourceStatus:
    only_source = dataclasses.replace(config, sources=(make_source(),))
    _, status = write(only_source, tmp_path, health=health)
    (only,) = status.sources
    return only


def completeness(status: PublicSourceStatus) -> tuple[Any, ...]:
    return (
        status.read_in_full,
        status.skipped_pages,
        status.listed,
        status.usual_size,
        status.resized_from,
    )


class TestASourceNotReadInFull:
    def test_a_run_read_in_full_says_nothing_more(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        status = row(app_config, tmp_path, last_run(events=12, accepted=14))
        assert completeness(status) == (True, 0, None, None, None)

    def test_pages_that_could_not_be_fetched_are_counted(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        status = row(app_config, tmp_path, last_run(events=3, accepted=3, skipped_pages=2))
        assert (status.state, status.events) == ("ok", 3)
        assert completeness(status) == (False, 2, None, None, None)

    def test_a_listing_far_smaller_than_usual_says_both_sizes(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        status = row(app_config, tmp_path, last_run(events=4, accepted=5, shrank_from=20))
        assert completeness(status) == (False, 0, 5, 20, None)

    def test_a_smaller_listing_taken_as_the_new_usual_size_says_so(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        status = row(app_config, tmp_path, last_run(events=5, accepted=5, resized_from=20))
        assert completeness(status) == (True, 0, 5, None, 20)

    def test_only_the_last_run_counts(self, app_config: AppConfig, tmp_path: Path) -> None:
        health = last_run(events=5, accepted=5, shrank_from=20)
        later = SourceHealthEntry(at=RAN_AT + timedelta(days=1), status="ok", events=20)
        status = row(app_config, tmp_path, health.with_source_entry(SOURCE_ID, later))
        assert completeness(status) == (True, 0, None, None, None)

    def test_a_disabled_source_shows_nothing_of_its_last_run(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        config = dataclasses.replace(app_config, sources=(make_source(enabled=False),))
        _, status = write(config, tmp_path, health=last_run(events=4, accepted=5, shrank_from=20))
        (only,) = status.sources
        assert only.state == "disabled"
        assert completeness(only) == (True, 0, None, None, None)
