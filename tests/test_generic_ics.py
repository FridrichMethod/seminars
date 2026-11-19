"""The ics adapter: reading feeds, time conventions and the YAML-driven fields.

The recurring events are in test_generic_ics_recurrence.py.
"""

from __future__ import annotations

import functools
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from factories import make_document, make_payload, make_source, make_window
from fakes import FakeHttpClient
from seminars.config import ConfigError
from seminars.config.loader import load_sources
from seminars.http import offline_client
from seminars.models import EventDraft, SourceConfig, TimeWindow
from seminars.outputs.health import allows_empty
from seminars.sources import base
from seminars.sources.base import ParseError
from seminars.sources.generic import ics
from seminars.sources.generic.ics import (
    MAX_PATTERN_INPUT,
    IcsAdapter,
    IcsOccurrence,
    ics_params,
    occurrence_draft,
    occurrences,
    plain_text,
    property_texts,
    read_calendar,
    vevent_draft,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PACIFIC = ZoneInfo("America/Los_Angeles")
RECORDED = datetime(2026, 11, 11, 16, 16, tzinfo=UTC)
FEED = "https://calendar.example.edu/feeds/talks.ics"

TOPOS = "https://researchseminars.org/seminar/ToposInstituteColloquium/ics"
AGSTANFORD = "https://researchseminars.org/seminar/agstanford/ics"

SERIES_FEED_PARAMS: dict[str, Any] = {
    # Exercises the generic machinery on a recorded series feed: SUMMARY is the speaker, the
    # URL properties are the livestreams, and the title and the room are in DESCRIPTION.
    # LOCATION -> url only shows that a property can fill another field. It is no recipe for
    # researchseminars.org: there LOCATION holds the talk page today but will hold the
    # livestream link, passcode included, once the site's current code is deployed, so the
    # sources of that site use their own adapter, which builds the talk page from the UID.
    "property_fields": {
        "SUMMARY": "speaker",
        "LOCATION": "url",
        "URL": "stream_url",
        "DESCRIPTION": None,
    },
    "patterns": {
        "SUMMARY": r"^(?P<speaker>.+?)\s*\((?P<affiliation>[^()]+)\)\s*$",
        "DESCRIPTION": [
            r"\ATitle: (?P<title>[^\n]+)",
            r"(?m)^Lecture held in (?P<location>[^\n]+?)\.?$",
            r"(?s)\n\nAbstract\n(?P<abstract>.+)",
        ],
    },
}


def feed_source(source_id: str, url: str, **params: Any) -> SourceConfig:
    """A hand-written ``adapter: ics`` entry for a recorded feed."""
    return make_source(
        id=source_id,
        name="A recorded feed",
        group="researchseminars",
        adapter="ics",
        url=url,
        host_org="Somewhere",
        timezone="UTC",
        default_fields=["math"],
        params=params,
    )


@functools.cache
def recorded(
    source_id: str, url: str, with_params: bool
) -> tuple[tuple[str, ...], tuple[EventDraft, ...]]:
    source = feed_source(source_id, url, **(SERIES_FEED_PARAMS if with_params else {}))
    http = offline_client(source_id)
    adapter = IcsAdapter()
    drafts = adapter.parse(adapter.fetch(http, source, window=make_window(RECORDED)), source)
    return tuple(http.requests), tuple(drafts)


def by_id(drafts: tuple[EventDraft, ...], native_id: str) -> EventDraft:
    (draft,) = [draft for draft in drafts if draft.native_id == native_id]
    return draft


LA = "TZID=America/Los_Angeles"
PST_4PM = datetime(2026, 11, 17, 16, 0, tzinfo=PACIFIC)
PST_5PM = datetime(2026, 11, 17, 17, 0, tzinfo=PACIFIC)
UTC_4PM = datetime(2026, 11, 17, 16, 0, tzinfo=UTC)
FLOATING_4PM = datetime(2026, 11, 17, 16, 0)
FLOATING_5PM = datetime(2026, 11, 17, 17, 0)


def calendar(*events: str, header: str = "") -> str:
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:test\n" + header + "".join(events) + "END:VCALENDAR\n"
    )


def vevent(
    *lines: str, uid: str | None = "talk-1", start: str | None = "DTSTART:20261117T160000Z"
) -> str:
    head = ([f"UID:{uid}"] if uid else []) + ([start] if start else [])
    return "BEGIN:VEVENT\n" + "".join(f"{line}\n" for line in [*head, *lines]) + "END:VEVENT\n"


def example_source(**params: Any) -> SourceConfig:
    return make_source(
        id="custom-example-talks",
        name="Example talks",
        group="stanford-custom",
        adapter="ics",
        url=FEED,
        host_org="Example",
        default_fields=["cs"],
        params=params,
    )


def parse(text: str, *, window: TimeWindow | None = None, **params: Any) -> list[EventDraft]:
    source = example_source(**params)
    document = make_document(url=FEED, text=text, content_type="text/calendar")
    payload = make_payload(document, source_id=source.id, window=window or make_window())
    return IcsAdapter().parse(payload, source)


def parse_one(*lines: str, **kwargs: Any) -> EventDraft:
    (draft,) = parse(calendar(vevent(*lines, **kwargs)))
    return draft


WINDOW = TimeWindow(
    start=datetime(2026, 10, 20, 7, 0, tzinfo=UTC),  # 2026-10-20 00:00 PDT
    end=datetime(2026, 11, 17, 8, 0, tzinfo=UTC),  # 2026-11-17 00:00 PST
)


