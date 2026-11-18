"""Tests for the ICS feed writer and the feed validator.

The golden files in tests/golden/ hold feeds with LF line endings and ASCII
text only (the charset gate accepts nothing else there). A produced feed is
compared after its CRLF line endings were replaced, and the CRLF endings are
asserted on their own. Run the tests with SEMINARS_UPDATE_GOLDEN=1 to write
the golden files again, then review the diff.
"""

from __future__ import annotations

import dataclasses
import json
import os
import random
import re
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import icalendar
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from factories import PACIFIC, REFERENCE_NOW, event_lists, make_event
from seminars.config import AppConfig, Interests
from seminars.contracts import IcsFeedWriter, IcsValidator, load_implementation
from seminars.i18n import Locale
from seminars.models import FEED_IDS, FIELD_IDS, Event
from seminars.outputs.ics import validate_ics, write_ics_feeds
from seminars.textio import read_text, write_text_atomic
from seminars.textrules import decorative_characters

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
DETAIL_URL = "https://bioengineering.stanford.edu/events/protein-design-seminar"
LISTING_URL = "https://events.stanford.edu/event/protein-design"
UID_DOMAIN = "seminars.fridrichmethod.github.io"
LOCAL = "TZID=America/Los_Angeles:"

E_ACUTE = "\u00e9"
CJK = "\u86cb\u767d\u8d28\u8bbe\u8ba1"  # four CJK letters, three octets each
MATH_BOLD = "\U0001d400\U0001d401"  # letters outside the BMP, four octets each
ROCKET = "\U0001f680"
DIRTY = f"Launch{ROCKET}\u00a0day\u200b \ufb01ne \u2728"  # emoji, odd spaces, a ligature
HOSTILE = (
    "Commas, semicolons; back\\slashes and \\n\r\n"
    "END:VEVENT\r\nBEGIN:VEVENT\r\nUID:evil@example.org\n"
    "ATTENDEE:mailto:x@example.org\u2028X-A:1\x0bX-B:2\x85X-C:3\x00\x1b[31m\x7f"
)


def at(month: int, day: int, hour: int, minute: int = 0, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=PACIFIC)


