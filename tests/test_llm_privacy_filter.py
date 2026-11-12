"""The privacy filter reads text the way normalization does before it scrubs it.

Normalization decodes entities and cleans the characters of a text field
before it scrubs it, so the LLM layer, which caches page text that was never
normalized, has to do the same. Every private value here is made up.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import seminars.llm
from seminars.contracts import IMPLEMENTATIONS
from seminars.llm.grounding import Grounding
from seminars.llm.privacy_filter import (
    MAX_DECODING_PASSES,
    PrivacyFilter,
    project_privacy_filter,
    project_scrubber,
    readable,
)
from seminars.pipeline.privacy import scrub_private
from seminars.pipeline.text import one_line, paragraphs

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


def tag_scrubber(text: str) -> str:
    """A scrubber for ``IMPLEMENTATIONS``: takes the word "secret" out."""
    return text.replace("secret", "[x]")


class TestTheProjectFilter:
    def test_it_uses_the_scrubber_of_normalization(self) -> None:
        assert project_privacy_filter().scrub is scrub_private
        assert project_scrubber() is scrub_private

    def test_the_scrubber_comes_from_its_contract_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(IMPLEMENTATIONS, "private_scrubber", f"{__name__}:tag_scrubber")
        assert project_scrubber() is tag_scrubber
        assert project_privacy_filter().clean("a secret&#33;") == "a [x]!"

    def test_no_module_of_the_llm_layer_imports_the_pipeline(self) -> None:
        package = Path(seminars.llm.__file__).parent
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    assert not name.startswith("seminars.pipeline"), f"{path.name}: {name}"


class TestGroundingHas:
    PAGE = "Talk: How keys leak [[1]] from public code\nRoom 104"

    @pytest.mark.parametrize("value", ["how KEYS leak", "leak from  public", "Room 104"])
    def test_a_part_of_the_page_up_to_case_spacing_and_link_markers(self, value: str) -> None:
        for similarity in (0, 90, 100):
            assert Grounding(self.PAGE, min_similarity=similarity).has(value)

    @pytest.mark.parametrize("value", ["How keys leek from public code", "", "   ", "Room 105"])
    def test_anything_else_whatever_the_similarity(self, value: str) -> None:
        grounding = Grounding(self.PAGE, min_similarity=0)
        assert not grounding.has(value)


class TestWhiteSpaceOfNormalization:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("a  b\t\tc", "a b c"),
            ("  first line  \n\n\n  second   line  ", "first line\nsecond line"),
            ("Passcode:\n\n\n\n\n482913", "Passcode:\n482913"),
            ("a\u00a0\u00a0\u00a0b", "a b"),
        ],
    )
    def test_readable_collapses_white_space_in_each_line(self, text: str, expected: str) -> None:
        assert readable(text) == expected

    def test_it_reads_a_block_as_normalization_does(self) -> None:
        for text in ("Zoom passcode:\n\n\n\n482913", "a  b\n\n  c   d ", "x\u00a0 \u00a0y"):
            assert readable(text) == paragraphs(text, limit=200)

    @pytest.mark.parametrize(
        "text",
        [
            "Zoom passcode:" + "\n" * 5 + "482913",
            "Zoom passcode:" + " " * 6 + "482913",
            "Mail jane.doe @" + " " * 5 + "example.edu",
            "Passcode:" + " \n " * 3 + "482913",
            "Mail jane.doe\n" + " " * 4 + "@example.edu",
        ],
    )
    def test_private_data_split_by_white_space_is_found(self, text: str) -> None:
        assert scrub_private(text) == text, "the scrubber alone misses it"
        assert FILTER.hides(text)
        assert FILTER.clean(text) != readable(text)

    def test_private_data_that_only_a_line_field_joins_is_found(self) -> None:
        # A line field (a title, a place) joins its lines; a block keeps them apart.
        text = "Contact Jane Doe\nat 723-" + "1234"
        assert scrub_private(text) == text
        assert FILTER.clean(text) == readable(text), "the block form shows nothing"
        joined = one_line(text, limit=200)
        assert joined == "Contact Jane Doe at 723-" + "1234"
        assert scrub_private(joined) != joined
        assert FILTER.hides(text)
