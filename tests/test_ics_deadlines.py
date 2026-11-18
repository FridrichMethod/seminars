"""The feed of the deadlines, deadlines.ics, and its validation.

``seminars.outputs.ics.write_ics_feeds`` writes it after the ten feeds of FEED_IDS, with
the VEVENT of each deadline from ``seminars.outputs.deadlines``. The golden file
tests/golden/deadlines.ics holds the feed with LF line endings; run the tests with
SEMINARS_UPDATE_GOLDEN=1 to write it again, then review the diff.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import icalendar
import pytest

from deadline_samples import SCHOOL_URL, make_deadline
from factories import make_event
from seminars.config import AppConfig
from seminars.i18n import Locale
from seminars.models import DEADLINE_SHOWN, FEED_IDS, Deadline
from seminars.outputs.deadlines import DEADLINES_FILE, deadline_vevent, stated, summary
from seminars.outputs.ics import validate_ics, write_ics_feeds
from seminars.textio import read_text, write_text_atomic
from test_ics import UID_DOMAIN, assert_matches_golden, content_lines, timezone_block

CHANGED = datetime(2026, 11, 12, 18, 30, tzinfo=UTC)

SHOWN_CHANGES: dict[str, Any] = {
    "edition_title": "Example Symposium 2027: Design of Proteins",
    "edition_short": "Symposium",
    "edition_start": date(2027, 3, 28),
    "edition_end": date(2027, 4, 2),
    "location": "Example Center, Oakland, CA",
    "url": "https://symposium.example.org/2027/program",
    "label": "Scholarship application deadline",
    "date": date(2026, 11, 21),
    "time": "17:00",
    "zone": "PT",
    "qualifier": "by",
    "verified_on": date(2026, 11, 16),
    "verified_url": "https://symposium.example.org/2027/key-dates",
}
"""Another value of each fact of DEADLINE_SHOWN for ``make_deadline()``."""


def golden_deadlines() -> list[Deadline]:
    """One deadline of each form a page states: a zone, a time, by a day, by a time, none."""
    return [
        make_deadline(),
        make_deadline(
            uid="dl.example-symposium-2027.abstract",
            kind="abstract",
            label="Short talk abstract deadline",
            date=date(2026, 12, 3),
            time="23:59",
            zone="MT",
        ),
        make_deadline(
            uid="dl.example-symposium-2027.registration_open",
            kind="registration_open",
            label="Registration opens",
            date=date(2027, 2, 1),
            zone=None,
            qualifier="by",
        ),
        make_deadline(
            uid="dl.example-symposium-2027.abstract-2",
            kind="abstract",
            label="Poster abstract deadline",
            date=date(2027, 3, 8),
            zone=None,
            last_changed=CHANGED,
            sequence=2,
        ),
        make_deadline(
            uid="dl.example-school-2027.application",
            edition_id="example-school-2027",
            edition_title="Example Summer School 2027",
            edition_short="Example School",
            series="Example Summer School",
            region="world",
            edition_start=date(2027, 7, 5),
            edition_end=date(2027, 7, 5),
            location="Example Institute, Basel, Switzerland",
            url=SCHOOL_URL,
            fields=["math"],
            kind="application",
            label="Application deadline",
            date=date(2027, 1, 15),
            time="12:00",
            zone="Europe/Zurich",
            qualifier="by",
            verified_on=date(2026, 11, 2),
            verified_url=SCHOOL_URL,
        ),
    ]


def feed(config: AppConfig, out_dir: Path, deadlines: list[Deadline], **kwargs: Any) -> str:
    paths = write_ics_feeds(kwargs.pop("events", []), out_dir, config=config, deadlines=deadlines)
    assert paths[-1] == out_dir / DEADLINES_FILE
    return read_text(paths[-1])


def vevents(text: str) -> dict[str, Any]:
    calendar = icalendar.Calendar.from_ical(text)
    assert isinstance(calendar, icalendar.Calendar)
    return {str(part["uid"]).split("@")[0]: part for part in calendar.walk("VEVENT")}


@pytest.fixture
def english(app_config: AppConfig) -> Locale:
    return Locale.load("en", app_config.paths.locales_dir)


class TestFile:
    def test_it_comes_after_the_ten_feeds_and_is_no_feed_id(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        paths = write_ics_feeds([], tmp_path, config=app_config, deadlines=golden_deadlines())
        assert [path.name for path in paths] == [f"{feed}.ics" for feed in FEED_IDS] + [
            "deadlines.ics"
        ]
        assert "deadlines" not in FEED_IDS

    def test_it_matches_its_golden_file(self, app_config: AppConfig, tmp_path: Path) -> None:
        text = feed(app_config, tmp_path, golden_deadlines())
        assert text.isascii()
        assert text.endswith("END:VCALENDAR\r\n")
        assert not re.search(r"[\r\n]", text.replace("\r\n", ""))
        assert validate_ics(text, deadlines=True) == []
        assert_matches_golden("deadlines.ics", text.replace("\r\n", "\n"))

    def test_the_bytes_depend_on_the_deadlines_alone(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        first = feed(app_config, tmp_path / "a", golden_deadlines())
        shuffled = list(reversed(golden_deadlines()))
        events = [make_event(uid="talk-a"), make_event(uid="talk-b", all_day=True)]
        second = feed(app_config, tmp_path / "b", shuffled, events=events)
        assert second == first

    def test_without_deadlines_the_feed_is_valid_and_has_its_header(
        self, app_config: AppConfig, tmp_path: Path, english: Locale
    ) -> None:
        text = feed(app_config, tmp_path, [])
        lines = content_lines(text)
        assert f"X-WR-CALNAME:{english.t('deadlines.feed.name')}" in lines
        assert "DTSTART:19700101T000000" in timezone_block(text)
        assert "BEGIN:VEVENT" not in lines
        assert validate_ics(text, deadlines=True) == []

    def test_a_uid_twice_writes_nothing(self, app_config: AppConfig, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="duplicate deadline uid"):
            write_ics_feeds(
                [make_event()],
                tmp_path / "feeds",
                config=app_config,
                deadlines=[make_deadline(), make_deadline(label="Copy")],
            )
        assert not (tmp_path / "feeds").exists()


class TestHeader:
    def test_the_header_of_the_other_feeds_with_the_name_of_the_deadlines(
        self, app_config: AppConfig, tmp_path: Path, english: Locale
    ) -> None:
        lines = content_lines(feed(app_config, tmp_path, golden_deadlines()))
        assert lines[:5] == [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            f"PRODID:-//{UID_DOMAIN}//seminars//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
        ]
        description = english.t("deadlines.feed.description").replace(",", "\\,")
        assert "X-WR-CALNAME:Seminars - Deadlines" in lines
        assert f"X-WR-CALDESC:{description}" in lines
        assert "REFRESH-INTERVAL;VALUE=DURATION:PT6H" in lines
        assert not [line for line in lines if "VALARM" in line or line.startswith("LOCATION")]

    def test_the_time_zone_covers_the_years_of_the_deadlines(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        block = timezone_block(feed(app_config, tmp_path, golden_deadlines()))
        onsets = [line for line in block if line.startswith("DTSTART:")]
        assert onsets[0] == "DTSTART:20260101T000000"
        assert onsets[-1].startswith("DTSTART:2027")


class TestEvent:
    def test_one_all_day_event_of_free_time_per_deadline(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        parts = vevents(feed(app_config, tmp_path, golden_deadlines()))
        assert list(parts) == [
            "dl.example-symposium-2027.scholarship",
            "dl.example-symposium-2027.abstract",
            "dl.example-school-2027.application",
            "dl.example-symposium-2027.registration_open",
            "dl.example-symposium-2027.abstract-2",
        ]
        scholarship = parts["dl.example-symposium-2027.scholarship"]
        assert str(scholarship["uid"]) == f"dl.example-symposium-2027.scholarship@{UID_DOMAIN}"
        assert scholarship.decoded("dtstart") == date(2026, 11, 20)
        assert scholarship.decoded("dtend") == date(2026, 11, 21)
        assert str(scholarship["url"]) == "https://symposium.example.org/2027/"
        assert int(scholarship["sequence"]) == 0
        lines = scholarship.to_ical().decode().replace("\r\n ", "").split("\r\n")
        assert "DTSTART;VALUE=DATE:20261120" in lines
        assert "DTEND;VALUE=DATE:20261121" in lines
        assert "DTSTAMP:20261110T200000Z" in lines
        assert str(scholarship["transp"]) == "TRANSPARENT"
        assert str(scholarship["x-microsoft-cdo-busystatus"]) == "FREE"
        assert not scholarship.subcomponents

    def test_the_stamps_are_the_last_change(self, app_config: AppConfig, tmp_path: Path) -> None:
        part = vevents(feed(app_config, tmp_path, golden_deadlines()))[
            "dl.example-symposium-2027.abstract-2"
        ]
        assert part.decoded("dtstamp") == CHANGED
        assert part.decoded("last-modified") == CHANGED
        assert int(part["sequence"]) == 2

    @pytest.mark.parametrize(
        ("uid", "expected"),
        [
            ("dl.example-symposium-2027.scholarship", "Scholarship deadline (AoE)"),
            ("dl.example-symposium-2027.abstract", "Short talk abstract deadline (23:59 MT)"),
            ("dl.example-symposium-2027.registration_open", "Registration opens (by this day)"),
            ("dl.example-symposium-2027.abstract-2", "Poster abstract deadline"),
        ],
    )
    def test_the_summary_says_what_the_page_states_about_the_hour(
        self, uid: str, expected: str, app_config: AppConfig, tmp_path: Path
    ) -> None:
        part = vevents(feed(app_config, tmp_path, golden_deadlines()))[uid]
        assert str(part["summary"]) == f"Example Symposium 2027: {expected}"

    def test_by_a_time_and_by_a_day_in_a_zone(self, english: Locale) -> None:
        by_time = make_deadline(time="12:00", zone="Europe/Zurich", qualifier="by")
        assert stated(by_time, english) == "by 12:00 Europe/Zurich"
        assert stated(make_deadline(qualifier="by"), english) == "by this day AoE"
        assert stated(make_deadline(zone=None, time="09:30"), english) == "09:30"
        assert summary(make_deadline(zone=None), english) == (
            "Example Symposium 2027: Scholarship deadline"
        )

    def test_the_description_names_the_edition_the_day_the_advice_and_the_check(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        parts = vevents(feed(app_config, tmp_path, golden_deadlines()))
        symposium = str(parts["dl.example-symposium-2027.registration_open"]["description"])
        assert symposium.split("\n") == [
            "Example Symposium 2027: Design of Molecules",
            "2027-03-29 to 2027-04-01, Example Hall, San Francisco, CA",
            "",
            "Registration opens: by 2027-02-01",
            "",
            "Check the organizer's page before you rely on it: "
            "https://symposium.example.org/2027/dates/",
            "Verified 2026-11-09",
        ]
        school = str(parts["dl.example-school-2027.application"]["description"])
        assert school.split("\n")[1] == "2027-07-05, Example Institute, Basel, Switzerland"
        assert school.split("\n")[3] == "Application deadline: by 2027-01-15 12:00 Europe/Zurich"

    def test_the_event_shows_the_shown_facts_and_no_other(self, english: Locale) -> None:
        """A change of a fact of DEADLINE_SHOWN changes the bytes, any other change does not."""

        def text(deadline: Deadline) -> bytes:
            return deadline_vevent(deadline, uid_domain=UID_DOMAIN, locale=english).to_ical()

        base = make_deadline()
        assert set(SHOWN_CHANGES) == DEADLINE_SHOWN
        assert [name for name, value in SHOWN_CHANGES.items() if getattr(base, name) == value] == []
        changed = [
            name
            for name, value in SHOWN_CHANGES.items()
            if text(base.evolve(**{name: value})) == text(base)
        ]
        assert changed == []
        assert text(base.evolve(series="Another Series", region="world", fields=["math"])) == (
            text(base)
        )

    def test_no_text_is_hardcoded(self, app_config: AppConfig, tmp_path: Path) -> None:
        path = app_config.paths.locales_dir / "en.json"
        tree = json.loads(read_text(path))
        tree["deadlines"]["feed"]["name"] = "Fristen; Kalender"
        tree["deadlines"]["when"] = {"by": "until {when}", "by_day": "until today"}
        tree["deadlines"]["event"].update(check="See {url}", verified="Checked {date}")
        write_text_atomic(path, json.dumps(tree))
        text = feed(app_config, tmp_path, golden_deadlines())
        assert "X-WR-CALNAME:Fristen\\; Kalender" in content_lines(text)
        part = vevents(text)["dl.example-symposium-2027.registration_open"]
        assert str(part["summary"]).endswith("(until today)")
        lines = str(part["description"]).split("\n")
        assert lines[3:] == [
            "Registration opens: until 2027-02-01",
            "",
            "See https://symposium.example.org/2027/dates/",
            "Checked 2026-11-09",
        ]


class TestValidation:
    @pytest.fixture
    def text(self, app_config: AppConfig, tmp_path: Path) -> str:
        return feed(app_config, tmp_path, golden_deadlines())

    def test_the_feed_passes_both_validations(self, text: str) -> None:
        assert validate_ics(text) == []
        assert validate_ics(text, deadlines=True) == []

    def test_an_event_feed_is_no_feed_of_deadlines(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        paths = write_ics_feeds([make_event()], tmp_path, config=app_config)
        problems = validate_ics(read_text(paths[0]), deadlines=True)
        assert any("UID is not the uid of a deadline" in problem for problem in problems)
        assert any("DTSTART of a deadline is not a date" in problem for problem in problems)
        assert any("VEVENT has no TRANSP" in problem for problem in problems)

    @pytest.mark.parametrize(
        ("old", "new", "problem"),
        [
            ("TRANSP:TRANSPARENT", "TRANSP:OPAQUE", "TRANSP of a deadline is not TRANSPARENT"),
            (
                "X-MICROSOFT-CDO-BUSYSTATUS:FREE",
                "X-MICROSOFT-CDO-BUSYSTATUS:BUSY",
                "X-MICROSOFT-CDO-BUSYSTATUS of a deadline is not FREE",
            ),
            (
                "DTEND;VALUE=DATE:20261121",
                "DTEND;VALUE=DATE:20261122",
                "DTEND of a deadline is not the day after DTSTART",
            ),
            (
                "DTSTART;VALUE=DATE:20261120",
                "DTSTART:20261120T090000Z",
                "DTSTART of a deadline is not a date",
            ),
            ("UID:dl.example-symposium-2027.scholarship@", "UID:talk-1@", "not the uid of"),
            ("X-WR-CALNAME:Seminars - Deadlines\r\n", "", "VCALENDAR has no X-WR-CALNAME"),
            ("URL:https://symposium.example.org/2027/\r\n", "", "VEVENT has no URL"),
        ],
    )
    def test_a_broken_rule_is_named(self, text: str, old: str, new: str, problem: str) -> None:
        assert old in text
        broken = text.replace(old, new, 1)
        assert validate_ics(broken) == [] or problem.startswith("DTSTART")
        assert any(problem in found for found in validate_ics(broken, deadlines=True))

    def test_an_alarm_or_another_component_is_named(self, text: str) -> None:
        alarm = "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT1H\r\nEND:VALARM\r\nEND:VEVENT"
        broken = text.replace("END:VEVENT", alarm, 1)
        assert any(
            "VALARM inside a deadline" in found for found in validate_ics(broken, deadlines=True)
        )
        todo = "BEGIN:VTODO\r\nUID:x@y\r\nDTSTAMP:20261110T200000Z\r\nEND:VTODO\r\nEND:VCALENDAR"
        other = text.replace("END:VCALENDAR", todo)
        found = validate_ics(other, deadlines=True)
        assert any("VTODO is not part of the feed of deadlines" in problem for problem in found)
