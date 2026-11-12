"""Tests for the time zone definition of the feeds, read back the way calendar programs do.

A VTIMEZONE that is valid RFC 5545 can still be read wrongly: ical.js (the
library of Thunderbird and other web calendars) ignores the DTSTART of an
observance that has an RDATE, so an observance has to name its onset in
DTSTART alone. The readers below are an independent one that follows that
rule and the reader of python-dateutil; both must agree with zoneinfo on
every hour the definition covers.
"""

from __future__ import annotations

import dataclasses
import io
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from dateutil import tz

from factories import PACIFIC, make_event
from seminars.config import AppConfig
from seminars.models import Event
from seminars.outputs.ics import validate_ics, write_ics_feeds
from seminars.textio import read_text

ONE_HOUR = timedelta(hours=1)


def at(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, tzinfo=PACIFIC)


def feed(events: list[Event], config: AppConfig, out_dir: Path) -> str:
    paths = write_ics_feeds(events, out_dir, config=config)
    return read_text(next(path for path in paths if path.stem == "all"))


def timezone_lines(text: str) -> list[str]:
    lines = text.replace("\r\n ", "").split("\r\n")
    return lines[lines.index("BEGIN:VTIMEZONE") : lines.index("END:VTIMEZONE") + 1]


@dataclass(frozen=True)
class Observance:
    dtstart: datetime
    rdates: tuple[datetime, ...]
    offset_from: timedelta
    offset_to: timedelta


def _offset(text: str) -> timedelta:
    sign = -1 if text.startswith("-") else 1
    return sign * timedelta(hours=int(text[1:3]), minutes=int(text[3:5]))


def _local(text: str) -> datetime:
    return datetime.strptime(text, "%Y%m%dT%H%M%S")


def observances(block: list[str]) -> list[Observance]:
    found: list[Observance] = []
    values: dict[str, list[str]] = {}
    for line in block:
        name, _, value = line.partition(":")
        if name == "BEGIN" and value in ("STANDARD", "DAYLIGHT"):
            values = {}
        elif name == "END" and value in ("STANDARD", "DAYLIGHT"):
            found.append(
                Observance(
                    dtstart=_local(values["DTSTART"][0]),
                    rdates=tuple(
                        _local(item) for text in values.get("RDATE", []) for item in text.split(",")
                    ),
                    offset_from=_offset(values["TZOFFSETFROM"][0]),
                    offset_to=_offset(values["TZOFFSETTO"][0]),
                )
            )
        else:
            values.setdefault(name, []).append(value)
    return found


def icaljs_onsets(block: list[str]) -> list[tuple[datetime, timedelta]]:
    """UTC onsets and offsets as ical.js 2.1.0 expands them: DTSTART only without an RDATE."""
    onsets = []
    for item in observances(block):
        for local in item.rdates or (item.dtstart,):
            onsets.append((local.replace(tzinfo=UTC) - item.offset_from, item.offset_to))
    return sorted(onsets)


def icaljs_offset(onsets: list[tuple[datetime, timedelta]], moment: datetime) -> timedelta:
    before = [offset for onset, offset in onsets if onset <= moment]
    return before[-1] if before else onsets[0][1]


def hours(first_year: int, last_year: int) -> Iterator[datetime]:
    moment = datetime(first_year, 1, 2, tzinfo=UTC)
    stop = datetime(last_year, 12, 31, tzinfo=UTC)
    while moment < stop:
        yield moment
        moment += ONE_HOUR


WINDOWS = {
    # a window that crosses New Year in the autumn: summer time of the first year matters
    "autumn": ([at(2027, 9, 2, 16), at(2027, 11, 4, 16), at(2028, 1, 6, 16)], (2027, 2028)),
    "spring": ([at(2027, 3, 20, 9), at(2027, 6, 1, 9)], (2027, 2027)),
    "live": ([at(2026, 10, 29, 10), at(2027, 2, 17, 16)], (2026, 2027)),
}


def window_events(starts: list[datetime]) -> list[Event]:
    return [
        make_event(uid=f"e{number}", start=start, end=None) for number, start in enumerate(starts)
    ]