def expand(*events: str) -> list[EventDraft]:
    """The drafts of a calendar of ``events`` in the window ``WINDOW``."""
    return parse(calendar(*events), window=WINDOW)


# --- registration and configured sources ---------------------------------------------------


def test_the_module_registers_the_ics_adapter() -> None:
    assert isinstance(base.get_adapter("ics"), IcsAdapter)


@pytest.mark.parametrize("source_id", ["custom-sail-events", "custom-theory-seminar"])
def test_the_params_of_the_configured_ics_sources_are_accepted(source_id: str) -> None:
    sources = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
    params = ics_params(sources[source_id])
    assert params.calendar_ids
    assert params.property_fields == {}


# --- the recorded Indico feeds of SLAC ---------------------------------------------------

SLAC_THEORY = "bayarea-slac-theory"
INDICO_FEED = "https://indico.slac.stanford.edu/category/129/events.ics"
SLAC_EXPERIMENTAL = "bayarea-slac-experimental"
EXPERIMENTAL_FEED = "https://indico.slac.stanford.edu/category/14/events.ics"


@functools.cache
def indico_source(source_id: str) -> tuple[tuple[str, ...], tuple[EventDraft, ...]]:
    """A configured Indico source over its recording (both recorded 2026-11-17 UTC)."""
    sources = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
    source = sources[source_id]
    http = offline_client(source_id)
    window = make_window(datetime(2026, 11, 17, 1, 27, tzinfo=UTC))
    adapter = IcsAdapter()
    drafts = adapter.parse(adapter.fetch(http, source, window=window), source)
    return tuple(http.requests), tuple(drafts)


def slac_theory() -> tuple[tuple[str, ...], tuple[EventDraft, ...]]:
    return indico_source(SLAC_THEORY)


def slac_experimental() -> tuple[tuple[str, ...], tuple[EventDraft, ...]]:
    return indico_source(SLAC_EXPERIMENTAL)


class TestRecordedIndicoFeed:
    def test_the_source_reads_the_category_feed_and_ignores_robots_on_its_host(self) -> None:
        sources = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
        source = sources[SLAC_THEORY]
        assert source.enabled
        assert source.ignore_robots == ["indico.slac.stanford.edu"]
        requests, drafts = slac_theory()
        assert requests == (INDICO_FEED,)
        assert len(drafts) == 11

    def test_the_speaker_line_gives_the_speaker_and_the_abstract_comes_without_it(self) -> None:
        _, drafts = slac_theory()
        draft = by_id(drafts, "indico-event-10800@indico.slac.stanford.edu")
        assert draft.title == "Decays of beautiful hadrons"
        assert [(person.name, person.affiliation) for person in draft.speakers] == [
            ("Federico Cima", "Caltech")
        ]
        assert draft.speaker_text is None
        assert draft.abstract is not None
        assert draft.abstract.startswith("In this talk, I will present some recent progress")
        assert draft.abstract.endswith("to test the size of U-spin breaking.")
        assert draft.location == "48/2-224 - Madrone (SLAC)"
        assert draft.url == "https://indico.slac.stanford.edu/event/10800/"
        assert draft.start == datetime(2026, 10, 21, 12, 30, tzinfo=PACIFIC)

    def test_a_talk_without_an_abstract_keeps_its_speaker_and_no_abstract(self) -> None:
        _, drafts = slac_theory()
        draft = by_id(drafts, "indico-event-10828@indico.slac.stanford.edu")
        assert draft.title == "TBA"
        assert [(person.name, person.affiliation) for person in draft.speakers] == [
            ("Alessandro Lenoci", "Hebrew U")
        ]
        assert draft.abstract is None

    def test_a_talk_without_a_speaker_line_has_no_speaker_and_no_abstract(self) -> None:
        _, drafts = slac_theory()
        draft = by_id(drafts, "indico-event-10820@indico.slac.stanford.edu")
        assert draft.title is not None
        assert draft.title.startswith("Debopam Goswami: Producing the GeV Galactic Center")
        assert draft.speakers == []
        assert draft.abstract is None


class TestRecordedExperimentalFeed:
    def test_the_source_reads_the_category_feed_and_ignores_robots_on_its_host(self) -> None:
        sources = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
        source = sources[SLAC_EXPERIMENTAL]
        assert source.enabled
        assert source.ignore_robots == ["indico.slac.stanford.edu"]
        requests, drafts = slac_experimental()
        assert requests == (EXPERIMENTAL_FEED,)
        assert len(drafts) == 5

    def test_the_abstract_stops_before_the_zoom_address(self) -> None:
        _, drafts = slac_experimental()
        draft = by_id(drafts, "indico-event-10676@indico.slac.stanford.edu")
        assert draft.title == (
            "Probing New Physics through Higgs\u2013Vector-Boson Couplings with the ATLAS Detector"
        )
        assert [(person.name, person.affiliation) for person in draft.speakers] == [
            ("Jiayi Chen", "Simon Fraser University")
        ]
        assert draft.abstract is not None
        assert draft.abstract.startswith("Thirteen years after the discovery of the Higgs boson")
        assert draft.abstract.endswith("at a center-of-mass energy of 13 TeV.")
        assert "zoom" not in draft.abstract
        assert draft.location == "Stanford"
        assert draft.start == datetime(2026, 10, 20, 12, 30, tzinfo=PACIFIC)

    def test_every_talk_with_a_speaker_line_has_an_abstract_without_the_meeting(self) -> None:
        _, drafts = slac_experimental()
        with_speaker = [draft for draft in drafts if draft.speakers]
        assert len(with_speaker) == 3
        for draft in with_speaker:
            assert draft.abstract is not None
            assert "pwd=" not in draft.abstract
            assert "indico.slac.stanford.edu" not in draft.abstract

    def test_a_tba_talk_has_no_speaker_and_no_abstract(self) -> None:
        _, drafts = slac_experimental()
        draft = by_id(drafts, "indico-event-10805@indico.slac.stanford.edu")
        assert draft.title == "TBA"
        assert draft.speakers == []
        assert draft.abstract is None
        assert draft.start == datetime(2026, 11, 24, 12, 30, tzinfo=PACIFIC)


