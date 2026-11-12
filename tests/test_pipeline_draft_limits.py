"""The limits of a draft keep everything normalization reads.

``seminars.models.draft`` cannot import the pipeline, so its numbers are
written out there; these tests tie each one to the limits of normalization
and show that cutting a text to it changes nothing normalization makes of
the text.
"""

from __future__ import annotations

import pytest

from conftest import REPO_ROOT
from factories import REFERENCE_NOW, make_draft, make_source, make_window
from seminars.config import load_settings
from seminars.models import (
    DRAFT_TEXT_LIMITS,
    MAX_DRAFT_AFFILIATION,
    MAX_DRAFT_CATEGORIES,
    MAX_DRAFT_CATEGORY,
    MAX_DRAFT_SPEAKER_NAME,
    MAX_DRAFT_SPEAKERS,
    MAX_DRAFT_TIME,
    MAX_DRAFT_URL,
)
from seminars.pipeline import normalize, times
from seminars.pipeline.normalize import NormalizeContext, normalize_drafts
from seminars.pipeline.text import RAW_FACTOR, one_line, paragraphs

SETTINGS = load_settings(REPO_ROOT / "config" / "settings.yaml")
READ = 4 * RAW_FACTOR
"""Raw characters normalization reads per character it stores (4 for scrubbing, 8 for markup)."""

LINE_FIELDS = {
    "title": normalize.MAX_TITLE,
    "series": normalize.MAX_SERIES,
    "host_org": normalize.MAX_HOST,
    "location": normalize.MAX_LOCATION,
}
BLOCK_FIELDS = {
    "speaker_text": normalize.MAX_SPEAKER_LINE,
    "abstract": SETTINGS.events.abstract_max_chars,
    "abstract_html": SETTINGS.events.abstract_max_chars,
}


def context() -> NormalizeContext:
    return NormalizeContext(settings=SETTINGS, now=REFERENCE_NOW, window=make_window())


def text_with_a_tag_across(limit: int) -> str:
    """Words and markup, with a tag that the raw read of ``limit`` cuts in two."""
    size = READ * limit
    head = ("<b>word</b> " * (size // 12 + 1))[: size - 4]
    return head + "<span class='x'>tail</span> more text " * 4


class TestNumbers:
    @pytest.mark.parametrize("name", sorted(LINE_FIELDS | BLOCK_FIELDS))
    def test_a_text_field_keeps_one_character_more_than_normalization_reads(
        self, name: str
    ) -> None:
        limit = (LINE_FIELDS | BLOCK_FIELDS)[name]
        assert DRAFT_TEXT_LIMITS[name] == READ * limit + 1

    def test_speakers_categories_and_addresses(self) -> None:
        assert MAX_DRAFT_SPEAKER_NAME == READ * normalize.MAX_NAME + 1
        assert MAX_DRAFT_AFFILIATION == READ * normalize.MAX_AFFILIATION + 1
        assert MAX_DRAFT_CATEGORY == READ * normalize.MAX_CATEGORY + 1
        assert MAX_DRAFT_SPEAKERS >= normalize.MAX_SPEAKERS
        assert MAX_DRAFT_CATEGORIES >= normalize.MAX_CATEGORIES
        assert MAX_DRAFT_URL == normalize.MAX_URL
        assert MAX_DRAFT_TIME == times.MAX_TEXT_LENGTH + 1

    def test_a_time_text_cut_to_its_limit_is_still_too_long_to_read(self) -> None:
        text = "Tuesday, November 17, 2026, 4:00 PM " * 10_000
        (invalid,) = normalize_drafts([make_draft(start=text)], make_source(), context()).invalid
        cut = make_draft(start=text).start
        assert isinstance(cut, str)
        assert len(cut) == MAX_DRAFT_TIME
        assert invalid.reason == "invalid"
        assert "too long" in (invalid.detail or "")


class TestReading:
    @pytest.mark.parametrize("name", sorted(LINE_FIELDS))
    def test_a_cut_line_field_reads_the_same(self, name: str) -> None:
        limit = LINE_FIELDS[name]
        text = text_with_a_tag_across(limit)
        cut = text[: DRAFT_TEXT_LIMITS[name]]
        assert one_line(cut, limit=4 * limit) == one_line(text, limit=4 * limit)

    @pytest.mark.parametrize("name", sorted(BLOCK_FIELDS))
    def test_a_cut_block_field_reads_the_same(self, name: str) -> None:
        limit = BLOCK_FIELDS[name]
        text = text_with_a_tag_across(limit)
        cut = text[: DRAFT_TEXT_LIMITS[name]]
        is_html = name == "abstract_html"
        assert paragraphs(cut, limit=4 * limit, is_html=is_html) == paragraphs(
            text, limit=4 * limit, is_html=is_html
        )

    def test_an_event_from_a_huge_draft_is_what_it_was(self) -> None:
        text = "A talk on knots and links. " * 100_000
        draft = make_draft(title=text, abstract=text, location=text, categories=[text] * 500)
        (candidate,) = normalize_drafts([draft], make_source(), context()).candidates
        event = candidate.event
        assert len(event.title) <= normalize.MAX_TITLE
        assert event.title.startswith("A talk on knots and links.")
        assert event.abstract is not None
        assert len(event.abstract) == SETTINGS.events.abstract_max_chars
        assert len(candidate.hints.categories) <= normalize.MAX_CATEGORIES
