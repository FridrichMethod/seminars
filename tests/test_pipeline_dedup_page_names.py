"""Tests for the link between a page named after a speaker and a listing of that speaker.

A host can list a talk by its title alone, on a page named after the
speaker ("/events/breakthroughs/Mehnert.html"), while the campus calendar
names the speaker in a title that shares no word with the host's. The two
listings start at exactly the same moment, and the page is the speaker's.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from factories import PACIFIC, make_event
from seminars.config import DedupSettings
from seminars.models import Candidate, EventHints, Speaker
from seminars.pipeline.dedup import DedupResult, dedupe

SETTINGS = DedupSettings()
START = datetime(2026, 12, 8, 16, 0, tzinfo=PACIFIC)
END = START + timedelta(hours=1)
HOST_PAGE = "https://med.stanford.edu/cancer/about/events/breakthroughs/Mehnert.html"
CALENDAR_TITLE = "Stanford Cancer Institute Breakthroughs in Cancer: Janice Mehnert, MD"
HOST_TITLE = "Beyond Checkpoint Blockade: Vaccines and the Next Era of Melanoma Immunotherapy"


def calendar(**overrides: Any) -> Candidate:
    """The campus calendar's listing: the speaker in the title and as a speaker."""
    url = "https://events.stanford.edu/event/breakthroughs-in-cancer-janice-mehnert"
    values: dict[str, Any] = {
        "uid": "localist-stanford.54075329251149",
        "source_ids": ["localist-stanford:54075329251149"],
        "url": url,
        "source_urls": [url],
        "title": CALENDAR_TITLE,
        "speakers": [Speaker(name="Janice Mehnert", affiliation="MD")],
        "start": START,
        "end": END,
        "location": "Beckman Center, Munzer Auditorium",
        "fields": [],
        "abstract": None,
    }
    event = make_event(**{**values, **overrides})
    return Candidate(event=event, hints=EventHints(source_ids=["localist-stanford"]))


def host(**overrides: Any) -> Candidate:
    """The host's listing: the title of the talk on a page named after the speaker."""
    url = overrides.pop("url", HOST_PAGE)
    values: dict[str, Any] = {
        "uid": "sites-chemh.09e36756-51af-457f-97f8-7af08384d70e",
        "source_ids": ["sites-chemh:09e36756-51af-457f-97f8-7af08384d70e"],
        "url": url,
        "source_urls": ["https://chemh.stanford.edu/events"],
        "title": HOST_TITLE,
        "speakers": [],
        "start": START,
        "end": END,
        "location": None,
        "fields": [],
        "abstract": None,
    }
    event = make_event(**{**values, **overrides})
    return Candidate(event=event, hints=EventHints(source_ids=["sites-chemh"]))


def run(*candidates: Candidate) -> DedupResult:
    return dedupe(list(candidates), settings=SETTINGS, aggregators={"localist-stanford"})


class TestPageOfTheSpeaker:
    def test_a_page_named_after_the_speaker_at_the_same_start_is_one_talk(self) -> None:
        for order in ((calendar(), host()), (host(), calendar())):
            result = run(*order)
            assert len(result.candidates) == 1
            merged = result.candidates[0].event
            assert merged.title == HOST_TITLE
            assert str(merged.url) == HOST_PAGE
            assert merged.location == "Beckman Center, Munzer Auditorium"
            assert [speaker.name for speaker in merged.speakers] == ["Janice Mehnert"]

    def test_the_page_of_the_calendar_may_name_the_speaker_of_the_host(self) -> None:
        page = "https://events.stanford.edu/event/hai-seminar-with-mike-wooldridge-the-ai-100"
        listing = calendar(title="HAI Seminar The AI 100", speakers=[], url=page)
        talk = host(
            title="AI Goes Mainstream: The Scaling Era and the Rise of Generative AI",
            speakers=[Speaker(name="Michael Wooldridge")],
            url="https://hai.stanford.edu/events/hai-seminar-the-ai-100",
        )
        assert len(run(listing, talk).candidates) == 1

    def test_the_name_may_be_spelled_with_accents_and_escapes(self) -> None:
        speaker = Speaker(name="J\u00fcrgen M\u00fcller")
        page = "https://med.stanford.edu/events/breakthroughs/M%C3%BCller.html"
        assert len(run(calendar(speakers=[speaker]), host(url=page)).candidates) == 1

    @pytest.mark.parametrize(
        "change",
        [
            {"start": START + timedelta(minutes=5)},
            {"url": "https://med.stanford.edu/cancer/mehnert/events/breakthroughs.html"},
            {"url": "https://med.stanford.edu/cancer/events/Mehnertson.html"},
            {"url": "https://med.stanford.edu/cancer/events/Janice.html"},
        ],
        ids=["five-minutes-later", "name-in-a-directory", "longer-word", "first-name"],
    )
    def test_another_start_or_a_page_of_another_name_stays_apart(
        self, change: dict[str, Any]
    ) -> None:
        assert len(run(calendar(), host(**change)).candidates) == 2

    def test_a_speaker_that_is_no_person_names_no_page(self) -> None:
        department = [Speaker(name="Computer Science")]
        page = "https://cs.stanford.edu/events/science.html"
        assert len(run(calendar(speakers=department), host(url=page)).candidates) == 2

    def test_a_page_that_is_no_detail_page_names_nobody(self) -> None:
        listing = "https://med.stanford.edu/cancer/about/events/breakthroughs/Mehnert.html"
        result = dedupe(
            [calendar(), host()],
            settings=SETTINGS,
            aggregators={"localist-stanford"},
            listing_urls={listing},
        )
        assert len(result.candidates) == 2