# --- the recorded researchseminars.org feeds -----------------------------------------------


# --- the recorded Google Calendar of Chemical and Systems Biology --------------------------

CSB_CALENDAR = "custom-csb-calendar"
CSB_FEED = "https://calendar.google.com/calendar/ical/csbdept%40gmail.com/public/basic.ics"


def csb_source() -> SourceConfig:
    sources = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
    return sources[CSB_CALENDAR]


@functools.cache
def csb_calendar() -> tuple[tuple[str, ...], tuple[EventDraft, ...]]:
    """The configured CSB calendar over its recording (2026-11-17, trimmed to October on)."""
    source = csb_source()
    http = offline_client(CSB_CALENDAR)
    window = make_window(datetime(2026, 11, 17, 4, 1, tzinfo=UTC))
    adapter = IcsAdapter()
    drafts = adapter.parse(adapter.fetch(http, source, window=window), source)
    return tuple(http.requests), tuple(drafts)


def csb_talk(drafts: tuple[EventDraft, ...], start: datetime) -> EventDraft:
    (draft,) = [draft for draft in drafts if draft.start == start]
    return draft


class TestRecordedCsbCalendar:
    def test_the_source_reads_the_calendar_and_ignores_robots_on_its_host(self) -> None:
        source = csb_source()
        assert source.enabled
        assert source.ignore_robots == ["calendar.google.com"]
        requests, drafts = csb_calendar()
        assert requests == (CSB_FEED,)
        assert len(drafts) == 31

    def test_a_science_circle_talk_names_the_student_and_the_lab(self) -> None:
        _, drafts = csb_calendar()
        draft = csb_talk(drafts, datetime(2026, 11, 10, 12, 0, tzinfo=PACIFIC))
        assert draft.title == "Science Circle: Pizza Talk"
        assert draft.series == "Science Circle"
        assert [(person.name, person.affiliation) for person in draft.speakers] == [
            ("Olivia Crocker", "Wysocka Lab")
        ]
        assert draft.speaker_text is None
        assert draft.abstract is None  # the moderator line is no abstract
        assert draft.location == "Alway M112"

    def test_speaker_tba_names_nobody(self) -> None:
        _, drafts = csb_calendar()
        draft = csb_talk(drafts, datetime(2027, 2, 16, 12, 0, tzinfo=PACIFIC))
        assert draft.title == "Science Circle: Faculty Talk"
        assert draft.speakers == []

    def test_a_special_seminar_takes_the_speaker_from_its_title(self) -> None:
        _, drafts = csb_calendar()
        draft = csb_talk(drafts, datetime(2027, 2, 8, 12, 30, tzinfo=PACIFIC))
        assert draft.title == "CSB Special Seminar: Jeanne Hardy, PhD"
        assert [(person.name, person.affiliation) for person in draft.speakers] == [
            ("Jeanne Hardy", None)
        ]
        assert draft.abstract is None  # "Faculty Host: Matt Bogyo"

    def test_an_announcement_gives_the_talk_title_and_the_abstract(self) -> None:
        _, drafts = csb_calendar()
        draft = csb_talk(drafts, datetime(2026, 11, 16, 11, 0, tzinfo=PACIFIC))
        assert draft.title is not None
        assert draft.title.startswith("Science with Claude: agentic biology")
        assert [person.name for person in draft.speakers] == ["David Garfield"]
        assert draft.abstract is not None
        assert draft.abstract.startswith("Large language models are now good enough")
        assert "Speaker Bio" not in draft.abstract

    @pytest.mark.parametrize(
        ("summary", "speaker"),
        [
            ("CSB Cutting Lecture Series: Karen Adelman - Harvard", ("Karen Adelman", "Harvard")),
            (
                "CSB Cutting Lecture:  Ibrahim Cisse - Max Planck Institute",
                ("Ibrahim Cisse", "Max Planck Institute"),
            ),
            ("CSB Cutting Lecture Series: Ronald T Raines (MIT)", ("Ronald T Raines", "MIT")),
            (
                "CSB Cutting Lecture Series: Dr. Wallace Marshall, UCSF",
                ("Wallace Marshall", "UCSF"),
            ),
            ("CSB Faculty Candidate Seminar: Matthew King, Ph.D.", ("Matthew King", None)),
            ("CSB Thesis Defense: Rebecca Chan, Li Lab", ("Rebecca Chan", "Li Lab")),
            ("CSB Thesis Defense: Jo-Hsi Huang", ("Jo-Hsi Huang", None)),
            (
                "CSB and Biochemistry Joint Special Seminar: Xiang (David) Li, PhD",
                None,
            ),
            ("CSB Special Seminar: TBA", None),
            ("CSB Holiday Party", None),
        ],
    )
    def test_the_titles_of_the_talks_name_their_speakers(
        self, summary: str, speaker: tuple[str, str | None] | None
    ) -> None:
        (draft,) = parse(calendar(vevent(f"SUMMARY:{summary}")), **csb_source().params)
        expected = [] if speaker is None else [speaker]
        assert [(person.name, person.affiliation) for person in draft.speakers] == expected


