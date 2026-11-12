"""A draft keeps a bounded amount of text, however long the page and however many events share it.

An adapter copies what a page says into each of its events, and many events
can share one text (a detail page that several listings point at, a shared
list of topics). The limits of ``EventDraft`` make the size of every draft
independent of the page. The numbers are tied to what normalization reads
in tests/test_pipeline_draft_limits.py.
"""

from __future__ import annotations

import tracemalloc
from datetime import UTC, datetime

import pytest

from factories import make_draft
from seminars.models import (
    DRAFT_TEXT_LIMITS,
    MAX_DRAFT_AFFILIATION,
    MAX_DRAFT_CATEGORIES,
    MAX_DRAFT_CATEGORY,
    MAX_DRAFT_CHARACTERS,
    MAX_DRAFT_NATIVE_ID,
    MAX_DRAFT_SPEAKER_NAME,
    MAX_DRAFT_SPEAKERS,
    MAX_DRAFT_TIME,
    MAX_DRAFT_URL,
    EventDraft,
    SpeakerDraft,
)

SHARED = "word " * 400_000
"""Two million characters, the size of a large page."""

LONG = "word " * 60_000
"""Three hundred thousand characters, more than any field of a draft keeps."""


def fresh(text: str) -> str:
    """A copy of ``text`` that is a new object, as an adapter makes when it reads a page."""
    return (text + "x")[:-1]


def long_url(length: int) -> str:
    base = "https://example.edu/"
    return base + "a" * (length - len(base))


class TestText:
    @pytest.mark.parametrize("name", sorted(DRAFT_TEXT_LIMITS))
    def test_a_long_text_field_is_cut_to_its_limit(self, name: str) -> None:
        draft = make_draft(**{name: SHARED})
        assert getattr(draft, name) == SHARED[: DRAFT_TEXT_LIMITS[name]]

    @pytest.mark.parametrize("name", sorted(DRAFT_TEXT_LIMITS))
    def test_a_text_within_its_limit_is_kept_as_the_same_object(self, name: str) -> None:
        text = fresh("A talk " * 10)
        draft = make_draft(**{name: text})
        assert getattr(draft, name) is text

    def test_a_speaker_keeps_a_bounded_name_and_affiliation(self) -> None:
        speaker = SpeakerDraft(name=SHARED, affiliation=SHARED)
        assert speaker.name == SHARED[:MAX_DRAFT_SPEAKER_NAME]
        assert speaker.affiliation == SHARED[:MAX_DRAFT_AFFILIATION]
        draft = make_draft(speakers=[{"name": SHARED, "affiliation": SHARED}])
        assert len(draft.speakers[0].name) == MAX_DRAFT_SPEAKER_NAME

    def test_a_category_is_cut_to_its_limit(self) -> None:
        draft = make_draft(categories=["Physics", SHARED])
        assert draft.categories == ["Physics", SHARED[:MAX_DRAFT_CATEGORY]]

    def test_a_tag_is_cut_like_a_category(self) -> None:
        draft = make_draft(tags=["physics", SHARED])
        assert draft.tags == ["physics", SHARED[:MAX_DRAFT_CATEGORY]]


class TestLists:
    def test_speakers_and_categories_keep_their_first_items(self) -> None:
        speakers = [{"name": f"Speaker {number}"} for number in range(5000)]
        categories = [f"Topic {number}" for number in range(5000)]
        draft = make_draft(speakers=speakers, categories=categories)
        assert [speaker.name for speaker in draft.speakers] == [
            f"Speaker {number}" for number in range(MAX_DRAFT_SPEAKERS)
        ]
        assert draft.categories == categories[:MAX_DRAFT_CATEGORIES]

    def test_tags_keep_their_first_items(self) -> None:
        tags = [f"tag {number}" for number in range(5000)]
        assert make_draft(tags=tags).tags == tags[:MAX_DRAFT_CATEGORIES]
        assert len(make_draft(tags=(f"t{n}" for n in range(10**9))).tags) == MAX_DRAFT_CATEGORIES

    def test_a_tuple_or_an_iterator_is_cut_as_well(self) -> None:
        draft = make_draft(categories=(f"t{number}" for number in range(10**9)))
        assert len(draft.categories) == MAX_DRAFT_CATEGORIES
        speakers = tuple(SpeakerDraft(name=f"S{number}") for number in range(100))
        assert len(make_draft(speakers=speakers).speakers) == MAX_DRAFT_SPEAKERS

    def test_short_lists_are_kept(self) -> None:
        draft = make_draft(categories=["Physics", "Colloquium"], speakers=[{"name": "Jane Doe"}])
        assert draft.categories == ["Physics", "Colloquium"]
        assert [speaker.name for speaker in draft.speakers] == ["Jane Doe"]


