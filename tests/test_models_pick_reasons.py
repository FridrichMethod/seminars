"""Tests for reading a picks reason back from the line the picks scorer stored."""

from __future__ import annotations

import pytest

from seminars.models import PickReason


class TestParse:
    @pytest.mark.parametrize(
        "text",
        ['"x" in title (+1e999)', 'speaker "Ann Smith" (-1e400)', 'penalty "x" (+1e309)'],
    )
    def test_a_line_of_a_known_form_with_points_that_overflow_is_kept_as_it_is(
        self, text: str
    ) -> None:
        assert PickReason.parse(text) == PickReason(kind="other", term=text)

    def test_a_line_of_a_known_form_is_taken_apart(self) -> None:
        reason = PickReason.parse('"protein design" in abstract (+1.5)')
        assert reason == PickReason(
            kind="keyword", term="protein design", place="abstract", points=1.5
        )
        assert reason.text == '"protein design" in abstract (+1.5)'
