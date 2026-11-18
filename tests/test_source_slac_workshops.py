"""The SLAC workshops over their recording: five Indico category feeds read by the ics adapter.

The source keeps its meetings (workshops of several days) up to a year ahead
(``meeting_horizon``). What the fixture holds is described in
tests/fixtures/bayarea-slac-workshops/README.md.
"""

from __future__ import annotations

import functools
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import icalendar
import pytest

from factories import make_document, make_payload
from seminars.classify.filters import build_event_filter
from seminars.classify.rules import build_field_tagger
from seminars.config import Settings, load_settings, load_sources, load_taxonomy
from seminars.http import offline_client
from seminars.models import EventDraft, SourceConfig
from seminars.pipeline.normalize import NormalizeContext, Normalized, normalize_drafts
from seminars.sources.generic.ics import MAX_PATTERN_INPUT, IcsAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / "config"
SOURCE_ID = "bayarea-slac-workshops"
INDICO = "https://indico.slac.stanford.edu"
RECORDED = datetime(2026, 11, 18, 9, 50, 20, tzinfo=UTC)
"""The time of the recording."""
PACIFIC = ZoneInfo("America/Los_Angeles")

FEEDS = [f"{INDICO}/category/{category}/events.ics" for category in (170, 171, 20, 16, 19)]
"""RL4AA, SSRL Workshops, QIS Workshop, SLAC Summer Institute, ML@SLAC Workshop."""

RL4AA = "Reinforcement Learning for Autonomous Accelerators (RL4AA'27)"
SSRL = "Re-envisioning X-ray Science in the Age of SI and Automation"
QNEXT = "Q-NEXT All Hands 2027"


@functools.cache
def sources() -> dict[str, SourceConfig]:
    return {source.id: source for source in load_sources(CONFIG / "sources.d")}


@functools.cache
def settings() -> Settings:
    return load_settings(CONFIG / "settings.yaml")


@functools.cache
def recorded() -> tuple[tuple[str, ...], tuple[EventDraft, ...]]:
    """The requests and the drafts of the source over its recording, in its fetch window."""
    source = sources()[SOURCE_ID]
    http = offline_client(SOURCE_ID)
    adapter = IcsAdapter()
    window = settings().fetch_window_at(RECORDED, source)
    drafts = adapter.parse(adapter.fetch(http, source, window=window), source)
    return tuple(http.requests), tuple(drafts)


def by_title(drafts: tuple[EventDraft, ...], title: str) -> EventDraft:
    (draft,) = [draft for draft in drafts if draft.title == title]
    return draft


def normalized(source: SourceConfig, drafts: tuple[EventDraft, ...]) -> Normalized:
    context = NormalizeContext(
        settings=settings(), now=RECORDED, window=settings().window_at(RECORDED)
    )
    return normalize_drafts(list(drafts), source, context)


class TestConfiguration:
    def test_the_source_reads_the_workshop_categories_with_a_year_of_meetings(self) -> None:
        source = sources()[SOURCE_ID]
        assert source.enabled
        assert (source.group, source.adapter, source.institution) == ("bayarea", "ics", "slac")
        assert source.all_urls == FEEDS
        assert source.ignore_robots == ["indico.slac.stanford.edu"]
        assert source.default_fields == ["physics"]
        assert source.default_event_type == "workshop"
        assert source.meeting_horizon
        assert source.research is not None
        assert source.research.verified is not None

    def test_no_feed_of_the_host_is_read_twice_and_the_internal_meetings_are_left_out(
        self,
    ) -> None:
        on_host = [
            url
            for source in sources().values()
            for url in source.all_urls
            if url.startswith(f"{INDICO}/")
        ]
        assert len(on_host) == len(set(on_host))
        assert f"{INDICO}/category/129/events.ics" in on_host  # bayarea-slac-theory
        assert f"{INDICO}/category/14/events.ics" in on_host  # bayarea-slac-experimental
        # 5 takes in the weekly CIDeR-ML General Meetings of 110, with their Zoom links.
        for category in (5, 110):
            assert f"{INDICO}/category/{category}/events.ics" not in on_host

    def test_the_host_stays_within_its_request_budget(self) -> None:
        on_host = sum(
            url.startswith(f"{INDICO}/")
            for source in sources().values()
            if source.enabled
            for url in source.all_urls
        )
        assert on_host <= settings().http.max_requests_per_host