def utc(month: int, day: int, hour: int, minute: int = 0, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def people(*names: str) -> list[dict[str, str]]:
    return [{"name": name} for name in names]


def write_feeds(events: list[Event], config: AppConfig, out_dir: Path) -> dict[str, str]:
    """Write the feeds and return ``{feed id: text}`` with the line endings as written."""
    paths = write_ics_feeds(events, out_dir, config=config)
    return {path.stem: read_text(path) for path in paths}


def vevents(text: str) -> list[Any]:
    calendar = icalendar.Calendar.from_ical(text)
    assert isinstance(calendar, icalendar.Calendar)
    return list(calendar.walk("VEVENT"))


def uids(text: str) -> list[str]:
    return [str(component["uid"]).split("@")[0] for component in vevents(text)]


def content_lines(text: str) -> list[str]:
    """The unfolded content lines of a feed."""
    return text.replace("\r\n ", "").split("\r\n")


def timezone_block(text: str) -> list[str]:
    lines = content_lines(text)
    return lines[lines.index("BEGIN:VTIMEZONE") : lines.index("END:VTIMEZONE") + 1]


def with_settings(config: AppConfig, **changes: Any) -> AppConfig:
    return dataclasses.replace(config, settings=config.settings.evolve(**changes))


def assert_matches_golden(name: str, text: str) -> None:
    path = GOLDEN_DIR / name
    if os.environ.get("SEMINARS_UPDATE_GOLDEN"):
        write_text_atomic(path, text)
    assert read_text(path) == text, f"{name} differs; see the module docstring"


@pytest.fixture
def one(app_config: AppConfig, tmp_path: Path) -> Any:
    """Write one event and return its VEVENT as parsed back from all.ics."""

    def build(event: Event, config: AppConfig = app_config) -> Any:
        (component,) = vevents(write_feeds([event], config, tmp_path / "feeds")["all"])
        return component

    return build


def golden_event(uid: str, title: str, start: datetime, **overrides: Any) -> Event:
    url = f"https://example.stanford.edu/events/{uid}"
    defaults = {"uid": uid, "title": title, "start": start, "url": url, "source_urls": [url]}
    defaults["end"] = start + timedelta(hours=1)
    return make_event(**{**defaults, **overrides})


def golden_events() -> list[Event]:
    """ASCII-only events that cover every branch of the writer."""
    return [
        make_event(
            uid="golden-pick",
            speakers=[{"name": "Jane Doe", "affiliation": "Stanford University"}, *people("Li Wu")],
            fields=["bio", "ai-ml"],
            picks_score=9.0,
            picks_reasons=["title: protein design", "title: diffusion model"],
            source_urls=[DETAIL_URL, LISTING_URL],
            sequence=2,
        ),
        golden_event(
            "golden-tba",
            "TBA",
            at(11, 18, 16, 30),
            speakers=[{"name": "Zoe Muller", "affiliation": "ETH Zurich"}],
            series="Physical Chemistry Seminar",
            host_org="Stanford Chemistry",
            end=None,
            location="Sapp Center Lecture Hall",
            abstract=None,
            fields=["chem"],
        ),
        golden_event(
            "golden-all-day",
            "Bay Area Symposium on Structural Biology",
            at(11, 20, 0),
            speakers=[],
            series=None,
            host_org="SLAC",
            event_type="conference",
            end=at(11, 22, 0),
            all_day=True,
            location="SLAC, Kavli Auditorium",
            abstract="Two days of talks; cryo-EM, crystallography, and structure prediction.",
            fields=["chem", "bio", "physics"],
            picks_score=4.5,
            picks_reasons=["abstract: cryo-EM"],
        ),
        golden_event(
            "golden-cancelled",
            "Random matrices and number theory",
            at(11, 13, 16),
            speakers=[{"name": "Wei Zhang", "affiliation": "MIT"}],
            series="Math Colloquium",
            host_org="Stanford Mathematics",
            location="Building 380, Room 380-C",
            abstract=None,
            fields=["math"],
            status="cancelled",
            sequence=1,
        ),
        golden_event(
            "golden-online",
            "Serving large models; cheap, fast \\ robust",
            at(11, 14, 15, 30),
            speakers=[{"name": "Priya Raman", "affiliation": "Example Labs, Inc."}],
            series="MLSys Seminar",
            host_org="Stanford MLSys",
            location=None,
            is_online=True,
            stream_url="https://stanford.zoom.us/j/1234567890",
            abstract="Online only.\n\nFirst: batching; second: caching.\nThird, " + "x" * 1200,
            fields=["ai-ml"],
            status="tentative",
        ),
        golden_event(
            "golden-hybrid",
            "Grand Rounds: cell therapies for solid tumors",
            at(11, 19, 8),
            speakers=people("Maria Chen", "Omar Haddad", "Ana Diaz", "Bo Li"),
            series=None,
            host_org="Stanford Medicine",
            location="Li Ka Shing Center, Berg Hall",
            is_online=True,
            abstract="Hybrid event.",
            fields=["med", "bio"],
        ),
    ]


class TestFeedFiles:
    def test_the_contracts_point_at_the_implementation(self) -> None:
        writer: IcsFeedWriter = load_implementation("ics_feeds")
        validator: IcsValidator = load_implementation("ics_validator")
        assert (writer, validator) == (write_ics_feeds, validate_ics)

    def test_ten_feeds_are_written_in_feed_order(self, app_config: AppConfig) -> None:
        out_dir = app_config.paths.public_feeds_dir
        paths = write_ics_feeds(golden_events(), out_dir, config=app_config)
        assert paths == [out_dir / f"{feed}.ics" for feed in FEED_IDS]
        assert sorted(out_dir.iterdir()) == sorted(paths)

    def test_each_feed_holds_its_events_ordered_by_start_and_uid(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        feeds = write_feeds(golden_events(), app_config, tmp_path)
        assert uids(feeds["all"]) == [
            *("golden-cancelled", "golden-online", "golden-pick"),
            *("golden-tba", "golden-hybrid", "golden-all-day"),
        ]
        assert uids(feeds["picks"]) == ["golden-pick", "golden-all-day"]
        assert uids(feeds["cs"]) == []
        assert uids(feeds["conferences"]) == ["golden-all-day"]
        for field in FIELD_IDS:
            expected = {event.uid for event in golden_events() if field in event.fields}
            assert set(uids(feeds[field])) == expected
        same_start = [make_event(uid=uid) for uid in ("c", "a", "b")]
        assert uids(write_feeds(same_start, app_config, tmp_path)["all"]) == ["a", "b", "c"]

    def test_the_conferences_feed_holds_the_meetings(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        day = {"all_day": True, "start": at(12, 1, 0)}
        events = [
            make_event(uid="talk"),
            make_event(uid="conference", event_type="conference"),
            make_event(uid="long-workshop", event_type="workshop", end=at(11, 17, 19)),
            make_event(uid="short-workshop", event_type="workshop"),
            make_event(uid="workshop-days", event_type="workshop", **day, end=at(12, 3, 0)),
            make_event(uid="workshop-day", event_type="workshop", **day, end=None),
            make_event(uid="cancelled", event_type="conference", status="cancelled"),
        ]
        feeds = write_feeds(events, app_config, tmp_path)
        meetings = ["cancelled", "conference", "long-workshop", "workshop-days"]
        assert uids(feeds["conferences"]) == meetings
        assert {event.uid for event in events if event.is_meeting} == set(meetings)
        assert set(uids(feeds["all"])) == {event.uid for event in events}
        assert set(meetings) <= set(uids(feeds["bio"]))

    def test_the_picks_threshold_comes_from_the_interests(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        events = [make_event(uid="low", picks_score=2.0), make_event(uid="high", picks_score=5.0)]
        assert uids(write_feeds(events, app_config, tmp_path)["picks"]) == ["high"]
        eager = dataclasses.replace(app_config, interests=Interests(threshold=1.5))
        assert uids(write_feeds(events, eager, tmp_path)["picks"]) == ["high", "low"]

    def test_a_uid_that_appears_twice_is_an_error(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="duplicate event uid: 'same'"):
            write_ics_feeds([make_event(uid="same")] * 2, tmp_path / "feeds", config=app_config)
        assert not (tmp_path / "feeds").exists()


class TestDeterminism:
    def test_the_same_input_gives_the_same_bytes(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        events = golden_events()
        before = [event.model_dump_json() for event in events]
        first = write_ics_feeds(events, tmp_path / "a", config=app_config)
        second = write_ics_feeds(events, tmp_path / "b", config=app_config)
        assert [path.read_bytes() for path in first] == [path.read_bytes() for path in second]
        assert [event.model_dump_json() for event in events] == before

    def test_neither_the_order_nor_the_spelling_of_the_input_matters(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        """Events come back from data/events.json with fixed offsets instead of a zone."""
        expected = write_feeds(golden_events(), app_config, tmp_path / "a")
        shuffled = golden_events()
        random.Random(7).shuffle(shuffled)
        assert write_feeds(shuffled, app_config, tmp_path / "b") == expected
        reloaded = [Event.model_validate(json.loads(e.model_dump_json())) for e in shuffled]
        assert write_feeds(reloaded, app_config, tmp_path / "c") == expected

    def test_a_rerun_replaces_the_files(self, app_config: AppConfig, tmp_path: Path) -> None:
        write_ics_feeds(golden_events(), tmp_path, config=app_config)
        assert uids(write_feeds([make_event(uid="only")], app_config, tmp_path)["all"]) == ["only"]


class TestGoldenFiles:
    @pytest.mark.parametrize("feed", ["all", "picks", "cs", "conferences"])
    def test_feed_matches_its_golden_file(
        self, feed: str, app_config: AppConfig, tmp_path: Path
    ) -> None:
        text = write_feeds(golden_events(), app_config, tmp_path)[feed]
        assert text.isascii()
        assert_matches_golden(f"{feed}.ics", text.replace("\r\n", "\n"))

    def test_feeds_end_every_line_with_crlf_and_are_valid(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        for feed, text in write_feeds(golden_events(), app_config, tmp_path).items():
            assert text.endswith("END:VCALENDAR\r\n")
            assert not re.search(r"[\r\n]", text.replace("\r\n", ""))
            assert validate_ics(text) == [], feed
            assert decorative_characters(text) == []


class TestCalendarHeader:
    def test_fixed_header_lines(self, app_config: AppConfig, tmp_path: Path) -> None:
        lines = content_lines(write_feeds(golden_events(), app_config, tmp_path)["all"])
        assert lines[:5] == [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            f"PRODID:-//{UID_DOMAIN}//seminars//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
        ]
        assert "X-WR-TIMEZONE:America/Los_Angeles" in lines
        assert "REFRESH-INTERVAL;VALUE=DURATION:PT6H" in lines
        assert "X-PUBLISHED-TTL:PT6H" in lines
        assert not [line for line in lines if "VALARM" in line]

    def test_names_and_descriptions_come_from_the_english_locale(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        english = Locale.load("en", app_config.paths.locales_dir)
        feeds = write_feeds(golden_events(), app_config, tmp_path)
        assert "X-WR-CALNAME:Seminars - Math" in content_lines(feeds["math"])
        assert "X-WR-CALNAME:Seminars - Picks" in content_lines(feeds["picks"])
        for feed, text in feeds.items():
            description = english.t(f"feeds.{feed}.description").replace(",", "\\,")
            assert f"X-WR-CALNAME:{english.t(f'feeds.{feed}.name')}" in content_lines(text)
            assert f"X-WR-CALDESC:{description}" in content_lines(text)

    def test_no_text_is_hardcoded(self, app_config: AppConfig, tmp_path: Path) -> None:
        path = app_config.paths.locales_dir / "en.json"
        tree = json.loads(read_text(path))
        tree["feeds"]["math"] = {"name": "Mathe; Kalender", "description": "Alles, was rechnet"}
        tree["fields"]["bio"] = "Life science"
        tree["digest"]["table"] = {"speaker": "Presenter", "reasons": "Chosen for"}
        tree["digest"]["labels"] = {"online": "Remote", "details": "More", "cancelled": "Off"}
        tree["digest"]["health"]["source"] = "Seen at"
        write_text_atomic(path, json.dumps(tree))
        feeds = write_feeds(golden_events(), app_config, tmp_path)
        assert "X-WR-CALNAME:Mathe\\; Kalender" in content_lines(feeds["math"])
        assert "X-WR-CALDESC:Alles\\, was rechnet" in content_lines(feeds["math"])
        by_uid = {str(part["uid"]).split("@")[0]: part for part in vevents(feeds["all"])}
        pick = by_uid["golden-pick"]
        assert [str(name) for name in pick.categories] == ["AI/ML", "Life science"]
        assert str(pick["description"]).split("\n") == [
            "Presenter: Jane Doe (Stanford University); Li Wu",
            "Bioengineering Seminar",
            "Stanford Bioengineering",
            "Chosen for: title: protein design; title: diffusion model",
            "",
            "A talk about generative models for de novo protein design.",
            "",
            f"More: {DETAIL_URL}",
            f"Seen at: {LISTING_URL}",
        ]
        assert str(by_uid["golden-online"]["location"]) == "Remote"
        assert "\nRemote: https://stanford.zoom.us/" in str(by_uid["golden-online"]["description"])
        assert str(by_uid["golden-cancelled"]["summary"]).startswith("Off: Random matrices")

    def test_refresh_interval_and_zone_follow_the_settings(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        feeds_settings = app_config.settings.feeds.evolve(refresh_interval_hours=12)
        config = with_settings(app_config, feeds=feeds_settings, timezone="Europe/Berlin")
        lines = content_lines(write_feeds([make_event()], config, tmp_path)["all"])
        assert "REFRESH-INTERVAL;VALUE=DURATION:PT12H" in lines
        assert "X-PUBLISHED-TTL:PT12H" in lines
        assert "X-WR-TIMEZONE:Europe/Berlin" in lines
        assert "DTSTART;TZID=Europe/Berlin:20261118T010000" in lines


class TestTimezoneDefinition:
    def test_pacific_time_with_the_changes_of_the_years_in_use(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        events = [make_event(uid="a"), make_event(uid="b", start=at(3, 20, 9, year=2027), end=None)]
        assert timezone_block(write_feeds(events, app_config, tmp_path)["all"]) == [
            *("BEGIN:VTIMEZONE", "TZID:America/Los_Angeles"),
            *("BEGIN:STANDARD", "DTSTART:20260101T000000", "TZNAME:PST"),
            *("TZOFFSETFROM:-0800", "TZOFFSETTO:-0800", "END:STANDARD"),
            *("BEGIN:DAYLIGHT", "DTSTART:20260308T020000", "TZNAME:PDT"),
            *("TZOFFSETFROM:-0800", "TZOFFSETTO:-0700", "END:DAYLIGHT"),
            *("BEGIN:STANDARD", "DTSTART:20261101T020000", "TZNAME:PST"),
            *("TZOFFSETFROM:-0700", "TZOFFSETTO:-0800", "END:STANDARD"),
            *("BEGIN:DAYLIGHT", "DTSTART:20270314T020000", "TZNAME:PDT"),
            *("TZOFFSETFROM:-0800", "TZOFFSETTO:-0700", "END:DAYLIGHT"),
            *("BEGIN:STANDARD", "DTSTART:20271107T020000", "TZNAME:PST"),
            *("TZOFFSETFROM:-0700", "TZOFFSETTO:-0800", "END:STANDARD"),
            "END:VTIMEZONE",
        ]

    def test_every_feed_of_a_run_has_the_same_definition(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        feeds = write_feeds(golden_events(), app_config, tmp_path)
        assert len({tuple(timezone_block(text)) for text in feeds.values()}) == 1
        nothing = write_feeds([], app_config, tmp_path)["all"]
        assert "DTSTART:19700101T000000" in timezone_block(nothing)
        assert validate_ics(nothing) == []

    def test_onsets_are_local_times_before_the_change(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        """Berlin: summer time starts at 02:00 standard time and ends at 03:00 summer time."""
        berlin = with_settings(app_config, timezone="Europe/Berlin")
        block = timezone_block(write_feeds([make_event()], berlin, tmp_path)["all"])
        assert "DTSTART:20260329T020000" in block
        assert "DTSTART:20261025T030000" in block
        shanghai = with_settings(app_config, timezone="Asia/Shanghai")
        assert timezone_block(write_feeds([make_event()], shanghai, tmp_path)["all"]) == [
            *("BEGIN:VTIMEZONE", "TZID:Asia/Shanghai"),
            *("BEGIN:STANDARD", "DTSTART:20260101T000000", "TZNAME:CST"),
            *("TZOFFSETFROM:+0800", "TZOFFSETTO:+0800", "END:STANDARD"),
            "END:VTIMEZONE",
        ]

    def test_an_end_in_the_far_future_does_not_stretch_the_definition(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        text = write_feeds([make_event(end=at(1, 1, 0, year=2400))], app_config, tmp_path)["all"]
        assert max(re.findall(r"2\d\d\d(?=\d{4}T)", "\n".join(timezone_block(text)))) == "2036"
        assert validate_ics(text) == []


class TestEventProperties:
    def test_uid_stamps_and_sequence(self, one: Any, app_config: AppConfig) -> None:
        changed = datetime(2026, 11, 12, 6, 30, 15, tzinfo=timezone(timedelta(hours=-8)))
        component = one(make_event(uid="abc.DEF_1-2", last_changed=changed, sequence=4))
        assert str(component["uid"]) == f"abc.DEF_1-2@{UID_DOMAIN}"
        assert b"\r\nDTSTAMP:20261112T143015Z\r\n" in component.to_ical()
        assert b"\r\nLAST-MODIFIED:20261112T143015Z\r\n" in component.to_ical()
        assert component.decoded("sequence") == 4
        other_domain = with_settings(app_config, uid_domain="cal.example.org")
        assert str(one(make_event(uid="x"), other_domain)["uid"]) == "x@cal.example.org"

    def test_timed_event_in_local_time(self, one: Any) -> None:
        component = one(make_event())
        assert f"DTSTART;{LOCAL}20261117T160000\r\n".encode() in component.to_ical()
        assert f"DTEND;{LOCAL}20261117T170000\r\n".encode() in component.to_ical()
        assert component.decoded("dtstart") == at(11, 17, 16)
        assert component.decoded("dtend") == at(11, 17, 17)

    @pytest.mark.parametrize("end", [None, at(11, 17, 16)])
    def test_missing_or_empty_duration_becomes_the_default(
        self, end: datetime | None, one: Any, app_config: AppConfig
    ) -> None:
        assert one(make_event(end=end)).decoded("dtend") == at(11, 17, 17)
        longer = app_config.settings.events.evolve(default_duration_minutes=90)
        component = one(make_event(end=end), with_settings(app_config, events=longer))
        assert component.decoded("dtend") == at(11, 17, 17, 30)

    @pytest.mark.parametrize(
        ("end", "after"),
        [
            (at(11, 22, 0), "20261122"),
            (None, "20261121"),
            (at(11, 20, 0), "20261121"),
            (at(11, 20, 17), "20261121"),
            (at(11, 21, 9), "20261122"),
        ],
    )
    def test_all_day_event_uses_date_values_and_covers_whole_days(
        self, end: datetime | None, after: str, one: Any
    ) -> None:
        component = one(make_event(all_day=True, start=at(11, 20, 0), end=end))
        assert b"DTSTART;VALUE=DATE:20261120\r\n" in component.to_ical()
        assert f"DTEND;VALUE=DATE:{after}\r\n".encode() in component.to_ical()
        assert component.decoded("dtstart") == date(2026, 11, 20)

    def test_all_day_dates_are_days_of_the_configured_zone(self, one: Any) -> None:
        midnight = utc(11, 20, 8)  # 00:00 Pacific Standard Time
        component = one(make_event(all_day=True, start=midnight, end=None))
        assert component.decoded("dtstart") == date(2026, 11, 20)

    @pytest.mark.parametrize(
        ("overrides", "summary"),
        [
            ({}, "Protein design with diffusion models - Jane Doe"),
            ({"speakers": []}, "Protein design with diffusion models"),
            ({"speakers": people("A", "B", "C")}, "Protein design with diffusion models - A, B, C"),
            ({"speakers": people("A", "B", "C", "D"), "title": "T"}, "T - A, B, C, ..."),
            ({"title": "TBA"}, "Bioengineering Seminar: Jane Doe"),
            ({"title": "TBA", "speakers": []}, "Bioengineering Seminar: TBA"),
            ({"title": "TBA", "series": None}, "Stanford Bioengineering: Jane Doe"),
            ({"status": "cancelled", "title": "T"}, "Cancelled: T - Jane Doe"),
            ({"title": "Two\nlines  and\ttabs"}, "Two lines and tabs - Jane Doe"),
            ({"title": ROCKET}, "Bioengineering Seminar: Jane Doe"),
            # a speaker the title names already is not repeated
            (
                {
                    "title": "Thesis Defense: Julisia Chau, Lin Group",
                    "speakers": people("Julisia Chau"),
                },
                "Thesis Defense: Julisia Chau, Lin Group",
            ),
            (
                {"title": "DISSERTATION DEFENSE: Connie Miao", "speakers": people("Connie Miao")},
                "DISSERTATION DEFENSE: Connie Miao",
            ),
            (
                {"title": f"Seminar: Jos{E_ACUTE} Garcia", "speakers": people("JOSE GARC\u00cdA")},
                f"Seminar: Jos{E_ACUTE} Garcia",
            ),
            (
                {"title": "Jane Doe on knots", "speakers": people("Jane Doe", "Bob Roe")},
                "Jane Doe on knots - Bob Roe",
            ),
            ({"title": "Annals of knots", "speakers": people("Ann")}, "Annals of knots - Ann"),
            ({"title": "Jane Doe", "status": "cancelled"}, "Cancelled: Jane Doe"),
            # speakers read from the title: the summary is the title alone
            (
                {"title": "Evolgenome Seminar: Ami Bhatt", "speakers": people("Ami Bhatt")},
                "Evolgenome Seminar: Ami Bhatt",
            ),
            (
                {
                    "title": "Sunshine - Bryant Lin in conversation with Latha Palaniappan",
                    "speakers": people("Bryant Lin", "Latha Palaniappan"),
                },
                "Sunshine - Bryant Lin in conversation with Latha Palaniappan",
            ),
            ({"title": "Knots", "speakers": people("Jane Doe")}, "Knots - Jane Doe"),
            # the limit of three counts the speakers that are left after the title
            (
                {"title": "Panel: Ann Lee", "speakers": people("Ann Lee", "B", "C", "D")},
                "Panel: Ann Lee - B, C, D",
            ),
            # the title of a talk without a title stays the series, with every speaker
            (
                {"title": "TBA", "series": "Ami Bhatt Lecture", "speakers": people("Ami Bhatt")},
                "Ami Bhatt Lecture: Ami Bhatt",
            ),
        ],
    )
    def test_summary(self, overrides: dict[str, Any], summary: str, one: Any) -> None:
        assert str(one(make_event(**overrides))["summary"]) == summary

    @pytest.mark.parametrize(
        ("overrides", "location"),
        [
            ({}, "Shriram Center, Room 104"),
            ({"is_online": True}, "Shriram Center, Room 104"),
            ({"location": None, "is_online": True}, "Online"),
            ({"location": "  ", "is_online": True}, "Online"),
            ({"location": None}, None),
            ({"location": "Room 1\nBuilding 2"}, "Room 1 Building 2"),
        ],
    )
    def test_location(self, overrides: dict[str, Any], location: str | None, one: Any) -> None:
        component = one(make_event(**overrides))
        assert (str(component["location"]) if "location" in component else None) == location

    def test_url_categories_and_status(self, one: Any) -> None:
        component = one(make_event(fields=["physics", "ai-ml", "math"], status="tentative"))
        assert str(component["url"]) == DETAIL_URL
        assert [str(name) for name in component.categories] == ["Math", "AI/ML", "Physics"]
        assert str(component["status"]) == "TENTATIVE"
        assert str(one(make_event())["status"]) == "CONFIRMED"
        assert str(one(make_event(status="cancelled"))["status"]) == "CANCELLED"
        assert "categories" not in one(make_event(fields=[]))
        odd = one(make_event(url="https://example.org/a b?q=\u00e9,;|"))
        assert str(odd["url"]) == "https://example.org/a%20b?q=%C3%A9,;%7C"

    def test_description_of_a_plain_event(self, one: Any) -> None:
        assert str(one(make_event())["description"]).split("\n") == [
            "Speaker: Jane Doe (Stanford University)",
            "Bioengineering Seminar",
            "Stanford Bioengineering",
            "",
            "A talk about generative models for de novo protein design.",
            "",
            f"Details: {DETAIL_URL}",
        ]

    def test_description_with_everything(self, one: Any) -> None:
        event = make_event(
            speakers=[{"name": "Jane Doe", "affiliation": "Stanford University"}, *people("Li Wu")],
            picks_score=9.0,
            picks_reasons=["title: protein design", "series: allow-list"],
            is_online=True,
            stream_url="https://stanford.zoom.us/j/1234567890",
            source_urls=[LISTING_URL, DETAIL_URL, "https://example.org/mirror"],
        )
        assert str(one(event)["description"]).split("\n") == [
            "Speaker: Jane Doe (Stanford University); Li Wu",
            "Bioengineering Seminar",
            "Stanford Bioengineering",
            "Why picked: title: protein design; series: allow-list",
            "Online: https://stanford.zoom.us/j/1234567890",
            "",
            "A talk about generative models for de novo protein design.",
            "",
            f"Details: {DETAIL_URL}",
            f"Source: {LISTING_URL}",
            "Source: https://example.org/mirror",
        ]

    def test_description_of_a_bare_event(self, one: Any) -> None:
        bare = {"speakers": [], "abstract": None, "is_online": True, "picks_reasons": ["below"]}
        assert str(one(make_event(series=None, **bare))["description"]).split("\n") == [
            *("Stanford Bioengineering", "Online", ""),
            f"Details: {DETAIL_URL}",
        ]
        repeated = make_event(series="SLAC Colloquium", host_org="slac  colloquium", **bare)
        assert str(one(repeated)["description"]).split("\n")[:2] == ["SLAC Colloquium", "Online"]

    def test_long_text_is_cut_between_words(self, one: Any) -> None:
        abstract = "First  line \r\n\r\n\n\n second\tline\n" + "word " * 400
        component = one(make_event(title="word " * 200, abstract=abstract))
        summary = str(component["summary"])
        body = str(component["description"]).split("\n\n", 1)[1].rsplit("\n\n", 1)[0]
        assert 240 < len(summary) <= 250
        assert summary.endswith(" word...")
        assert body.startswith("First line\n\nsecond line\nword word")
        assert 990 < len(body) <= 1000
        assert body.endswith(" word...")


class TestStableUid:
    def test_a_title_that_is_announced_keeps_the_uid_and_raises_the_sequence(
        self, one: Any
    ) -> None:
        placeholder = make_event(uid="chem-42", title="TBA", sequence=0)
        announced = placeholder.evolve(
            title="Catalysis in confined spaces",
            sequence=placeholder.sequence + 1,
            last_changed=REFERENCE_NOW + timedelta(days=1),
        )
        before, after = one(placeholder), one(announced)
        assert str(before["uid"]) == str(after["uid"]) == f"chem-42@{UID_DOMAIN}"
        assert (before.decoded("sequence"), after.decoded("sequence")) == (0, 1)
        assert str(before["summary"]) == "Bioengineering Seminar: Jane Doe"
        assert str(after["summary"]) == "Catalysis in confined spaces - Jane Doe"
        assert before.decoded("last-modified") < after.decoded("last-modified")
        moved = announced.evolve(start=at(11, 24, 10), end=at(11, 24, 11), sequence=2)
        assert str(one(moved)["uid"]) == str(before["uid"])

    def test_two_runs_give_the_same_set_of_uids(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        first = write_feeds(golden_events(), app_config, tmp_path / "a")
        second = write_feeds(golden_events(), app_config, tmp_path / "b")
        for feed in FEED_IDS:
            assert set(uids(first[feed])) == set(uids(second[feed]))
        assert len(set(uids(first["all"]))) == len(golden_events())


class TestTimeZonesAndDst:
    @pytest.mark.parametrize(
        ("start", "line"),
        [
            # the Sunday the clocks go back: 00:30 PDT, 01:30 PDT, 01:30 PST, 02:30 PST;
            # both moments of the repeated hour are written in UTC
            (utc(11, 1, 7, 30), f"DTSTART;{LOCAL}20261101T003000"),
            (utc(11, 1, 8, 30), "DTSTART:20261101T083000Z"),
            (utc(11, 1, 9, 30), "DTSTART:20261101T093000Z"),
            (utc(11, 1, 10, 30), f"DTSTART;{LOCAL}20261101T023000"),
            # the Sunday the clocks go forward: 01:30 PST, then 03:30 PDT
            (utc(3, 14, 9, 30, year=2027), f"DTSTART;{LOCAL}20270314T013000"),
            (utc(3, 14, 10, 30, year=2027), f"DTSTART;{LOCAL}20270314T033000"),
        ],
    )
    def test_the_moment_survives_a_change_of_the_clocks(
        self, start: datetime, line: str, one: Any
    ) -> None:
        component = one(make_event(start=start, end=None))
        assert line in content_lines(component.to_ical().decode("utf-8"))
        # compared in UTC: a local time in the repeated hour equals no time of another zone
        assert component.decoded("dtstart").astimezone(UTC) == start
        assert component.decoded("dtend").astimezone(UTC) == start + timedelta(hours=1)

    def test_other_offsets_become_local_time_and_midnight_is_crossed(self, one: Any) -> None:
        india = timezone(timedelta(hours=5, minutes=30))
        start = datetime(2026, 11, 18, 12, 30, tzinfo=india)  # 23:00 on the 17th in Pacific time
        component = one(make_event(start=start, end=start + timedelta(hours=2)))
        assert f"DTSTART;{LOCAL}20261117T230000\r\n".encode() in component.to_ical()
        assert f"DTEND;{LOCAL}20261118T010000\r\n".encode() in component.to_ical()


class TestEscapingAndFolding:
    def test_hostile_text_stays_inside_its_property(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        event = make_event(title=HOSTILE, location=HOSTILE, abstract=HOSTILE)
        text = write_feeds([event], app_config, tmp_path)["all"]
        lines = content_lines(text)
        assert validate_ics(text) == []
        assert lines.count("BEGIN:VEVENT") == 1
        assert [line for line in lines if line.startswith(("ATTENDEE", "X-A", "X-B", "X-C"))] == []
        assert [line for line in lines if line.startswith("UID:")] == [
            f"UID:sites-bioengineering-1234@{UID_DOMAIN}"
        ]
        assert not re.search("[\x00-\x09\x0b\x0c\x0e-\x1f\x7f\x85\u2028]", text)
        flat = (
            "Commas, semicolons; back\\slashes and \\n END:VEVENT BEGIN:VEVENT "
            "UID:evil@example.org ATTENDEE:mailto:x@example.org X-A:1 X-B:2 X-C:3[31m"
        )
        (component,) = vevents(text)
        assert str(component["summary"]) == f"{flat} - Jane Doe"
        assert str(component["location"]) == flat
        assert flat.replace(" END", "\nEND").replace(" BEGIN", "\nBEGIN").replace(
            " UID", "\nUID"
        ).replace(" ATT", "\nATT").replace(" X-", "\nX-") in str(component["description"])

    def test_escapes_as_written(self, app_config: AppConfig, tmp_path: Path) -> None:
        event = make_event(title="a, b; c \\ d", abstract="one\ntwo", speakers=[])
        lines = content_lines(write_feeds([event], app_config, tmp_path)["all"])
        assert "SUMMARY:a\\, b\\; c \\\\ d" in lines
        assert "LOCATION:Shriram Center\\, Room 104" in lines
        assert (
            "DESCRIPTION:Bioengineering Seminar\\nStanford Bioengineering\\n\\none\\ntwo"
            f"\\n\\nDetails: {DETAIL_URL}"
        ) in lines

    @pytest.mark.parametrize("filler", ["x", E_ACUTE, CJK, MATH_BOLD, "a" + CJK + E_ACUTE])
    def test_lines_are_folded_at_75_octets_between_characters(
        self, filler: str, app_config: AppConfig, tmp_path: Path
    ) -> None:
        text = filler * (240 // len(filler))
        speakers = [{"name": text, "affiliation": text}]
        event = make_event(title=text, location=text, abstract=text, speakers=speakers)
        raw = write_ics_feeds([event], tmp_path, config=app_config)[0].read_bytes()
        for line in raw.split(b"\r\n"):
            assert len(line) <= 75
            line.decode("utf-8")  # no character is split between two lines
        (component,) = vevents(raw.decode("utf-8"))
        assert str(component["location"]) == text
        assert f"\n\n{text}\n\n" in str(component["description"])
        assert validate_ics(raw.decode("utf-8")) == []

    def test_non_ascii_text_is_kept(self, one: Any) -> None:
        name = f"Zo{E_ACUTE} M\u00fcller"
        event = make_event(
            title=f"{CJK} and Poincar{E_ACUTE} duality",
            speakers=[{"name": name, "affiliation": f"Universit{E_ACUTE} de Gen\u00e8ve"}],
            location=f"\u6e05\u534e\u5927\u5b66 {MATH_BOLD}",
        )
        component = one(event)
        assert str(component["summary"]) == f"{CJK} and Poincar{E_ACUTE} duality - {name}"
        assert str(component["location"]) == f"\u6e05\u534e\u5927\u5b66 {MATH_BOLD}"
        assert f"Speaker: {name} (Universit{E_ACUTE} de Gen\u00e8ve)" in str(
            component["description"]
        )

    def test_emoji_and_decorative_characters_never_reach_a_feed(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        event = make_event(
            title=DIRTY,
            speakers=[{"name": DIRTY, "affiliation": DIRTY}],
            series=DIRTY,
            host_org=DIRTY,
            location=DIRTY,
            abstract=DIRTY,
            picks_score=9.0,
            picks_reasons=[DIRTY],
        )
        feeds = write_feeds([event], app_config, tmp_path)
        assert all(decorative_characters(text) == [] for text in feeds.values())
        (component,) = vevents(feeds["all"])
        assert str(component["location"]) == "Launch day fine"


class TestAnyInput:
    @given(events=event_lists())
    @settings(
        max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    def test_every_feed_is_valid_whatever_the_events_are(
        self, events: list[Event], app_config: AppConfig, tmp_path: Path
    ) -> None:
        paths = write_ics_feeds(events, tmp_path / "a", config=app_config)
        again = write_ics_feeds(list(reversed(events)), tmp_path / "b", config=app_config)
        assert [path.read_bytes() for path in paths] == [path.read_bytes() for path in again]
        for path in paths:
            raw = path.read_bytes()
            assert validate_ics(raw.decode("utf-8")) == []
            assert decorative_characters(raw.decode("utf-8")) == []
            assert all(len(line) <= 75 for line in raw.split(b"\r\n"))
        by_uid = {event.uid: event for event in events}
        components = vevents(read_text(paths[0]))
        assert sorted(str(part["uid"]).split("@")[0] for part in components) == sorted(by_uid)
        for component in components:
            event = by_uid[str(component["uid"]).split("@")[0]]
            start = component.decoded("dtstart")
            assert component.decoded("sequence") == event.sequence
            assert component.decoded("dtstamp") == event.last_changed
            assert component.decoded("dtend") > start
            assert isinstance(start, datetime) != event.all_day
            assert event.all_day or start.astimezone(UTC) == event.start.astimezone(UTC)

    @given(text=st.text(max_size=300), position=st.integers(min_value=0, max_value=2000))
    @settings(max_examples=200, deadline=None)
    def test_the_validator_judges_any_text_without_failing(self, text: str, position: int) -> None:
        valid = document(*MINIMAL)
        for candidate in (text, valid[:position] + text + valid[position:]):
            problems = validate_ics(candidate)
            assert all(problem.isascii() and problem.isprintable() for problem in problems)
        assert validate_ics(text) != [] or "BEGIN:VCALENDAR" in text.upper()


MINIMAL = (
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//example//test//EN",
    "BEGIN:VTIMEZONE",
    "TZID:America/Los_Angeles",
    "BEGIN:STANDARD",
    "DTSTART:20260101T000000",
    "TZOFFSETFROM:-0800",
    "TZOFFSETTO:-0800",
    "END:STANDARD",
    "END:VTIMEZONE",
    "BEGIN:VEVENT",
    "UID:a@example.org",
    "DTSTAMP:20261110T200000Z",
    f"DTSTART;{LOCAL}20261117T160000",
    f"DTEND;{LOCAL}20261117T170000",
    "SUMMARY:Talk\\, with \\; escapes \\\\ and \\n",
    "CATEGORIES:Math,Physics",
    "STATUS:CONFIRMED",
    "SEQUENCE:0",
    "URL:https://example.org/talk",
    "END:VEVENT",
    "END:VCALENDAR",
)
VERSION, PRODID, ZONE_ID, ZONE_START, ZONE_FROM = 1, 2, 4, 6, 7
UID, STAMP, START, END, SUMMARY, CATEGORIES, STATUS, SEQUENCE, URL, CLOSE = range(12, 22)


def document(*lines: str) -> str:
    return "".join(f"{line}\r\n" for line in lines)


def swapped(index: int, *new: str) -> str:
    """The MINIMAL document with line ``index`` (counted from 0) replaced by ``new``."""
    return document(*MINIMAL[:index], *new, *MINIMAL[index + 1 :])


class TestValidator:
    def test_correct_documents_have_no_problems(self) -> None:
        assert validate_ics(document(*MINIMAL)) == []
        folded = "SUMMARY:Talk\\, with \\; esc\r\n apes \\\\ and \\\r\n\tn"
        assert validate_ics(swapped(SUMMARY, folded)) == []
        quoted = 'DTSTART;TZID="America/Los_Angeles";X-NOTE="a:b;c":20261117T160000'
        assert validate_ics(swapped(START, quoted)) == []
        assert validate_ics(swapped(START, "DTSTART:20261117T235959Z")) == []  # 15:59:59 PST
        dates = [*MINIMAL[:START], "DTSTART;VALUE=DATE:20261117", "DTEND;VALUE=DATE:20261118"]
        assert validate_ics(document(*dates, *MINIMAL[SUMMARY:])) == []
        bare = [*MINIMAL[:3], *MINIMAL[11:14], "DTSTART:20261117T160000Z", *MINIMAL[CLOSE:]]
        assert validate_ics(document(*bare)) == []

    @pytest.mark.parametrize(
        ("text", "problem"),
        [
            ("", "the document is empty"),
            ("<html>\r\n", "line 1: not a content line"),
            (
                document(*MINIMAL).replace("\r\n", "\n"),
                "line 1: line ending is not CRLF (23 lines)",
            ),
            (document(*MINIMAL)[:-2], "line 23: line ending is not CRLF (1 lines)"),
            (document(*MINIMAL[:-1]), "line 1: BEGIN:VCALENDAR is never closed"),
            (document(*MINIMAL[1:]), "line 1: the document must start with BEGIN:VCALENDAR"),
            (document(*MINIMAL, *MINIMAL), "line 24: content after END:VCALENDAR"),
            (document(" X:1", *MINIMAL), "line 1: continuation of nothing"),
            (document(*MINIMAL[:3], MINIMAL[-1]), "line 1: VCALENDAR has no component"),
            (
                document(*MINIMAL[:5], *MINIMAL[10:]),
                "line 4: VTIMEZONE has neither STANDARD nor DAYLIGHT",
            ),
            (
                document(*MINIMAL[:22], *MINIMAL[11:]),
                "line 24: UID is used by more than one VEVENT",
            ),
        ],
    )
    def test_problems_of_the_document(self, text: str, problem: str) -> None:
        assert problem in validate_ics(text)

    @pytest.mark.parametrize(
        ("index", "new", "problem"),
        [
            (CLOSE, ["END:VTODO"], "line 22: END:VTODO does not close BEGIN:VEVENT"),
            (CLOSE, [], "line 22: END:VCALENDAR does not close BEGIN:VEVENT"),
            (VERSION, [], "line 1: VCALENDAR has no VERSION"),
            (VERSION, ["VERSION:1.0"], "line 2: VERSION must be 2.0"),
            (VERSION, ["VERSION:2.0", "CALSCALE:JULIAN"], "line 3: CALSCALE must be GREGORIAN"),
            (PRODID, [], "line 1: VCALENDAR has no PRODID"),
            (ZONE_ID, [], "line 4: VTIMEZONE has no TZID"),
            (ZONE_START, ["DTSTART:20260101T000000Z"], "line 7: DTSTART is not a local date-time"),
            (ZONE_FROM, ["TZOFFSETFROM:-8"], "line 8: TZOFFSETFROM is not a UTC offset"),
            (UID, [], "line 12: VEVENT has no UID"),
            (UID, ["UID:"], "line 13: UID is empty"),
            (STAMP, [], "line 12: VEVENT has no DTSTAMP"),
            (STAMP, ["DTSTAMP:20261110T200000"], "line 14: DTSTAMP is not a UTC date-time"),
            (START, [], "line 12: VEVENT has no DTSTART"),
            (START, ["DTSTART:2026-11-17"], "line 15: DTSTART is not a date-time"),
            (START, ["DTSTART:20261317T160000"], "line 15: DTSTART is not a date-time"),
            (START, ["DTSTART;VALUE=DATE:20261117T160000"], "line 15: DTSTART is not a date"),
            (START, [f"DTSTART;{LOCAL}20261117T160000Z"], "line 15: DTSTART is UTC with a TZID"),
            (
                START,
                ["DTSTART;TZID=Mars/Olympus:20261117T160000"],
                "line 15: TZID 'Mars/Olympus' has no VTIMEZONE",
            ),
            (
                START,
                ["DTSTART;VALUE=DATE:20261117"],
                "line 16: DTEND and DTSTART have different value types",
            ),
            (END, [f"DTEND;{LOCAL}20261117T160000"], "line 16: DTEND is not later than DTSTART"),
            (END, ["DTEND:20261117T235959Z"], "line 16: DTEND is not later than DTSTART"),
            (END, [MINIMAL[END], "DURATION:PT1H"], "line 12: VEVENT has both DTEND and DURATION"),
            (STATUS, ["STATUS:MAYBE"], "line 19: STATUS is not a status of an event"),
            (SEQUENCE, ["SEQUENCE:-1"], "line 20: SEQUENCE is not a non-negative integer"),
            (SEQUENCE, ["SEQUENCE:0", "SEQUENCE:1"], "line 12: VEVENT has more than one SEQUENCE"),
            (SUMMARY, ["SUMMARY:a, b"], "line 17: SUMMARY has an unescaped separator"),
            (SUMMARY, ["SUMMARY:a\\\\; b"], "line 17: SUMMARY has an unescaped separator"),
            (SUMMARY, ["SUMMARY:a \\x b"], "line 17: SUMMARY has an invalid escape"),
            (SUMMARY, ["SUMMARY:a \\"], "line 17: SUMMARY has an invalid escape"),
            (SUMMARY, ["SUMMARY:a\x07b"], "line 17: control character U+0007"),
            (SUMMARY, ["SUMMARY:" + "x" * 68], "line 17: longer than 75 octets (76)"),
            (SUMMARY, ["SUMMARY:" + E_ACUTE * 34], "line 17: longer than 75 octets (76)"),
            (SUMMARY, ["SUM MARY:x"], "line 17: not a content line"),
            (CATEGORIES, ["CATEGORIES:a;b"], "line 18: CATEGORIES has an unescaped separator"),
            (URL, ["URL:not a url"], "line 21: URL is not a URI"),
        ],
    )
    def test_problems_of_one_line(self, index: int, new: list[str], problem: str) -> None:
        assert problem in validate_ics(swapped(index, *new))

    def test_problems_never_quote_raw_control_characters(self) -> None:
        problems = validate_ics(swapped(START, "DTSTART;TZID=Evil\x1b[31m:20261117T160000"))
        assert "line 15: TZID 'Evil\\x1b[31m' has no VTIMEZONE" in problems
        assert all(problem.isascii() and problem.isprintable() for problem in problems)

    def test_what_the_parser_library_rejects_is_reported_too(self) -> None:
        (problem,) = validate_ics(swapped(STATUS, "RRULE:FREQ=SOMETIMES"))
        assert problem.startswith("the icalendar parser reports: VEVENT RRULE: ")
        twice = f"DTSTART;VALUE=DATE-TIME,DATE-TIME;{LOCAL}20261117T160000"
        (problem,) = validate_ics(swapped(START, twice))  # the library fails on the parameter
        assert problem.startswith("the icalendar parser reports: AttributeError: ")

    def test_the_placeholder_of_the_fake_writer_is_not_a_valid_feed(self) -> None:
        text = "BEGIN:VCALENDAR\nX-FAKE-FEED:all\nX-FAKE-EVENTS:0\nEND:VCALENDAR\n"
        assert validate_ics(text) != []