class TestObservances:
    @pytest.mark.parametrize("window", sorted(WINDOWS))
    def test_every_observance_names_its_onset_in_dtstart_alone(
        self, window: str, app_config: AppConfig, tmp_path: Path
    ) -> None:
        starts, _ = WINDOWS[window]
        block = timezone_lines(feed(window_events(starts), app_config, tmp_path))
        assert not [line for line in block if line.startswith(("RDATE", "RRULE"))]
        assert sum(line.startswith("DTSTART") for line in block) == sum(
            line in ("BEGIN:STANDARD", "BEGIN:DAYLIGHT") for line in block
        )

    def test_a_window_of_two_years_has_five_observances(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        starts, _ = WINDOWS["live"]
        block = timezone_lines(feed(window_events(starts), app_config, tmp_path))
        onsets = [line for line in block if line.startswith("DTSTART")]
        assert onsets == [
            "DTSTART:20260101T000000",
            "DTSTART:20260308T020000",
            "DTSTART:20261101T020000",
            "DTSTART:20270314T020000",
            "DTSTART:20271107T020000",
        ]

    @pytest.mark.parametrize("window", sorted(WINDOWS))
    def test_a_reader_that_ignores_dtstart_next_to_rdate_gets_every_hour_right(
        self, window: str, app_config: AppConfig, tmp_path: Path
    ) -> None:
        starts, (first, last) = WINDOWS[window]
        text = feed(window_events(starts), app_config, tmp_path)
        assert validate_ics(text) == []
        onsets = icaljs_onsets(timezone_lines(text))
        wrong = [
            moment
            for moment in hours(first, last)
            if icaljs_offset(onsets, moment) != moment.astimezone(PACIFIC).utcoffset()
        ]
        assert wrong == []

    @pytest.mark.parametrize("window", sorted(WINDOWS))
    def test_python_dateutil_reads_the_definition_like_zoneinfo(
        self, window: str, app_config: AppConfig, tmp_path: Path
    ) -> None:
        starts, (first, last) = WINDOWS[window]
        block = timezone_lines(feed(window_events(starts), app_config, tmp_path))
        zone = tz.tzical(io.StringIO("\n".join(block) + "\n")).get("America/Los_Angeles")
        wrong = [
            moment
            for moment in hours(first, last)
            if moment.astimezone(zone).utcoffset() != moment.astimezone(PACIFIC).utcoffset()
        ]
        assert wrong == []

    def test_a_zone_with_two_changes_a_year_elsewhere_is_read_right_too(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        berlin = app_config.settings.evolve(timezone="Europe/Berlin")
        config = dataclasses.replace(app_config, settings=berlin)
        starts = [at(2026, 9, 1, 9), at(2027, 8, 1, 9)]
        onsets = icaljs_onsets(timezone_lines(feed(window_events(starts), config, tmp_path)))
        zone = ZoneInfo("Europe/Berlin")
        wrong = [
            moment
            for moment in hours(2026, 2027)
            if icaljs_offset(onsets, moment) != moment.astimezone(zone).utcoffset()
        ]
        assert wrong == []


class TestRepeatedHour:
    def test_no_local_time_of_the_feed_names_two_moments(
        self, app_config: AppConfig, tmp_path: Path
    ) -> None:
        """ical.js and libical take the second of two equal local times, RFC 5545 the first."""
        first = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)  # 00:00 PDT, an hour before the change
        starts = [first + timedelta(minutes=15 * step) for step in range(16)]
        text = feed(window_events(starts), app_config, tmp_path)
        written = [line for line in text.split("\r\n") if line.startswith(("DTSTART;", "DTEND;"))]
        local = [datetime.strptime(line.rpartition(":")[2], "%Y%m%dT%H%M%S") for line in written]
        ambiguous = [
            moment
            for moment in local
            if moment.replace(tzinfo=PACIFIC, fold=0).utcoffset()
            != moment.replace(tzinfo=PACIFIC, fold=1).utcoffset()
        ]
        assert local
        assert ambiguous == []
        assert "DTSTART:20261101T083000Z" in text.split("\r\n")
