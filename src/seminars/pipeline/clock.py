"""The time of a run, fixed once.

Everything in the pipeline that needs "now" gets it handed in: the window
of events, the age of cache entries, ``first_seen`` and ``last_changed``.
``current_time`` is the one place that reads the wall clock, and only a
live run uses it. An offline run takes the moment its fixtures were
recorded, so recorded events never drift out of the window and the run
gives the same result every day.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from seminars.fixtures import reference_time


class ClockError(Exception):
    """The time of an offline run cannot be determined."""


def current_time() -> datetime:
    """The wall clock in UTC, to the second. Called once per command, never by a stage."""
    return datetime.now(UTC).replace(microsecond=0)


def run_time(*, offline: bool, fixtures_dir: Path) -> datetime:
    """The moment a run counts as happening at (timezone-aware, UTC).

    Live: the current time. Offline: the latest recording time of all
    fixtures in ``fixtures_dir``; ``ClockError`` when there is no fixture.
    """
    if not offline:
        return current_time()
    recorded = reference_time(fixtures_dir) if fixtures_dir.is_dir() else None
    if recorded is None:
        raise ClockError(
            f"offline mode needs recorded fixtures, and {fixtures_dir} has none; record some "
            "with 'uv run python -m seminars.fixtures record --source-id ID URL'"
        )
    return recorded.astimezone(UTC)