class TestRecordedFeeds:
    def test_fetch_requests_the_feed_and_nothing_else(self) -> None:
        requests, drafts = recorded("rsorg-topos-colloquium", TOPOS, False)
        assert requests == (TOPOS,)
        assert len(drafts) == 233
        assert {draft.extraction for draft in drafts} == {"feed"}

    def test_every_event_is_returned_whatever_its_date(self) -> None:
        _, drafts = recorded("rsorg-stanford-ag", AGSTANFORD, False)
        assert len(drafts) == 148
        assert max(draft.start for draft in drafts if isinstance(draft.start, datetime)) < RECORDED

    def test_without_params_summary_is_the_title(self) -> None:
        _, drafts = recorded("rsorg-stanford-ag", AGSTANFORD, False)
        draft = by_id(drafts, "agstanford/152")
        assert draft.title == "Christopher Hacon (University of Utah)"
        assert draft.start == datetime(2024, 12, 4, 22, 0, tzinfo=UTC)
        assert draft.end == datetime(2024, 12, 4, 23, 0, tzinfo=UTC)
        assert draft.location == "https://researchseminars.org/talk/agstanford/152/"
        assert draft.abstract is None
        assert draft.abstract_html is not None
        assert draft.abstract_html.startswith('Title: <a href="https://researchseminars.org/talk/')
        assert draft.timezone is None

    def test_a_series_feed_read_through_params(self) -> None:
        _, drafts = recorded("rsorg-stanford-ag", AGSTANFORD, True)
        draft = by_id(drafts, "agstanford/152")
        assert draft.title == "(Distinguished Lecture) Algebraic geometry vs Kahler geometry"
        assert draft.start == datetime(2024, 12, 4, 14, 0, tzinfo=PACIFIC)
        assert [(s.name, s.affiliation) for s in draft.speakers] == [
            ("Christopher Hacon", "University of Utah")
        ]
        assert draft.speaker_text is None
        assert draft.location == "383-N"
        assert draft.url == "https://researchseminars.org/talk/agstanford/152/"
        assert draft.abstract is not None
        assert draft.abstract.startswith("Algebraic geometry and analytic geometry")
        assert draft.abstract_html is None

    def test_an_abstract_with_latex_stays_text(self) -> None:
        _, drafts = recorded("rsorg-stanford-ag", AGSTANFORD, True)
        draft = by_id(drafts, "agstanford/6")
        assert draft.title == "Bertini irreducibility theorems via statistics"
        assert draft.abstract is not None
        assert draft.abstract.startswith("Let $X \\subset \\mathbb{P}^n$")
        assert draft.location is None

    def test_an_online_talk_and_a_speaker_without_affiliation(self) -> None:
        _, drafts = recorded("rsorg-topos-colloquium", TOPOS, True)
        draft = by_id(drafts, "ToposInstituteColloquium/232")
        assert draft.title == "What are (infty,infty)-categories?"
        assert draft.start == datetime(2026, 11, 12, 9, 0, tzinfo=PACIFIC)
        assert draft.speakers == []
        assert draft.speaker_text == "Viktoriya Ozornova"
        assert draft.stream_url == (
            "https://topos-institute.zoom.us/j/00000000000?pwd=REDACTEDREDACTEDREDACTEDREDACTED"
        )
        assert draft.is_online is True
        assert draft.location is None
        assert draft.url == "https://researchseminars.org/talk/ToposInstituteColloquium/232/"

    def test_a_talk_without_a_title_yet(self) -> None:
        _, drafts = recorded("rsorg-topos-colloquium", TOPOS, True)
        draft = by_id(drafts, "ToposInstituteColloquium/254")
        assert draft.title is None
        assert draft.speaker_text == "Jacob Tsimerman"
        assert draft.abstract is None

    def test_the_public_helpers_give_the_common_part(self) -> None:
        document = offline_client("rsorg-stanford-ag").get(AGSTANFORD)
        feed = read_calendar(document)
        assert len(feed.vevents) == 148
        assert feed.timezone is None
        (event,) = [
            item for item in feed.vevents if property_texts(item, "UID") == ["agstanford/152"]
        ]
        source = feed_source("rsorg-stanford-ag", AGSTANFORD)
        draft = vevent_draft(event, source, base_url=document.final_url)
        assert draft.native_id == "agstanford/152"
        assert draft.title == "Christopher Hacon (University of Utah)"
        assert draft.start == datetime(2024, 12, 4, 22, 0, tzinfo=UTC)
        assert property_texts(event, "SUMMARY") == ["Christopher Hacon (University of Utah)"]
        text = plain_text(property_texts(event, "DESCRIPTION")[0])
        assert text.startswith("Title: (Distinguished Lecture) Algebraic geometry vs Kahler")
        assert "\nby Christopher Hacon (University of Utah) as part of" in text
        assert occurrences(feed, make_window(RECORDED), timezone="UTC")[0].vevent is feed.vevents[0]


# --- reading a feed ------------------------------------------------------------------------


