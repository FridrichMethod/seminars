"""Tests for placeholders written in Chinese, Japanese or Korean.

A source that writes its titles, speakers or abstracts in Chinese says
"to be determined" in Chinese as well. Such a placeholder is no title, no
speaker and no abstract, alone, after a word for "title" or "abstract",
or next to its English counterpart ("TBA (to be determined)").
"""

from __future__ import annotations

import pytest

from factories import make_draft
from seminars.models import TBA_TITLE, SpeakerDraft
from seminars.pipeline.text import is_empty_abstract, is_tba
from test_pipeline_normalize import one

TO_BE_DETERMINED = "\u5f85\u5b9a"
PLACEHOLDERS = [
    TO_BE_DETERMINED,
    "\u672a\u5b9a",
    "\u5f85\u516c\u5e03",
    "\u5f85\u786e\u5b9a",
    "\u5f85\u78ba\u5b9a",
    "\u53e6\u884c\u901a\u77e5",
    "\u9898\u76ee\u5f85\u5b9a",
    "\u9898\u76ee\uff1a\u5f85\u5b9a",
    "\u984c\u76ee\u672a\u5b9a",
    "\u62a5\u544a\u9898\u76ee\u5f85\u5b9a",
    "\u6807\u9898\u5f85\u5b9a",
    "\u30bf\u30a4\u30c8\u30eb\u672a\u5b9a",
    "\ubbf8\uc815",
    "\uc81c\ubaa9 \ubbf8\uc815",
    "TBA\uff08\u5f85\u5b9a\uff09",
    "TBA / \u5f85\u5b9a",
    "\u5f85\u5b9a (TBD)",
    "Title: \u5f85\u5b9a",
]


@pytest.mark.parametrize("title", PLACEHOLDERS)
def test_a_placeholder_in_cjk_is_no_title(title: str) -> None:
    assert is_tba(title)


@pytest.mark.parametrize(
    "title",
    [
        "\u86cb\u767d\u8d28\u8bbe\u8ba1\u7684\u65b0\u65b9\u6cd5",
        "\u5f85\u5b9a\u7684\u86cb\u767d\u8d28\u7ed3\u6784",
        "\u5173\u4e8e\u672a\u5b9a\u5f0f\u7684\u7814\u7a76",
        "TBA \u86cb\u767d\u8d28\u8bbe\u8ba1",
        "\u9898\u76ee",
    ],
)
def test_a_title_that_holds_a_placeholder_word_is_a_title(title: str) -> None:
    assert not is_tba(title)


@pytest.mark.parametrize(
    "text",
    [
        TO_BE_DETERMINED,
        "\u6458\u8981",
        "\u6458\u8981\uff1a",
        "\u6458\u8981\uff1a\u5f85\u5b9a",
        "\u6458\u8981\u5f85\u5b9a",
        "\u7b80\u4ecb\uff1a\u6682\u65e0",
        "\u6682\u65e0",
        "\u66ab\u7121\u6458\u8981",
        "\u65e0",
    ],
)
def test_an_abstract_that_is_a_placeholder_in_cjk_says_nothing(text: str) -> None:
    assert is_empty_abstract(text)


@pytest.mark.parametrize(
    "text",
    [
        "\u6458\u8981\uff1a\u672c\u62a5\u544a\u4ecb\u7ecd\u86cb\u767d\u8d28\u8bbe\u8ba1\u7684\u65b0\u65b9\u6cd5\u3002",
        "\u65e0\u5e8f\u86cb\u767d\u8d28\u7684\u7ed3\u6784",
        "\u7b80\u4ecb protein design",
    ],
)
def test_an_abstract_in_cjk_that_says_something_is_kept(text: str) -> None:
    assert not is_empty_abstract(text)


def test_a_title_that_is_a_placeholder_in_cjk_is_stored_as_tba() -> None:
    draft = make_draft(title="\u9898\u76ee\u5f85\u5b9a", speaker_text="\u5f20\u4e09")
    assert one(draft).event.title == TBA_TITLE


@pytest.mark.parametrize("name", [TO_BE_DETERMINED, "TBA\uff08\u5f85\u5b9a\uff09"])
def test_a_speaker_that_is_a_placeholder_in_cjk_is_dropped(name: str) -> None:
    draft = make_draft(
        speakers=[SpeakerDraft(name=name), SpeakerDraft(name="\u738b\u5c0f\u660e")],
        speaker_text=None,
    )
    assert [s.name for s in one(draft).event.speakers] == ["\u738b\u5c0f\u660e"]


@pytest.mark.parametrize(
    "line",
    [
        TO_BE_DETERMINED,
        "\u8bb2\u8005\uff1a\u5f85\u5b9a",
        "\u4e3b\u8bb2\u4eba: \u5f85\u5b9a",
        "\u5f85\u5b9a\uff08\u6e05\u534e\u5927\u5b66\uff09",
    ],
)
def test_a_speaker_line_that_is_a_placeholder_in_cjk_names_nobody(line: str) -> None:
    assert one(make_draft(speaker_text=line)).event.speakers == []


@pytest.mark.parametrize(
    "line", ["\u8bb2\u8005\uff1a\u5f20\u4e09", "\u62a5\u544a\u4eba: \u5f20\u4e09"]
)
def test_a_speaker_label_in_chinese_is_no_part_of_the_name(line: str) -> None:
    speakers = one(make_draft(speaker_text=line)).event.speakers
    assert [(s.name, s.affiliation) for s in speakers] == [("\u5f20\u4e09", None)]
