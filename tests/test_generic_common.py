"""Tests for the helpers the adapters share that no adapter test reaches on its own."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from seminars.sources.generic.common import MONTHS, month_number, parse_moment, require_groups


class TestRequireGroups:
    def test_a_pattern_with_the_groups_passes(self) -> None:
        require_groups(re.compile(r"(?P<start>\d+)-(?P<end>\d+)"), "times", "start", "end")
        require_groups(None, "times", "start")

    def test_the_missing_groups_are_named(self) -> None:
        with pytest.raises(ValueError, match=r"times needs the group\(s\) end, day"):
            require_groups(re.compile(r"(?P<start>\d+)"), "times", "start", "end", "day")


class TestMonthNumber:
    @pytest.mark.parametrize(
        ("name", "number"),
        [("January", 1), ("feb", 2), ("Sept", 9), ("SEPTEMBER", 9), ("Dec.", 12)],
    )
    def test_names_and_abbreviations(self, name: str, number: int) -> None:
        assert month_number(name) == number

    @pytest.mark.parametrize("name", [None, "", "Ma", "Mon", "Tuesday"])
    def test_anything_else_has_none(self, name: str | None) -> None:
        assert month_number(name) is None

    def test_there_are_twelve(self) -> None:
        assert len(MONTHS) == 12
        assert [month_number(name) for name in MONTHS] == list(range(1, 13))


class TestParseMoment:
    """The ISO 8601 reader of the adapters keeps the sign of an offset as it is written."""

    @pytest.mark.parametrize(
        ("text", "hours", "minutes"),
        [
            ("2027-01-29T23:59:00-12:00", -12, 0),
            ("2027-01-29T23:59-12", -12, 0),
            ("20270129T235900-1200", -12, 0),
            ("2027-01-29T23:59:00+05:30", 5, 30),
            ("2027-01-29T23:59:00+14:00", 14, 0),
            ("2027-01-29T23:59:00Z", 0, 0),
        ],
    )
    def test_an_offset_is_read_as_written(self, text: str, hours: int, minutes: int) -> None:
        moment = parse_moment(text)
        assert isinstance(moment, datetime)
        assert (moment.hour, moment.minute) == (23, 59)
        assert moment.utcoffset() == timedelta(hours=hours, minutes=minutes)

    @pytest.mark.parametrize(
        "text",
        ["2027-01-29 23:59 UTC-12", "2027-01-29T23:59:00 AoE", "Jan 29, 2027 23:59 AoE"],
    )
    def test_a_zone_in_words_is_left_to_normalization(self, text: str) -> None:
        # No ISO 8601: jsonld keeps the text, rss too when it is no RFC 822 date either,
        # and normalization reads it (seminars.pipeline.times.parse_moment).
        assert parse_moment(text) is None
