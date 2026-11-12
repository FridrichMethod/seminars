"""The privacy filter reads text the way normalization does before it scrubs it.

Normalization decodes entities and cleans the characters of a text field
before it scrubs it, so the LLM layer, which caches page text that was never
normalized, has to do the same. Every private value here is made up.
"""

from __future__ import annotations

import pytest

from seminars.llm.privacy_filter import (
    MAX_DECODING_PASSES,
    PrivacyFilter,
    project_privacy_filter,
    project_scrubber,
    readable,
)
from seminars.pipeline.privacy import scrub_private
from seminars.pipeline.text import one_line

FILTER = PrivacyFilter(scrub_private)


class TestReadable:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("jane.doe&#64;example.edu", "jane.doe@example.edu"),
            ("jane.doe&amp;#64;example.edu", "jane.doe@example.edu"),
            ("R&amp;D", "R&D"),
            ("jane.doe\u200b@example.edu", "jane.doe@example.edu"),
            ("(650)\u00a0555\u00ad-0142", "(650) 555-0142"),
            ("(650)&nbsp;555-0142", "(650) 555-0142"),
        ],
    )
    def test_entities_are_decoded_and_characters_cleaned(self, text: str, expected: str) -> None:
        assert readable(text) == expected

    def test_it_reads_a_value_as_normalization_does(self) -> None:
        for text in ("jane.doe&#64;example.edu", "a\u00a0b\u200bc", "Talk &ndash; part 2"):
            assert one_line(readable(text), limit=200) == one_line(text, limit=200)

    def test_markup_stays(self) -> None:
        text = "Inner products <x, y> and <b>bold</b> claims"
        assert readable(text) == text

    def test_entities_are_decoded_a_bounded_number_of_times(self) -> None:
        nested = "&" + "amp;" * (MAX_DECODING_PASSES + 1)
        assert readable(nested) == "&amp;"

    def test_text_without_entities_or_odd_characters_is_unchanged(self) -> None:
        text = "AT&T and R & D, 3 < 4 > 2; Room 104\nSecond line"
        assert readable(text) == text


class TestClean:
    def test_an_address_written_with_an_entity_is_scrubbed(self) -> None:
        assert FILTER.clean("Mail jane.doe&#64;example.edu") == "Mail [email removed]"
        assert scrub_private("Mail jane.doe&#64;example.edu") == "Mail jane.doe&#64;example.edu"

    def test_a_phone_number_with_a_soft_hyphen_is_scrubbed(self) -> None:
        assert FILTER.clean("Call (650) 555\u00ad-0142") == "Call [phone removed]"


class TestHides:
    @pytest.mark.parametrize(
        "text",
        [
            "Stanford University, jane.doe&#64;example.edu",
            "Stanford University, jane.doe&amp;#64;example.edu",
            "Stanford University, jane.doe<b></b>@example.edu",
            "Stanford University, (650) <span>555</span>-0142",
            "Stanford University, jane.doe\u200b@example.edu",
            "Zoom passcode: 482913",
        ],
    )
    def test_private_data_in_any_form_normalization_reads(self, text: str) -> None:
        assert FILTER.hides(text)

    def test_a_tag_that_normalization_reads_as_a_line_break(self) -> None:
        text = "Contact<br>jdoe at stanford.edu"
        assert scrub_private(text.replace("<br>", "")) == text.replace("<br>", "")
        assert FILTER.hides(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Stanford University",
            "Inner products <x, y> in Hilbert spaces",
            "Questions: [email removed] or [phone removed]. Zoom passcode: [removed]",
            "Join at https://stanford.zoom.us/j/123",
            "",
            "   ",
        ],
    )
    def test_text_without_private_data(self, text: str) -> None:
        assert not FILTER.hides(text)


class TestTheProjectFilter:
    def test_it_uses_the_scrubber_of_normalization(self) -> None:
        assert project_privacy_filter().scrub is scrub_private
        assert project_scrubber() is scrub_private
