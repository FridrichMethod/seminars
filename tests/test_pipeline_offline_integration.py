"""An offline run of the whole pipeline over every recorded fixture of the repository.

Nothing is faked here: the configured sources, the real adapters, normalization,
classification and the output writers run over tests/fixtures, as
``seminars run --offline --no-llm`` does. The run happens at the latest recording
time of all fixtures, 2026-11-12T06:37:13Z (22:37 on 11 November in Stanford), so
the talks of the afternoon of 11 November have already taken place.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import REPO_ROOT
from seminars import cli
from seminars.config import PROVIDER_CREDENTIAL_ENV, load_settings
from seminars.fixtures import reference_time
from seminars.models import FIELD_IDS, Event, read_events

RUN_TIME = datetime(2026, 11, 12, 6, 37, 13, tzinfo=UTC)
"""The latest recording time of the fixtures: the "now" of an offline run."""

PAST_AFTERNOON = {
    "Physical Chemistry Seminar: Professor Connor Coley, MIT": "sites-chemistry",
    "Viehweg-Zuo I": "sites-mathematics",
}
"""Talks of the afternoon of 11 November (Pacific time), before the run time."""


@pytest.fixture(scope="module")
def offline_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path]:
    """The output of one offline run and the project it wrote into."""
    root = tmp_path_factory.mktemp("offline") / "project"
    (root / "tests").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config", root / "config")
    shutil.copytree(REPO_ROOT / "locales", root / "locales")
    (root / "tests" / "fixtures").symlink_to(REPO_ROOT / "tests" / "fixtures")
    with pytest.MonkeyPatch.context() as patch:
        for name in (*filter(None, PROVIDER_CREDENTIAL_ENV.values()), "GITHUB_TOKEN"):
            patch.delenv(name, raising=False)
        for name in ("LLM_PROVIDERS", "GITHUB_ACTIONS", "GITHUB_REPOSITORY"):
            patch.delenv(name, raising=False)
        patch.setenv("SEMINARS_HTTP_CACHE_DIR", str(root.parent / "http-cache"))
        patch.chdir(root)
        result = CliRunner().invoke(cli.app, ["run", "--offline", "--no-llm"])
    assert result.exit_code == 0, result.output
    return result.output, root


def stored(root: Path) -> list[Event]:
    return read_events(root / "data" / "events.json")


def test_the_run_time_is_the_latest_recording() -> None:
    assert reference_time(REPO_ROOT / "tests" / "fixtures") == RUN_TIME


def test_the_run_happens_at_that_time_and_no_source_fails(offline_run: tuple[str, Path]) -> None:
    output, _ = offline_run
    lines = output.splitlines()
    assert lines[0] == f"run: offline at {RUN_TIME.isoformat()}"
    assert ", 0 failed" in lines[1]
    assert "result: ok" in lines


def test_every_stored_talk_lies_in_the_window_of_the_run(offline_run: tuple[str, Path]) -> None:
    _, root = offline_run
    window = load_settings(REPO_ROOT / "config" / "settings.yaml").window_at(RUN_TIME)
    events = stored(root)
    assert len(events) > 100
    assert all(window.contains(event.start) for event in events)


def test_the_talks_of_the_afternoon_before_have_taken_place(
    offline_run: tuple[str, Path],
) -> None:
    _, root = offline_run
    by_title = {event.title: event for event in stored(root)}
    for title, source in PAST_AFTERNOON.items():
        event = by_title[title]
        assert event.start < RUN_TIME
        assert event.start.date().isoformat() == "2026-11-11"
        assert event.start.hour >= 12  # Pacific time: the afternoon
        assert any(key.startswith(f"{source}:") for key in event.source_ids)


def test_every_field_has_talks_in_the_four_weeks_after_the_run(
    offline_run: tuple[str, Path],
) -> None:
    _, root = offline_run
    coming = [
        event for event in stored(root) if RUN_TIME <= event.start < RUN_TIME + timedelta(days=28)
    ]
    for field in FIELD_IDS:
        assert any(field in event.fields for event in coming), field


def test_a_talk_two_hosts_announce_keeps_the_title_of_the_talk(
    offline_run: tuple[str, Path],
) -> None:
    # Chemical and Systems Biology announces it by its speaker, ChEM-H by its title.
    _, root = offline_run
    (event,) = [
        event
        for event in stored(root)
        if [speaker.name for speaker in event.speakers] == ["David Garfield"]
    ]
    assert event.title.startswith("Science with Claude: ")
    assert "ai-ml" in event.fields
    assert {key.split(":")[0] for key in event.source_ids} == {
        "custom-csb-events",
        "sites-chemh",
    }