class TestReading:
    @pytest.mark.parametrize(
        "text",
        [
            "<!DOCTYPE html><html><body>Sign in</body></html>\n",
            "BEGIN:VCALENDAR\nthis is not a content line\nEND:VCALENDAR\n",
            "BEGIN:VEVENT\nUID:x\nEND:VEVENT\n",
            "\n",
        ],
    )
    def test_a_body_that_is_no_calendar_is_a_parse_error(self, text: str) -> None:
        with pytest.raises(ParseError, match="custom-example-talks"):
            parse(text)

    def test_a_time_zone_name_too_long_for_a_file_is_a_parse_error(self) -> None:
        start = "DTSTART;TZID=" + "x" * 300 + ":20261117T160000"
        with pytest.raises(ParseError, match="cannot be read"):
            parse(calendar(vevent("SUMMARY:Talk", start=start)))

    def test_a_long_calendar_time_zone_is_ignored(self) -> None:
        text = calendar(vevent("SUMMARY:Talk"), header="X-WR-TIMEZONE:" + "x" * 300 + "\n")
        (draft,) = parse(text)
        assert draft.title == "Talk"

    @pytest.mark.parametrize("error", [AttributeError, OSError, TypeError, KeyError])
    def test_any_failure_of_the_calendar_reader_is_a_parse_error(
        self, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
    ) -> None:
        def broken(*args: Any, **kwargs: Any) -> Any:
            raise error("a broken VTIMEZONE")

        monkeypatch.setattr(ics.icalendar.Calendar, "from_ical", broken)
        with pytest.raises(ParseError, match="cannot be read"):
            parse(calendar(vevent("SUMMARY:Talk")))

    def test_a_calendar_inside_other_text_is_not_enough(self) -> None:
        text = (
            "BEGIN:VEVENT\nUID:x\nSUMMARY:BEGIN:VCALENDAR\nDTSTART:20261117T160000Z\nEND:VEVENT\n"
        )
        with pytest.raises(ParseError, match="not an iCalendar feed"):
            parse(text)

    def test_an_empty_calendar_has_no_events(self) -> None:
        assert parse(calendar()) == []

    def test_fetch_uses_every_url_of_the_source(self) -> None:
        source = example_source().evolve(urls=[f"{FEED}?b"])
        http = FakeHttpClient({FEED: calendar(), f"{FEED}?b": calendar()})
        payload = IcsAdapter().fetch(http, source, window=make_window())
        assert http.requests == [FEED, f"{FEED}?b"]
        assert IcsAdapter().parse(payload, source) == []

    def test_property_texts_of_repeated_and_list_properties(self) -> None:
        feed = read_calendar(
            make_document(
                url=FEED,
                text=calendar(
                    vevent(
                        "CATEGORIES:A,B",
                        "CATEGORIES:C",
                        "URL:https://a.example/1",
                        "URL:https://a.example/2",
                    )
                ),
            )
        )
        (event,) = feed.vevents
        assert property_texts(event, "CATEGORIES") == ["A", "B", "C"]
        assert property_texts(event, "URL") == ["https://a.example/1", "https://a.example/2"]
        assert property_texts(event, "LOCATION") == []


# --- one event -----------------------------------------------------------------------------


