"""An offline run of the whole pipeline over every recorded fixture of the repository.

Nothing is faked here: the configured sources, the real adapters, normalization,
classification and the output writers run over tests/fixtures, as
``seminars run --offline --no-llm`` does. The run happens at the latest recording
time of all fixtures, 2026-11-17T01:26:52Z (17:26 on 16 November in Stanford), so
the talks of the afternoon of 11 November have already taken place.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from conftest import REPO_ROOT
from seminars import cli
from seminars.config import PROVIDER_CREDENTIAL_ENV, load_settings, load_sources
from seminars.fixtures import reference_time
from seminars.models import FIELD_IDS, Event, read_events

RUN_TIME = datetime(2026, 11, 18, 9, 58, 33, tzinfo=UTC)
"""The latest recording time of the fixtures: the "now" of an offline run."""

PAST_AFTERNOON = {
    "Physical Chemistry Seminar: Professor Connor Coley, MIT": "sites-chemistry",
    "Viehweg-Zuo I": "sites-mathematics",
}
"""Talks of the afternoon of 11 November (Pacific time), before the run time."""


def make_project(root: Path) -> Path:
    """A project at ``root`` with the configuration and the fixtures of the repository."""
    (root / "tests").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "config", root / "config")
    shutil.copytree(REPO_ROOT / "locales", root / "locales")
    (root / "tests" / "fixtures").symlink_to(REPO_ROOT / "tests" / "fixtures")
    return root


def run_offline(root: Path) -> str:
    """``seminars run --offline --no-llm`` in the project ``root``; its output."""
    with pytest.MonkeyPatch.context() as patch:
        for name in (*filter(None, PROVIDER_CREDENTIAL_ENV.values()), "GITHUB_TOKEN"):
            patch.delenv(name, raising=False)
        for name in ("LLM_PROVIDERS", "GITHUB_ACTIONS", "GITHUB_REPOSITORY"):
            patch.delenv(name, raising=False)
        patch.setenv("SEMINARS_HTTP_CACHE_DIR", str(root.parent / "http-cache"))
        patch.chdir(root)
        result = CliRunner().invoke(cli.app, ["run", "--offline", "--no-llm"])
    assert result.exit_code == 0, result.output
    return result.output


@pytest.fixture(scope="module")
def offline_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path]:
    """The output of one offline run and the project it wrote into."""
    root = make_project(tmp_path_factory.mktemp("offline") / "project")
    return run_offline(root), root


def test_a_second_run_over_the_same_recordings_changes_nothing(tmp_path: Path) -> None:
    # Merged talks keep the values of the same listing once they share their uid.
    root = make_project(tmp_path / "project")
    run_offline(root)
    events = (root / "data" / "events.json").read_bytes()
    output = run_offline(root)
    assert ", 0 new, 0 changed, 0 missing," in output
    assert (root / "data" / "events.json").read_bytes() == events


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
    settings = load_settings(REPO_ROOT / "config" / "settings.yaml")
    window, meetings = settings.window_at(RUN_TIME), settings.meeting_window_at(RUN_TIME)
    flagged = {s.id for s in load_sources(REPO_ROOT / "config" / "sources.d") if s.meeting_horizon}
    events = stored(root)
    assert len(events) > 100
    for event in events:
        if window.contains(event.start):
            continue
        # Only a meeting of a source with meeting_horizon starts after the window.
        assert event.is_meeting, event.title
        assert meetings.contains(event.start), event.title
        assert {key.split(":")[0] for key in event.source_ids} & flagged, event.title


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


@pytest.mark.parametrize(
    ("speaker", "title"),
    [
        ("Stefan Feske", "Ion channels and transporters regulating lymphocyte function"),
        ("Cecilia Leal", "Non-Lamellar Lipid Membranes as Regulators of Biological Function"),
    ],
)
def test_a_talk_the_campus_calendar_wraps_keeps_the_title_of_the_department(
    offline_run: tuple[str, Path], speaker: str, title: str
) -> None:
    # The calendar lists 'Biology Seminar Series - <speaker> "<title> "'; Biology lists <title>.
    _, root = offline_run
    (event,) = [
        event
        for event in stored(root)
        if [person.name for person in event.speakers] == [speaker]
        and event.start.year == RUN_TIME.year
    ]
    assert event.title == title
    assert {key.split(":")[0] for key in event.source_ids} == {"localist-stanford", "sites-biology"}


def test_a_talk_two_hosts_announce_keeps_the_title_of_the_talk(
    offline_run: tuple[str, Path],
) -> None:
    # Chemical and Systems Biology announces it by its speaker (on its page and in its
    # calendar), ChEM-H by its title.
    _, root = offline_run
    (event,) = [
        event
        for event in stored(root)
        if [speaker.name for speaker in event.speakers] == ["David Garfield"]
    ]
    assert event.title.startswith("Science with Claude: ")
    assert "ai-ml" in event.fields
    assert {key.split(":")[0] for key in event.source_ids} == {
        "custom-csb-calendar",
        "custom-csb-events",
        "sites-chemh",
    }


def test_the_sessions_of_a_schedule_name_one_speaker_per_label(
    offline_run: tuple[str, Path],
) -> None:
    # ICME's schedule: "Speaker 1 (1:30 PM - 2:05 PM) - Alison Marsden, Professor, ...".
    _, root = offline_run
    (event,) = [event for event in stored(root) if event.title == "Seminar Session 8"]
    assert [(person.name, person.affiliation) for person in event.speakers] == [
        ("Alison Marsden", "Professor, Pediatrics - Cardiology"),
        ("Emily Fox", "Professor, Statistics and Computer Science"),
    ]
    # Session 7's paragraphs run together: "..., GeophysicsSpeaker 2 (2:10 PM ...".
    (event,) = [event for event in stored(root) if event.title == "Seminar Session 7"]
    assert [(person.name, person.affiliation) for person in event.speakers] == [
        ("Ching-Yao Lai", "Assistant Professor, Geophysics"),
        ("Simona Onori", "Associate Professor, Energy Science and Engineering"),
    ]


def test_a_calendar_title_that_names_the_speaker_joins_the_host_listing(
    offline_run: tuple[str, Path],
) -> None:
    # "MRC Public Lecture: Tadashi Tokieda" on the calendar and Mathematics, without a
    # speaker; the Mathematics Research Center lists the talk with its speaker.
    _, root = offline_run
    (event,) = [
        event
        for event in stored(root)
        if any(key.startswith("sites-mrc:") for key in event.source_ids)
        and "Tokieda" in event.title
    ]
    assert event.title == "Tadashi Tokieda: Surprises from rubbing the wrong way"
    assert {key.split(":")[0] for key in event.source_ids} == {
        "localist-stanford",
        "sites-mathematics",
        "sites-mrc",
    }


# Speakers read from the titles of the campus calendar, whose API names no speaker.

TITLE_SPEAKERS = {
    "localist-stanford.54045893273780": [("Shang-Ping Xie", None)],
    "localist-stanford.54251449027978": [("Ami Bhatt", None)],
    "localist-stanford.53552415025391": [("Jonathan Fan", "Stanford University")],
    "localist-stanford.54251566724146": [("Bryant Lin", None), ("Latha Palaniappan", None)],
    "localist-stanford.54410800155205": [("Danielle Klinger", None)],
    "localist-stanford.53728348826147": [],  # "Norton Lecture"
}
"""Talks that only the campus calendar lists, by uid, with the people of their titles."""


@pytest.mark.parametrize(("uid", "people"), TITLE_SPEAKERS.items())
def test_a_calendar_talk_gets_the_speakers_its_title_names(
    offline_run: tuple[str, Path], uid: str, people: list[tuple[str, str | None]]
) -> None:
    _, root = offline_run
    (event,) = [event for event in stored(root) if event.uid == uid]
    assert {key.split(":")[0] for key in event.source_ids} == {"localist-stanford"}
    assert [(person.name, person.affiliation) for person in event.speakers] == people


def test_no_series_name_is_stored_as_a_speaker(offline_run: tuple[str, Path]) -> None:
    _, root = offline_run
    names = [person.name for event in stored(root) for person in event.speakers]
    series = ("Seminar", "Seminars", "Lecture", "Series", "Rounds", "Defense", "Colloquium")
    assert names
    assert [name for name in names if name.split()[-1] in series] == []


def test_no_timed_talk_ends_at_its_own_time_on_a_later_day(
    offline_run: tuple[str, Path],
) -> None:
    """A guard over the whole offline run, not a regression test.

    No recorded page holds a timed instance whose end is at the same wall
    time on a later day, so this holds without the rule as well. The rule
    is tested in test_pipeline_times.py (TestLength) and in
    test_pipeline_normalize.py
    (test_an_end_at_the_same_time_on_the_next_day_is_not_kept).
    """
    _, root = offline_run
    timed = [event for event in stored(root) if not event.all_day and event.end is not None]
    assert timed
    assert [
        event.uid
        for event in timed
        if event.end is not None
        and event.end.date() > event.start.date()
        and event.end.time() == event.start.time()
    ] == []


def test_no_source_skips_a_page_it_asks_for(offline_run: tuple[str, Path]) -> None:
    # Every detail page an adapter fetches in the window is recorded, so no source is
    # read only in part (the Sieber post of Chemical and Systems Biology included).
    _, root = offline_run
    report = json.loads((root / "data" / "run_report.json").read_text(encoding="utf-8"))
    assert report["sources"]
    assert {item["source_id"]: item["skipped_pages"] for item in report["sources"]} == {
        item["source_id"]: 0 for item in report["sources"]
    }


STANFORD = ZoneInfo("America/Los_Angeles")

COMING_TALKS = [
    (
        "sites-aimi",
        datetime(2026, 12, 15, 8, 0, tzinfo=STANFORD),
        "Genetic Diagnosis and Discovery Enabled by Large Language Models",
        ["Konstantina Stankovic"],
        {"ai-ml", "med"},
    ),
    (
        "sites-mse",
        datetime(2026, 11, 18, 11, 30, tzinfo=STANFORD),
        "MSE Colloquium: Robert Hovden",
        ["Robert Hovden"],
        {"chem", "physics"},
    ),
    (
        "sites-systemx",
        datetime(2026, 12, 3, 9, 0, tzinfo=STANFORD),
        "SystemX Physical Intelligence Summit",
        [],
        {"cs"},
    ),
    (
        "custom-dbds-workshop",
        datetime(2026, 11, 19, 13, 30, tzinfo=STANFORD),
        "TBA",  # "Coming soon" on the page
        ["Marinka Zitnik"],
        {"ai-ml", "math", "med"},
    ),
    (
        "bayarea-slac-theory",
        datetime(2026, 11, 18, 12, 30, tzinfo=STANFORD),
        "TBA",  # the Indico feed names the speaker only
        ["Alessandro Lenoci"],
        {"physics"},
    ),
    (
        "bayarea-slac-experimental",
        datetime(2026, 11, 24, 12, 30, tzinfo=STANFORD),
        "TBA",  # neither title nor speaker yet
        [],
        {"physics"},
    ),
    (
        "custom-csb-calendar",
        datetime(2027, 2, 8, 12, 30, tzinfo=STANFORD),
        "CSB Special Seminar: Jeanne Hardy, PhD",
        ["Jeanne Hardy"],
        {"bio", "chem"},
    ),
]
"""A talk after the run time of each series that only its own source lists."""


@pytest.mark.parametrize(("source", "start", "title", "speakers", "fields"), COMING_TALKS)
def test_each_added_series_lists_a_talk_after_the_run(
    offline_run: tuple[str, Path],
    source: str,
    start: datetime,
    title: str,
    speakers: list[str],
    fields: set[str],
) -> None:
    _, root = offline_run
    (event,) = [
        event
        for event in stored(root)
        if event.start == start and any(key.startswith(f"{source}:") for key in event.source_ids)
    ]
    assert event.start > RUN_TIME
    assert event.start.utcoffset() == timedelta(hours=-8)
    assert event.title == title
    assert [person.name for person in event.speakers] == speakers
    assert fields <= set(event.fields)
    assert {key.split(":")[0] for key in event.source_ids} == {source}
