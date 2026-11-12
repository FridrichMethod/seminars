"""Tests for the helpers the adapters share that no adapter test reaches on its own."""

from __future__ import annotations

import re

import pytest

from seminars.sources.generic.common import MONTHS, month_number, require_groups


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