class TestOneEvent:
    def test_the_default_fields(self) -> None:
        draft = parse_one(
            "DTEND:20261117T170000Z",
            "SUMMARY:Diffusion models for proteins",
            "LOCATION:Clark Center S360",
            "DESCRIPTION:We present a model\\, and more.\\nSecond line.",
            "URL:mailto:host@example.edu",
            "URL:/talks/1",
            "CATEGORIES:Seminar,Bio",
            "ORGANIZER;CN=Host:mailto:host@example.edu",
            "ATTENDEE:mailto:someone@example.edu",
        )
        assert draft.native_id == "talk-1"
        assert draft.title == "Diffusion models for proteins"
        assert draft.start == datetime(2026, 11, 17, 16, 0, tzinfo=UTC)
        assert draft.end == datetime(2026, 11, 17, 17, 0, tzinfo=UTC)
        assert draft.location == "Clark Center S360"
        assert draft.abstract == "We present a model, and more.\nSecond line."
        assert draft.url == "https://calendar.example.edu/talks/1"
        assert draft.categories == ["Seminar", "Bio"]
        assert draft.status_hint is None
        assert "example.edu" not in (draft.speaker_text or "")
        assert draft.speakers == []

    @pytest.mark.parametrize(
        ("lines", "start", "end"),
        [
            ([f"DTSTART;{LA}:20261117T160000", f"DTEND;{LA}:20261117T170000"], PST_4PM, PST_5PM),
            (["DTSTART;TZID=Pacific Standard Time:20261117T160000"], PST_4PM, None),
            (["DTSTART:20261117T160000", "DURATION:PT1H"], FLOATING_4PM, FLOATING_5PM),
            (["DTSTART;TZID=Nowhere/Land:20261117T160000"], FLOATING_4PM, None),
            (["DTSTART:20261117T160000Z", "DTEND:20261117T160000Z"], UTC_4PM, None),
        ],
    )
    def test_times(self, lines: list[str], start: datetime, end: datetime | None) -> None:
        draft = parse_one(*lines, start=None)
        assert (draft.start, draft.end, draft.all_day) == (start, end, False)
        assert (draft.start.tzinfo is None) == (start.tzinfo is None)  # type: ignore[union-attr]

    def test_a_zone_defined_in_the_feed(self) -> None:
        zone = (
            "BEGIN:VTIMEZONE\nTZID:Campus Time\n"
            "BEGIN:STANDARD\nDTSTART:19701101T020000\nTZOFFSETFROM:-0700\nTZOFFSETTO:-0800\n"
            "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\nEND:STANDARD\n"
            "BEGIN:DAYLIGHT\nDTSTART:19700308T020000\nTZOFFSETFROM:-0800\nTZOFFSETTO:-0700\n"
            "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\nEND:DAYLIGHT\nEND:VTIMEZONE\n"
        )
        (draft,) = parse(
            calendar(vevent("DTSTART;TZID=Campus Time:20261117T160000", start=None), header=zone)
        )
        assert isinstance(draft.start, datetime)
        assert draft.start.utcoffset() == timedelta(hours=-8)
        assert draft.start.astimezone(UTC) == datetime(2026, 11, 18, 0, 0, tzinfo=UTC)

    def test_each_feed_reads_its_own_definition_of_a_zone(self) -> None:
        """The library keeps the zones feeds define for the whole process; no feed sees them."""

        def fixed(offset: str) -> str:
            return (
                "BEGIN:VTIMEZONE\nTZID:Lab Time\nBEGIN:STANDARD\nDTSTART:19700101T000000\n"
                f"TZOFFSETFROM:{offset}\nTZOFFSETTO:{offset}\nEND:STANDARD\nEND:VTIMEZONE\n"
            )

        talk = vevent("DTSTART;TZID=Lab Time:20261117T100000", start=None)
        (east,) = parse(calendar(talk, header=fixed("+0200")))
        (west,) = parse(calendar(talk, header=fixed("-0500")))
        (undefined,) = parse(calendar(talk))
        assert isinstance(east.start, datetime)
        assert isinstance(west.start, datetime)
        assert east.start.astimezone(UTC) == datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
        assert west.start.astimezone(UTC) == datetime(2026, 11, 17, 15, 0, tzinfo=UTC)
        assert undefined.start == datetime(2026, 11, 17, 10, 0)

    def test_floating_times_take_the_zone_the_calendar_names(self) -> None:
        header = "X-WR-TIMEZONE:America/New_York\n"
        (draft,) = parse(calendar(vevent("DTSTART:20261117T160000", start=None), header=header))
        assert draft.start == datetime(2026, 11, 17, 16, 0)
        assert draft.timezone == "America/New_York"
        (aware,) = parse(calendar(vevent(), header=header))
        assert aware.timezone is None
        (unknown,) = parse(
            calendar(
                vevent("DTSTART:20261117T160000", start=None), header="X-WR-TIMEZONE:Mars/Base\n"
            )
        )
        assert unknown.timezone is None

    @pytest.mark.parametrize(
        ("lines", "last_day"),
        [
            ([], None),
            (["DTEND;VALUE=DATE:20261118"], None),
            (["DTEND;VALUE=DATE:20261120"], date(2026, 11, 19)),
            (["DURATION:P3D"], date(2026, 11, 19)),
            (["DTEND:20261119T120000"], date(2026, 11, 19)),
            (["DTEND;VALUE=DATE:20261117"], None),
        ],
    )
    def test_all_day_events_end_on_their_last_day(
        self, lines: list[str], last_day: date | None
    ) -> None:
        draft = parse_one(*lines, start="DTSTART;VALUE=DATE:20261117")
        assert (draft.start, draft.end, draft.all_day) == (date(2026, 11, 17), last_day, True)
        assert draft.is_all_day

    @pytest.mark.parametrize(
        ("status", "hint"),
        [
            ("CANCELLED", "cancelled"),
            ("cancelled", "cancelled"),
            ("TENTATIVE", "tentative"),
            ("CONFIRMED", "confirmed"),
            ("DONE", None),
        ],
    )
    def test_status(self, status: str, hint: str | None) -> None:
        assert parse_one(f"STATUS:{status}").status_hint == hint

    def test_html_descriptions(self) -> None:
        html = parse_one("DESCRIPTION:<p>Talk &amp; <b>food</b></p><br>Room 101")
        assert html.abstract is None
        assert html.abstract_html == "<p>Talk &amp; <b>food</b></p><br>Room 101"
        maths = parse_one("DESCRIPTION:We show $x<y$ and a < b for all <n>.")
        assert maths.abstract == "We show $x<y$ and a < b for all <n>."
        alternative = parse_one("X-ALT-DESC;FMTTYPE=text/html:<p>Only HTML</p>")
        assert alternative.abstract_html == "<p>Only HTML</p>"

    def test_text_is_copied_as_it_is(self) -> None:
        draft = parse_one(
            "SUMMARY:Q&amp;A with Fran\u00e7ois \u0160t\u011bp\u00e1n",
            "LOCATION:Caf\u00e9 \u00c9cole",
        )
        assert draft.title == "Q&amp;A with Fran\u00e7ois \u0160t\u011bp\u00e1n"
        assert draft.location == "Caf\u00e9 \u00c9cole"

    def test_events_without_uid_and_repeated_uids(self) -> None:
        drafts = parse(
            calendar(
                vevent("SUMMARY:first"),
                vevent("SUMMARY:again"),
                vevent("SUMMARY:anonymous", uid=None),
                vevent("SUMMARY:anonymous too", uid=None),
            )
        )
        assert [(draft.native_id, draft.title) for draft in drafts] == [
            ("talk-1", "first"),
            (None, "anonymous"),
            (None, "anonymous too"),
        ]

    @pytest.mark.parametrize(
        "broken",
        [
            vevent("SUMMARY:no start", start=None),
            vevent("SUMMARY:bad start", start="DTSTART:next tuesday"),
            vevent("RRULE:FREQ=SOMETIMES", "SUMMARY:bad rule"),
            vevent("SUMMARY:a time only", start="DTSTART;VALUE=TIME:160000"),
            vevent("RECURRENCE-ID:garbage", "SUMMARY:bad recurrence id"),
            vevent(
                "URL:https://user:pw@example.edu/x",
                "URL:javascript:void(0)",
                "SUMMARY:links",
                uid="links",
            ),
        ],
    )
    def test_a_malformed_event_is_skipped(self, broken: str) -> None:
        drafts = parse(calendar(broken, vevent("SUMMARY:fine", uid="fine")))
        assert [draft.native_id for draft in drafts][-1] == "fine"
        assert all(draft.title in ("fine", "links") for draft in drafts)
        assert all(draft.url is None for draft in drafts)

    def test_an_end_of_another_type_is_dropped(self) -> None:
        draft = parse_one("DTEND;VALUE=DATE:20261118", "SUMMARY:mixed types")
        assert draft.start == datetime(2026, 11, 17, 16, 0, tzinfo=UTC)
        assert draft.end is None
        recurring = expand(
            vevent(
                "DTEND;VALUE=DATE:20261021",
                "RRULE:FREQ=DAILY;COUNT=2",
                start="DTSTART:20261020T230000Z",
            )
        )
        assert [(draft.start.day, draft.end) for draft in recurring] == [(20, None), (21, None)]  # type: ignore[union-attr]

    def test_occurrence_draft_takes_the_times_and_id_of_the_occurrence(self) -> None:
        feed = read_calendar(
            make_document(url=FEED, text=calendar(vevent("SUMMARY:Talk", "URL:/t/1")))
        )
        moved = IcsOccurrence(
            feed.vevents[0], date(2026, 12, 1), date(2026, 12, 3), "talk-1/20261201"
        )
        draft = occurrence_draft(moved, example_source(), base_url=FEED, calendar_timezone="UTC")
        assert (draft.native_id, draft.start, draft.end, draft.all_day) == (
            "talk-1/20261201",
            date(2026, 12, 1),
            date(2026, 12, 2),
            True,
        )
        assert draft.timezone is None
        assert draft.url == "https://calendar.example.edu/t/1"
        assert draft.title == "Talk"
        with pytest.raises(ValueError, match="DTSTART"):
            IcsOccurrence.of(
                read_calendar(make_document(url=FEED, text=calendar(vevent(start=None)))).vevents[0]
            )

    def test_an_occurrence_that_cannot_be_a_draft_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = ics.occurrence_draft

        def picky(occurrence: IcsOccurrence, *args: Any, **kwargs: Any) -> EventDraft:
            if occurrence.native_id == "bad":
                raise ValueError("refused")
            return real(occurrence, *args, **kwargs)

        monkeypatch.setattr(ics, "occurrence_draft", picky)
        drafts = parse(calendar(vevent(uid="bad"), vevent(uid="good")))
        assert [draft.native_id for draft in drafts] == ["good"]