class TestAddresses:
    def test_a_long_address_counts_as_missing(self) -> None:
        draft = make_draft(
            url=long_url(MAX_DRAFT_URL + 1),
            stream_url=long_url(MAX_DRAFT_URL + 1),
            speakers=[{"name": "Jane Doe", "url": long_url(MAX_DRAFT_URL + 1)}],
        )
        assert (draft.url, draft.stream_url, draft.speakers[0].url) == (None, None, None)

    def test_an_address_at_the_limit_is_kept(self) -> None:
        address = long_url(MAX_DRAFT_URL)
        draft = make_draft(url=address, stream_url=address)
        assert (draft.url, draft.stream_url) == (address, address)

    def test_a_long_native_id_counts_as_missing(self) -> None:
        assert make_draft(native_id="7" * (MAX_DRAFT_NATIVE_ID + 1)).native_id is None
        assert make_draft(native_id="7" * MAX_DRAFT_NATIVE_ID).native_id == "7" * 2000

    def test_a_bad_short_address_is_still_an_error(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            make_draft(url="/events/1")


class TestStability:
    def test_evolving_a_draft_keeps_it_as_it_is(self) -> None:
        draft = make_draft(title=SHARED, categories=[SHARED] * 300)
        again = draft.evolve(series="Seminar")
        assert again.title == draft.title
        assert again.categories == draft.categories
        assert EventDraft.model_validate_json(draft.model_dump_json()) == draft


class TestTimes:
    def test_a_long_time_text_is_cut_and_stays_too_long_to_read(self) -> None:
        draft = make_draft(start=SHARED, end=SHARED)
        assert draft.start == SHARED[:MAX_DRAFT_TIME]
        assert draft.end == SHARED[:MAX_DRAFT_TIME]

    def test_a_short_time_text_and_a_typed_time_are_kept(self) -> None:
        start = datetime(2026, 11, 17, 16, 0, tzinfo=UTC)
        assert make_draft(start=start).start == start
        assert make_draft(start="Nov 17, 2026 4:00 PM").start == "Nov 17, 2026 4:00 PM"


def full_draft() -> EventDraft:
    """A draft with every field at its limit, cut from a long text, with speakers of its own."""
    speakers = [
        {"name": LONG, "affiliation": LONG, "url": fresh(long_url(MAX_DRAFT_URL))}
        for _ in range(MAX_DRAFT_SPEAKERS + 10)
    ]
    values: dict[str, object] = {
        "source_id": "custom-hai-events",
        "extraction": "llm",
        "native_id": fresh("7" * MAX_DRAFT_NATIVE_ID),
        "url": fresh(long_url(MAX_DRAFT_URL)),
        "stream_url": fresh(long_url(MAX_DRAFT_URL)),
        "start": LONG,
        "end": LONG,
        "speakers": speakers,
        "categories": [LONG] * (MAX_DRAFT_CATEGORIES + 10),
        "tags": [LONG] * (MAX_DRAFT_CATEGORIES + 10),
    }
    return EventDraft.model_validate(values | dict.fromkeys(DRAFT_TEXT_LIMITS, LONG))


def characters(draft: EventDraft) -> int:
    """The characters of the text, times, addresses and lists of ``draft``."""
    texts = [getattr(draft, name) for name in DRAFT_TEXT_LIMITS]
    texts += [draft.start, draft.end, draft.url, draft.stream_url, draft.native_id]
    texts += [*draft.categories, *draft.tags]
    for speaker in draft.speakers:
        texts += [speaker.name, speaker.affiliation, speaker.url]
    return sum(len(text) for text in texts if isinstance(text, str))


class TestMemory:
    COUNT = 20
    OVERHEAD = 64 * 1024
    """Bytes a draft takes beyond its characters: objects, lists, string headers."""

    def test_the_ceiling_is_the_sum_of_the_limits(self) -> None:
        draft = full_draft()
        assert characters(draft) == MAX_DRAFT_CHARACTERS
        assert MAX_DRAFT_CHARACTERS < 2_000_000

    def test_drafts_with_every_field_full_stay_within_the_ceiling(self) -> None:
        """Every draft has its own copies and its own speakers; ASCII takes a byte a character."""
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            drafts = [full_draft() for _ in range(self.COUNT)]
            used = tracemalloc.get_traced_memory()[0] - before
        finally:
            tracemalloc.stop()
        assert len({id(draft.speakers[0].name) for draft in drafts}) == self.COUNT
        assert all(characters(draft) == MAX_DRAFT_CHARACTERS for draft in drafts)
        assert used < self.COUNT * (MAX_DRAFT_CHARACTERS + self.OVERHEAD)
        assert used > self.COUNT * MAX_DRAFT_CHARACTERS

    def test_a_thousand_drafts_that_share_a_long_text_stay_small(self) -> None:
        """Copies of a long text and long lists add nothing beyond the limits of a field."""
        topics = [fresh("Topic") for _ in range(50)] * 200
        speaker = SpeakerDraft(name=fresh(SHARED))
        start = datetime(2026, 11, 17, 16, 0, tzinfo=UTC)
        kept = sum(DRAFT_TEXT_LIMITS[name] for name in ("title", "location", "series", "host_org"))
        bound = 1000 * (kept + MAX_DRAFT_CATEGORY + self.OVERHEAD)
        tracemalloc.start()
        try:
            drafts = [
                EventDraft(
                    source_id="custom-hai-events",
                    extraction="html",
                    start=start,
                    title=fresh(SHARED),
                    location=fresh(SHARED),
                    series=fresh(SHARED),
                    host_org=fresh(SHARED),
                    categories=[*topics, fresh(SHARED)],
                    speakers=[speaker] * 5000,
                    native_id=str(number),
                )
                for number in range(1000)
            ]
            assert tracemalloc.get_traced_memory()[0] < bound
        finally:
            tracemalloc.stop()
        for draft in drafts:
            assert len(draft.categories) == MAX_DRAFT_CATEGORIES
            assert len(draft.speakers) == MAX_DRAFT_SPEAKERS
            assert draft.title is not None
            assert len(draft.title) == DRAFT_TEXT_LIMITS["title"]
