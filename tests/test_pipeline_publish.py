"""Tests for assembling public/ and for validating data/ and public/."""

from __future__ import annotations

from pathlib import Path

from factories import REFERENCE_NOW, make_event
from fakes import FakeSecretScanner
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig, ProjectPaths
from seminars.models import FEED_IDS, dump_event_lines
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
        assert len([name for name in built.files if name.startswith("feeds/")]) == len(FEED_IDS)


class TestValidate:
    def test_an_empty_project_is_valid(self, tmp_path: Path) -> None:
        assert validate(ProjectPaths(tmp_path)) == ()

    def test_damaged_files_are_named_without_their_content(self, tmp_path: Path) -> None:
        paths = ProjectPaths(tmp_path)
        paths.data_dir.mkdir()
        paths.health_file.write_text('{"schema_version": 7}\n', encoding="utf-8")
        paths.archive_dir.mkdir()
        paths.archive_file(2025).write_text("{broken\n", encoding="utf-8")
        good = dump_event_lines([make_event()])
        paths.archive_file(2026).write_text(good, encoding="utf-8")
        paths.public_dir.mkdir()
        paths.public_events_file.write_text(f'{{"x": "{NEEDLE}"}}\n', encoding="utf-8")
        paths.public_status_file.write_text("not json at all\n", encoding="utf-8")
        problems = validate(paths)
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
        problems = validate(paths)
        assert len(problems) == len(FEED_IDS) - 1
        assert "public/feeds/picks.ics: missing" in problems

    def test_the_files_that_were_looked_at_are_listed(self, tmp_path: Path) -> None:
        paths = ProjectPaths(tmp_path)
        paths.data_dir.mkdir()
        paths.events_file.write_text('{"events": []}\n', encoding="utf-8")
        result = validate_project(
            paths, validator=FakeEmitters().validate_ics, scanner=FakeSecretScanner()
        )
        assert result.checked == ("data/events.json",)
