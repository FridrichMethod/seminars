"""An offline run of three of the sources that keep their meetings a year ahead.

``seminars run --offline --no-llm`` over the recordings of sites-aimi,
custom-hai-events and bayarea-slac-workshops, three of the sources with
``meeting_horizon``: their conferences and long workshops after the window of
talks are stored, their talks are not. The project holds the fixtures of these
three sources only, so the run happens at the latest recording of the three: a
recording of another source cannot move the window of talks over a meeting.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from conftest import REPO_ROOT
from seminars import cli
from seminars.config import PROVIDER_CREDENTIAL_ENV, load_settings, load_sources
from seminars.fixtures import reference_time
from seminars.models import Event, TimeWindow, read_events

SOURCES = ("sites-aimi", "custom-hai-events", "bayarea-slac-workshops")
PACIFIC = ZoneInfo("America/Los_Angeles")

MEETINGS = {
    "AIMI Symposium + Summit Series 2027": (
        "sites-aimi",
        "conference",
        True,
        datetime(2027, 5, 17, tzinfo=PACIFIC),
        datetime(2027, 5, 20, tzinfo=PACIFIC),
    ),
    "2027 Conference on Physics and AI (PhyStat 2027)": (
        "custom-hai-events",
        "conference",
        False,
        datetime(2027, 5, 24, 9, tzinfo=PACIFIC),
        datetime(2027, 5, 26, 17, tzinfo=PACIFIC),
    ),
    "Reinforcement Learning for Autonomous Accelerators (RL4AA'27)": (
        "bayarea-slac-workshops",
        "workshop",
        False,
        datetime(2027, 4, 20, 13, tzinfo=PACIFIC),
        datetime(2027, 4, 23, 17, 30, tzinfo=PACIFIC),
    ),
}
"""The meetings after the window of talks: source, type, all day, start, end."""


@pytest.fixture(scope="module")
def offline_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The project that one offline run of the three sources wrote into."""
    root = tmp_path_factory.mktemp("horizon") / "project"
    shutil.copytree(REPO_ROOT / "config", root / "config")
    shutil.copytree(REPO_ROOT / "locales", root / "locales")
    for source in SOURCES:
        shutil.copytree(
            REPO_ROOT / "tests" / "fixtures" / source, root / "tests" / "fixtures" / source
        )
    arguments = ["run", "--offline", "--no-llm"]
    for source in SOURCES:
        arguments += ["--source", source]
    with pytest.MonkeyPatch.context() as patch:
        for name in (*filter(None, PROVIDER_CREDENTIAL_ENV.values()), "GITHUB_TOKEN"):
            patch.delenv(name, raising=False)
        for name in ("LLM_PROVIDERS", "GITHUB_ACTIONS", "GITHUB_REPOSITORY"):
            patch.delenv(name, raising=False)
        patch.setenv("SEMINARS_HTTP_CACHE_DIR", str(root.parent / "http-cache"))
        patch.chdir(root)
        result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == 0, result.output
    assert "sources: 3 ok, 0 empty, 0 failed" in result.output
    return root


def stored(root: Path) -> list[Event]:
    return read_events(root / "data" / "events.json")


def report(root: Path) -> dict[str, Any]:
    return json.loads((root / "data" / "run_report.json").read_text(encoding="utf-8"))


def windows(root: Path) -> tuple[TimeWindow, TimeWindow]:
    """The windows of talks and of meetings of the run in ``root``, at its recordings."""
    settings = load_settings(REPO_ROOT / "config" / "settings.yaml")
    now = reference_time(root / "tests" / "fixtures")
    assert now is not None
    return settings.window_at(now), settings.meeting_window_at(now)


def test_the_three_sources_have_the_meeting_horizon() -> None:
    sources = load_sources(REPO_ROOT / "config" / "sources.d")
    flagged = {source.id for source in sources if source.meeting_horizon}
    assert set(SOURCES) <= flagged
    assert "sites-icme" not in flagged


@pytest.mark.parametrize(("title", "expected"), MEETINGS.items())
def test_a_meeting_after_the_window_of_talks_is_stored(
    offline_run: Path, title: str, expected: tuple[str, str, bool, datetime, datetime]
) -> None:
    source, event_type, all_day, start, end = expected
    talks, _ = windows(offline_run)
    assert start >= talks.end, f"{title} is in the window of talks"
    (event,) = [event for event in stored(offline_run) if event.title == title]
    assert {key.split(":")[0] for key in event.source_ids} == {source}
    assert (event.event_type, event.all_day, event.start, event.end) == (
        event_type,
        all_day,
        start,
        end,
    )
    assert event.is_meeting
    assert event.fields


def test_after_the_window_of_talks_only_meetings_are_stored(offline_run: Path) -> None:
    talks, meetings = windows(offline_run)
    later = [event for event in stored(offline_run) if event.start >= talks.end]
    assert {event.title for event in later} == set(MEETINGS)
    assert all(event.is_meeting and event.start < meetings.end for event in later)


def test_the_all_hands_meeting_is_excluded_and_no_page_is_skipped(offline_run: Path) -> None:
    run = report(offline_run)
    assert [(item["title"], item["reason"]) for item in run["excluded"]] == [
        ("Q-NEXT All Hands 2027", "administrative")
    ]
    assert {item["source_id"]: item["skipped_pages"] for item in run["sources"]} == dict.fromkeys(
        SOURCES, 0
    )