# --- YAML-driven fields --------------------------------------------------------------------


ADA = "Ada Lovelace (Cambridge)"
ZOOM = "https://zoom.example.com/j/1?pwd=x"
PAGE = "https://talks.example.edu/1"


class TestPropertyFields:
    @pytest.mark.parametrize(
        ("mapping", "expected"),
        [
            ({"SUMMARY": "speaker"}, {"title": None, "speaker_text": ADA}),
            ({"SUMMARY": "series", "DESCRIPTION": "title"}, {"series": ADA, "title": "Engines"}),
            ({"LOCATION": "stream_url"}, {"location": None, "stream_url": ZOOM, "is_online": True}),
            ({"LOCATION": "url", "URL": None}, {"location": None, "url": ZOOM}),
            ({"URL": "stream_url"}, {"url": None, "stream_url": PAGE}),
            ({"DESCRIPTION": None, "SUMMARY": "title"}, {"abstract": None, "title": ADA}),
            ({"SUMMARY": "location", "LOCATION": "abstract"}, {"location": ADA, "abstract": ZOOM}),
        ],
    )
    def test_a_property_fills_another_field(
        self, mapping: dict[str, Any], expected: dict[str, Any]
    ) -> None:
        lines = ("SUMMARY:" + ADA, "DESCRIPTION:Engines", "LOCATION:" + ZOOM, "URL:" + PAGE)
        text = calendar(vevent(*lines))
        (draft,) = parse(text, property_fields=mapping)
        assert {key: getattr(draft, key) for key in expected} == expected

    def test_an_html_property_mapped_to_the_abstract(self) -> None:
        (draft,) = parse(
            calendar(vevent("LOCATION:<p>Room <b>1</b></p>")),
            property_fields={"LOCATION": "abstract"},
        )
        assert (draft.abstract, draft.abstract_html, draft.location) == (
            None,
            "<p>Room <b>1</b></p>",
            None,
        )

    def test_a_missing_property_clears_its_new_field(self) -> None:
        (draft,) = parse(calendar(vevent("SUMMARY:Talk")), property_fields={"URL": "stream_url"})
        assert draft.stream_url is None
        assert draft.is_online is None


