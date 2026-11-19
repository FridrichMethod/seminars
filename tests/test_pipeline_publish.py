"""Tests for assembling public/ and for validating data/ and public/."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import icalendar

from deadline_samples import make_deadline
from factories import REFERENCE_NOW, make_event
from fakes import FakeSecretScanner
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig, ProjectPaths
from seminars.models import FEED_IDS, dump_deadlines, dump_event_lines, dump_events
from seminars.outputs.ics import validate_ics, write_ics_feeds
from seminars.pipeline.publish import assemble_site, validate_project

NEEDLE = "needle-value"


def validate(paths: ProjectPaths, emitters: FakeEmitters | None = None) -> tuple[str, ...]:
    validator = (emitters or FakeEmitters()).validate_ics
    return validate_project(paths, validator=validator, scanner=FakeSecretScanner()).problems


class TestAssemble:
    def test_links_in_the_site_are_not_followed(self, app_config: AppConfig) -> None:
        paths = app_config.paths
        paths.site_dir.mkdir()
        (paths.site_dir / "index.html").write_text("<p></p>\n", encoding="utf-8")
        secret = paths.root / "outside.txt"
        secret.write_text("private\n", encoding="utf-8")
        (paths.site_dir / "link.txt").symlink_to(secret)
        (paths.site_dir / "nested").mkdir()
        emitters = FakeEmitters()
        built = assemble_site(
            app_config,
            write_ics_feeds=emitters.write_ics_feeds,
            write_site_data=emitters.write_site_data,
            fallback_time=REFERENCE_NOW,
        )
        assert "index.html" in built.files
        assert "link.txt" not in built.files
        assert not (paths.public_dir / "link.txt").exists()
        assert built.missing == ()
        feeds = [name for name in built.files if name.startswith("feeds/")]
        assert sorted(feeds) == sorted(f"feeds/{feed}.ics" for feed in (*FEED_IDS, "deadlines"))

    def test_the_stored_deadlines_are_written_into_their_feed(self, app_config: AppConfig) -> None:
        paths = app_config.paths
        abstract = make_deadline(
            uid="dl.example-symposium-2027.abstract",
            kind="abstract",
            label="Short talk abstract deadline",
            date=date(2026, 12, 3),
        )
        deadlines = [make_deadline(), abstract]
        paths.data_dir.mkdir()
        paths.deadlines_file.write_text(dump_deadlines(deadlines), encoding="utf-8")
        built = assemble_site(
            app_config,
            write_ics_feeds=write_ics_feeds,
            write_site_data=FakeEmitters().write_site_data,
            fallback_time=REFERENCE_NOW,
        )
        assert "feeds/deadlines.ics" in built.files
        feed = icalendar.Calendar.from_ical((paths.public_feeds_dir / "deadlines.ics").read_bytes())
        uids = [str(event["UID"]).partition("@")[0] for event in feed.walk("VEVENT")]
        assert sorted(uids) == sorted(deadline.uid for deadline in deadlines)


def feed_problems(problems: tuple[str, ...]) -> list[str]:
    return [problem for problem in problems if problem.startswith("public/feeds/")]


def other_problems(problems: tuple[str, ...]) -> list[str]:
    return [problem for problem in problems if not problem.startswith("public/feeds/")]


class TestValidate:
    def test_an_empty_project_has_nothing_to_publish(self, tmp_path: Path) -> None:
        problems = validate(ProjectPaths(tmp_path))
        assert other_problems(problems) == [
            "data/events.json: missing (made by seminars run)",
            "public/events.json: missing (made by seminars build-site)",
            "public/status.json: missing (made by seminars build-site)",
        ]
        assert feed_problems(problems) == [
            f"public/feeds/{feed}.ics: missing" for feed in (*FEED_IDS, "deadlines")
        ]

    def test_damaged_files_are_named_without_their_content(self, tmp_path: Path) -> None:
        paths = ProjectPaths(tmp_path)
        paths.data_dir.mkdir()
        paths.events_file.write_text(dump_events([make_event()]), encoding="utf-8")
        paths.health_file.write_text('{"schema_version": 7}\n', encoding="utf-8")
        paths.archive_dir.mkdir()
        paths.archive_file(2025).write_text("{broken\n", encoding="utf-8")
        good = dump_event_lines([make_event()])
        paths.archive_file(2026).write_text(good, encoding="utf-8")
        paths.public_dir.mkdir()
        paths.public_events_file.write_text(f'{{"x": "{NEEDLE}"}}\n', encoding="utf-8")
        paths.public_status_file.write_text("not json at all\n", encoding="utf-8")
        problems = other_problems(validate(paths))
        assert [problem.split(": ")[0] for problem in problems] == [
            str(paths.health_file),
            "data/archive/2025.jsonl",
            "public/events.json",
            "public/status.json",
        ]
        assert problems[0].endswith("cannot be read (1 value(s) do not fit the schema)")
        assert problems[1].endswith(": ValidationError")
        assert all(problem.endswith("do not fit the schema") for problem in problems[2:])
        assert NEEDLE not in " ".join(problems)

    def test_missing_feeds_are_problems(self, tmp_path: Path) -> None:
        paths = ProjectPaths(tmp_path)
        paths.public_feeds_dir.mkdir(parents=True)
        (paths.public_feeds_dir / "all.ics").write_text("BEGIN:VCALENDAR\n", encoding="utf-8")
        problems = feed_problems(validate(paths))
        assert len(problems) == len(FEED_IDS)  # nine feed ids and the feed of the deadlines
        assert "public/feeds/picks.ics: missing" in problems
        assert "public/feeds/deadlines.ics: missing" in problems

    def test_only_the_feed_of_the_deadlines_is_held_to_the_rules_of_deadlines(
        self, app_config: AppConfig
    ) -> None:
        paths = app_config.paths
        # A talk in the event feeds: an event breaks the rules of deadlines, so the feeds
        # pass only when those rules are kept to the feed of the deadlines.
        feeds = paths.public_feeds_dir
        write_ics_feeds([make_event()], feeds, config=app_config, deadlines=[make_deadline()])

        def problems() -> list[str]:
            checked = validate_project(paths, validator=validate_ics, scanner=FakeSecretScanner())
            return feed_problems(checked.problems)

        assert problems() == []
        feed = feeds / "deadlines.ics"
        text = feed.read_bytes()  # as bytes: the lines end in CRLF
        assert text.count(b"\r\nTRANSP:TRANSPARENT\r\n") == 1
        feed.write_bytes(text.replace(b"TRANSP:TRANSPARENT", b"TRANSP:OPAQUE"))
        (problem,) = problems()
        assert problem.startswith("public/feeds/deadlines.ics: line ")
        assert problem.endswith(": TRANSP of a deadline is not TRANSPARENT")

    def test_the_files_that_were_looked_at_are_listed(self, tmp_path: Path) -> None:
        paths = ProjectPaths(tmp_path)
        paths.data_dir.mkdir()
        paths.events_file.write_text('{"events": []}\n', encoding="utf-8")
        result = validate_project(
            paths, validator=FakeEmitters().validate_ics, scanner=FakeSecretScanner()
        )
        assert result.checked == ("data/events.json",)