class TestRecordedFeeds:
    def test_one_request_per_category_and_one_draft_per_event(self) -> None:
        requests, drafts = recorded()
        assert list(requests) == FEEDS
        assert [draft.title for draft in drafts] == [RL4AA, SSRL, QNEXT]
        assert all(draft.source_id == SOURCE_ID for draft in drafts)

    def test_a_workshop_of_four_days_keeps_its_times_place_and_page(self) -> None:
        _, drafts = recorded()
        draft = by_title(drafts, RL4AA)
        assert draft.native_id == "indico-event-10793@indico.slac.stanford.edu"
        assert draft.start == datetime(2027, 4, 20, 13, 0, tzinfo=PACIFIC)
        assert draft.end == datetime(2027, 4, 23, 17, 30, tzinfo=PACIFIC)
        assert draft.location == "SLAC"
        assert draft.url == f"{INDICO}/event/10793/"

    def test_the_abstract_is_the_description_without_the_event_address(self) -> None:
        _, drafts = recorded()
        draft = by_title(drafts, RL4AA)
        assert draft.abstract is not None
        assert draft.abstract.startswith("Page under construction, final details to come!\n")
        assert draft.abstract.endswith("Please register early to secure your spot.")
        assert draft.speakers == []
        assert draft.speaker_text is None

    def test_the_speaker_line_of_a_workshop_stays_out_of_the_abstract(self) -> None:
        _, drafts = recorded()
        draft = by_title(drafts, SSRL)
        assert draft.abstract is not None
        assert draft.abstract.startswith("Superintelligence (SI), capable of reasoning")
        assert draft.abstract.endswith(
            "How can SI lower barriers for new users and new areas of science?"
        )
        assert "Speakers:" not in draft.abstract
        assert "Parkinson" not in draft.abstract
        assert f"{INDICO}/event/" not in draft.abstract
        assert draft.speakers == []
        assert draft.location == "53/2-2002 - Berryessa (SLAC)"

    def test_a_meeting_with_a_speaker_line_only_has_no_abstract(self) -> None:
        _, drafts = recorded()
        draft = by_title(drafts, QNEXT)
        assert draft.abstract is None
        assert draft.speakers == []
        assert draft.location == "51/1-102 - Kavli Auditorium (SLAC)"


class TestMeetings:
    def test_every_workshop_is_a_meeting_and_the_one_in_april_is_kept(self) -> None:
        _, drafts = recorded()
        result = normalized(sources()[SOURCE_ID], drafts)
        assert result.outside_window == 0
        events = {candidate.event.title: candidate.event for candidate in result.candidates}
        assert set(events) == {RL4AA, SSRL, QNEXT}
        assert all(event.event_type == "workshop" and event.is_meeting for event in events.values())
        assert not all(event.all_day for event in events.values())
        # RL4AA'27 starts after the window of talks; the meeting horizon keeps it.
        assert events[RL4AA].start >= settings().window_at(RECORDED).end
        assert events[RL4AA].start == datetime(2027, 4, 20, 13, 0, tzinfo=PACIFIC)

    def test_without_the_meeting_horizon_the_workshop_in_april_is_dropped(self) -> None:
        _, drafts = recorded()
        source = sources()[SOURCE_ID].evolve(meeting_horizon=False)
        result = normalized(source, drafts)
        assert result.outside_window == 1
        assert {candidate.event.title for candidate in result.candidates} == {SSRL, QNEXT}

    def test_the_all_hands_meeting_of_a_center_is_excluded_as_administrative(self) -> None:
        _, drafts = recorded()
        candidates = normalized(sources()[SOURCE_ID], drafts).candidates
        event_filter = build_event_filter(load_taxonomy(CONFIG / "taxonomy.yaml"), settings())
        result = event_filter.filter(candidates)
        assert [(item.title, item.reason) for item in result.excluded] == [
            (QNEXT, "administrative")
        ]
        assert {candidate.event.title for candidate in result.kept} == {RL4AA, SSRL}

    def test_the_fields_come_from_the_source_and_the_talk_not_from_speaker_names(self) -> None:
        _, drafts = recorded()
        candidates = normalized(sources()[SOURCE_ID], drafts).candidates
        tagger = build_field_tagger(load_taxonomy(CONFIG / "taxonomy.yaml"))
        fields = {
            candidate.event.title: tagger.tag(candidate).fields
            for candidate in candidates
            if candidate.event.title != QNEXT
        }
        # The speaker line of the SSRL workshop names Dula Parkinson, a keyword of med.
        assert fields == {RL4AA: ["ai-ml", "physics"], SSRL: ["physics"]}