class TestPatterns:
    def test_groups_set_fields_and_the_first_pattern_wins(self) -> None:
        text = calendar(
            vevent(
                "SUMMARY:Seminar: Ada Lovelace (Cambridge) - Engines",
                "DESCRIPTION:<p>Join at https://zoom.example.com/j/9</p><p>Room: Gates 104</p>"
                "<p>More: https://talks.example.edu/9</p>",
            )
        )
        (draft,) = parse(
            text,
            patterns={
                "SUMMARY": [
                    r"^(?P<series>[^:]+): (?P<speaker>[^(]+?) "
                    r"\((?P<affiliation>[^)]+)\) - (?P<title>.+)$",
                    r"^(?P<title>.+)$",
                ],
                "DESCRIPTION": [
                    r"Join at (?P<stream_url>\S+)",
                    r"(?m)^Room: (?P<location>.+)$",
                    r"More: (?P<url>\S+)",
                    r"(?P<location>Gates)",
                ],
            },
        )
        assert draft.title == "Engines"
        assert draft.series == "Seminar"
        assert [(s.name, s.affiliation) for s in draft.speakers] == [("Ada Lovelace", "Cambridge")]
        assert draft.stream_url == "https://zoom.example.com/j/9"
        assert draft.is_online is True
        assert draft.location == "Gates 104"
        assert draft.url == "https://talks.example.edu/9"

    def test_a_pattern_abstract_replaces_an_html_one(self) -> None:
        (draft,) = parse(
            calendar(vevent("DESCRIPTION:<p>Intro</p><p>Abstract: the talk</p>")),
            patterns={"DESCRIPTION": r"Abstract: (?P<abstract>.+)"},
        )
        assert (draft.abstract, draft.abstract_html) == ("the talk", None)

    def test_patterns_that_do_not_match_change_nothing(self) -> None:
        (draft,) = parse(
            calendar(vevent("SUMMARY:Plain title")),
            patterns={"SUMMARY": r"^Speaker: (?P<speaker>.+)$", "LOCATION": r"(?P<location>.+)"},
        )
        assert draft.title == "Plain title"
        assert draft.speakers == []
        assert draft.location is None

    def test_empty_groups_are_ignored(self) -> None:
        (draft,) = parse(
            calendar(vevent("SUMMARY:Talk")),
            patterns={"SUMMARY": r"^(?P<series>\s*)Talk(?P<speaker>)$"},
        )
        assert draft.series is None
        assert draft.speakers == []

    def test_a_link_group_that_is_no_web_page_gives_none(self) -> None:
        (draft,) = parse(
            calendar(vevent("SUMMARY:mailto:x@example.edu")), patterns={"SUMMARY": r"(?P<url>.+)"}
        )
        assert draft.url is None

    def test_patterns_see_a_bounded_text(self) -> None:
        long = "x" * MAX_PATTERN_INPUT
        (draft,) = parse(
            calendar(vevent(f"DESCRIPTION:{long}TAIL")),
            patterns={"DESCRIPTION": r"(?P<title>TAIL)"},
        )
        assert draft.title is None


# --- plain text ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "plain"),
    [
        ("Line one\nline two", "Line one\nline two"),
        ("a < b and c > d", "a < b and c > d"),
        ("$x<y$ for all <n>", "$x<y$ for all <n>"),
        ("Talk &amp; food<br>Room 101", "Talk & food\nRoom 101"),
        ("<p>First</p><p>Second</p>", "\nFirst\nSecond"),
        ('Title: <a href="https://x.example/1">Rings</a>\nby Emmy', "Title: Rings\nby Emmy"),
    ],
)
def test_plain_text(text: str, plain: str) -> None:
    assert plain_text(text) == plain


# --- params --------------------------------------------------------------------------------


class TestParams:
    @pytest.mark.parametrize(
        ("params", "named"),
        [
            ({"summary_is": "speaker"}, "params.summary_is"),
            ({"property_fields": {"DTSTART": "title"}}, "params.property_fields.DTSTART"),
            ({"property_fields": {"SUMMARY": "abstractt"}}, "params.property_fields.SUMMARY"),
            ({"patterns": {"URL": "x"}}, "params.patterns.URL"),
            ({"patterns": {"SUMMARY": "(unclosed"}}, "params.patterns.SUMMARY"),
            ({"patterns": {"SUMMARY": "(?P<room>.+)"}}, "params.patterns"),
            ({"calendar_ids": "one"}, "params.calendar_ids"),
        ],
    )
    def test_a_bad_or_unknown_key_is_named(self, params: dict[str, Any], named: str) -> None:
        source = example_source(**params)
        with pytest.raises(ConfigError, match=named.replace(".", r"\.")):
            ics_params(source)
        with pytest.raises(ConfigError, match="custom-example-talks"):
            IcsAdapter().fetch(FakeHttpClient(), source, window=make_window())
        with pytest.raises(ConfigError):
            IcsAdapter().parse(make_payload(source_id=source.id), source)

    def test_notes_and_shared_keys_are_accepted(self) -> None:
        params = ics_params(
            example_source(
                calendar_ids=["a@group.calendar.google.com"],
                embed_selector="iframe",
                extra_hosts=["x.example.org"],
            )
        )
        assert params.embed_selector == "iframe"

    def test_allow_empty_lets_a_run_without_events_be_healthy(self) -> None:
        assert not ics_params(example_source()).allow_empty
        source = example_source(allow_empty=True)
        assert ics_params(source).allow_empty
        assert allows_empty(source)
        with pytest.raises(ConfigError, match=r"params\.allow_empty"):
            ics_params(example_source(allow_empty="sometimes"))

    def test_a_single_pattern_is_a_list_of_one(self) -> None:
        params = ics_params(example_source(patterns={"SUMMARY": "^(?P<title>.+)$"}))
        assert [pattern.pattern for pattern in params.patterns["SUMMARY"]] == ["^(?P<title>.+)$"]


def test_the_adapter_keeps_no_state() -> None:
    assert vars(IcsAdapter()) == {}