HYBRID_FEED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//CERN//INDICO//EN
BEGIN:VEVENT
SUMMARY:Hybrid Workshop on Detectors
DTSTART:20270210T163000Z
DTEND:20270212T010000Z
UID:indico-event-99999@indico.slac.stanford.edu
DESCRIPTION:Speakers: Jane Doe (SLAC)\\n\\nTwo days of talks on detectors.\\nJ
 oin Zoom Meeting\\nhttps://stanford.zoom.us/j/99887766554?pwd=SECRETpwdAbC123
 xyz\\nMeeting ID: 998 8776 6554\\nPasscode: 731946\\n\\nhttps://indico.slac.sta
 nford.edu/event/99999/
LOCATION:51/1-102 - Kavli Auditorium (SLAC)
URL:https://indico.slac.stanford.edu/event/99999/
END:VEVENT
END:VCALENDAR
"""
"""An Indico export of a hybrid workshop whose description holds its Zoom meeting."""


def test_the_zoom_meeting_of_a_hybrid_workshop_loses_its_credentials() -> None:
    source = sources()[SOURCE_ID]
    document = make_document(FEEDS[0], HYBRID_FEED, content_type="text/calendar")
    payload = make_payload(document, source_id=SOURCE_ID, fetched_at=RECORDED)
    (draft,) = IcsAdapter().parse(payload, source)
    assert draft.abstract is not None
    assert "pwd=SECRETpwdAbC123xyz" in draft.abstract  # the adapter copies the source
    (candidate,) = normalized(source, (draft,)).candidates
    abstract = candidate.event.abstract
    assert abstract is not None
    assert abstract.startswith("Two days of talks on detectors.")
    assert "SECRETpwdAbC123xyz" not in abstract
    assert "731946" not in abstract
    assert "https://stanford.zoom.us/j/99887766554" in abstract
    assert "Meeting ID: 998 8776 6554" in abstract


def one_workshop(description: str) -> EventDraft:
    """The draft of an Indico export of one workshop whose DESCRIPTION is ``description``."""
    event = icalendar.Event()
    event.add("summary", "Workshop on Detectors")
    event.add("dtstart", datetime(2027, 2, 10, 16, 30, tzinfo=UTC))
    event.add("dtend", datetime(2027, 2, 12, 1, 0, tzinfo=UTC))
    event.add("uid", "indico-event-99998@indico.slac.stanford.edu")
    event.add("description", description)
    event.add("url", f"{INDICO}/event/99998/")
    calendar = icalendar.Calendar()
    calendar.add("version", "2.0")
    calendar.add("prodid", "-//CERN//INDICO//EN")
    calendar.add_component(event)
    text = calendar.to_ical().decode()
    source = sources()[SOURCE_ID]
    document = make_document(FEEDS[0], text, content_type="text/calendar")
    payload = make_payload(document, source_id=SOURCE_ID, fetched_at=RECORDED)
    (draft,) = IcsAdapter().parse(payload, source)
    return draft


SPEAKERS = "Speakers: Jane Doe (SLAC), John Roe (Stanford University)"
TEXT = "Two days of talks on detectors.\n\nRegistration is free."
ADDRESS = f"{INDICO}/event/99998/"


@pytest.mark.parametrize(
    ("description", "abstract"),
    [
        (f"{SPEAKERS}\n\n{TEXT}\n\n{ADDRESS}", TEXT),
        (f"{TEXT}\n\n{ADDRESS}\n", TEXT),
        (f"{SPEAKERS}\n\n{TEXT}", TEXT),
        (TEXT, TEXT),
        (f"{SPEAKERS}\n\n{ADDRESS}", None),
        (ADDRESS, None),
        (SPEAKERS, None),
    ],
    ids=[
        "speakers-text-address",
        "text-address",
        "speakers-text",
        "text",
        "speakers-address",
        "address",
        "speakers",
    ],
)
def test_the_abstract_is_the_description_without_the_speaker_line_and_the_address(
    description: str, abstract: str | None
) -> None:
    assert one_workshop(description).abstract == abstract


def test_a_description_longer_than_the_adapter_reads_keeps_its_beginning() -> None:
    sentence = "A workshop on the detectors of the next collider. "
    text = sentence * (MAX_PATTERN_INPUT // len(sentence) + 10)
    assert len(text) > MAX_PATTERN_INPUT
    draft = one_workshop(f"{SPEAKERS}\n\n{text}\n\n{ADDRESS}")
    assert draft.abstract is not None
    assert draft.abstract.startswith(sentence)
    assert "Speakers:" not in draft.abstract
    (candidate,) = normalized(sources()[SOURCE_ID], (draft,)).candidates
    abstract = candidate.event.abstract
    assert abstract is not None
    assert abstract.startswith(sentence)
    assert len(abstract) <= settings().events.abstract_max_chars
