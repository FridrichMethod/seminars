"""The ics adapter: reading feeds, time conventions, recurrences and the YAML-driven fields."""

from __future__ import annotations

import functools
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import icalendar
import pytest

from factories import make_document, make_payload, make_source, make_window
from fakes import FakeHttpClient
from seminars.config import ConfigError
from seminars.config.loader import load_sources
from seminars.http import offline_client
from seminars.models import EventDraft, SourceConfig, TimeWindow
from seminars.sources import base
from seminars.sources.base import ParseError
from seminars.sources.generic import ics
from seminars.sources.generic.ics import (
    MAX_OCCURRENCES,
    MAX_PATTERN_INPUT,
    IcsAdapter,
    IcsCalendar,
    IcsOccurrence,
    check_rule,
    ics_params,
    instance_key,
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


# --- registration and configured sources ---------------------------------------------------


def test_the_module_registers_the_ics_adapter() -> None:
    assert isinstance(base.get_adapter("ics"), IcsAdapter)


@pytest.mark.parametrize("source_id", ["custom-sail-events", "custom-theory-seminar"])
def test_the_params_of_the_configured_ics_sources_are_accepted(source_id: str) -> None:
    sources = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
    params = ics_params(sources[source_id])
    assert params.calendar_ids
    assert params.property_fields == {}


# --- the recorded researchseminars.org feeds -----------------------------------------------


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


# --- recurring events ----------------------------------------------------------------------

WINDOW = TimeWindow(
    start=datetime(2026, 10, 20, 7, 0, tzinfo=UTC),  # 2026-10-20 00:00 PDT
    end=datetime(2026, 11, 17, 8, 0, tzinfo=UTC),  # 2026-11-17 00:00 PST
)


WEEK = [datetime(2026, 10, 20, 23, 0, tzinfo=UTC) + timedelta(weeks=n) for n in range(4)]


def expand(*events: str) -> list[EventDraft]:
    """The drafts of a calendar of ``events`` in the window ``WINDOW``."""
    return parse(calendar(*events), window=WINDOW)


class TestRecurrence:
    def test_a_weekly_rule_keeps_the_local_time_across_the_change_of_offset(self) -> None:
        drafts = expand(
            vevent(
                "DTEND;TZID=America/Los_Angeles:20260901T170000",
                "RRULE:FREQ=WEEKLY;BYDAY=TU",
                "SUMMARY:Weekly seminar",
                start="DTSTART;TZID=America/Los_Angeles:20260901T160000",
            )
        )
        starts = [draft.start for draft in drafts]
        assert starts == [datetime(2026, 10, day, 16, 0, tzinfo=PACIFIC) for day in (20, 27)] + [
            datetime(2026, 11, day, 16, 0, tzinfo=PACIFIC) for day in (3, 10)
        ]
        assert [draft.native_id for draft in drafts] == [
            "talk-1/20261020T230000Z",
            "talk-1/20261027T230000Z",
            "talk-1/20261104T000000Z",
            "talk-1/20261111T000000Z",
        ]
        assert all(draft.end == draft.start + timedelta(hours=1) for draft in drafts)  # type: ignore[operator]

    def test_exdate_rdate_and_a_moved_occurrence(self) -> None:
        drafts = expand(
            vevent(
                "RRULE:FREQ=WEEKLY;COUNT=10",
                "EXDATE:20261027T230000Z",
                "RDATE:20261029T230000Z",
                "SUMMARY:Weekly",
                start="DTSTART:20261013T230000Z",
            ),
            vevent(
                "RECURRENCE-ID:20261103T230000Z",
                "DTSTART:20261105T230000Z",
                "SUMMARY:Moved to Thursday",
                start=None,
            ),
            vevent(
                "RECURRENCE-ID:20261110T230000Z",
                "DTSTART:20261110T230000Z",
                "STATUS:CANCELLED",
                "SUMMARY:Weekly",
                start=None,
            ),
        )
        found = [(draft.native_id, draft.start, draft.title, draft.status_hint) for draft in drafts]
        assert found == [
            ("talk-1/20261020T230000Z", datetime(2026, 10, 20, 23, 0, tzinfo=UTC), "Weekly", None),
            ("talk-1/20261029T230000Z", datetime(2026, 10, 29, 23, 0, tzinfo=UTC), "Weekly", None),
            (
                "talk-1/20261103T230000Z",
                datetime(2026, 11, 5, 23, 0, tzinfo=UTC),
                "Moved to Thursday",
                None,
            ),
            (
                "talk-1/20261110T230000Z",
                datetime(2026, 11, 10, 23, 0, tzinfo=UTC),
                "Weekly",
                "cancelled",
            ),
        ]

    def test_an_override_without_its_occurrence_stands_alone_inside_the_window(self) -> None:
        drafts = expand(
            vevent(
                "RECURRENCE-ID:20260101T230000Z",
                "DTSTART:20261104T230000Z",
                "SUMMARY:moved in",
                start=None,
                uid="gone",
            ),
            vevent(
                "RECURRENCE-ID:20260108T230000Z",
                "DTSTART:20270104T230000Z",
                "SUMMARY:outside",
                start=None,
                uid="gone",
            ),
            vevent("RECURRENCE-ID:20260115T230000Z", "SUMMARY:no start", start=None, uid="gone"),
            vevent("RECURRENCE-ID:20261111T230000Z", "SUMMARY:kept", start=None, uid="gone"),
        )
        assert [(draft.native_id, draft.title, draft.start) for draft in drafts] == [
            ("gone/20260101T230000Z", "moved in", datetime(2026, 11, 4, 23, 0, tzinfo=UTC)),
            ("gone/20261111T230000Z", "kept", datetime(2026, 11, 11, 23, 0, tzinfo=UTC)),
        ]

    def test_an_override_of_an_event_without_recurrence_replaces_it(self) -> None:
        drafts = expand(
            vevent("SUMMARY:Talk", uid="a", start=f"DTSTART;{LA}:20261103T090000"),
            vevent(
                "RECURRENCE-ID:20261103T170000Z",
                "DTSTART:20261104T170000Z",
                "SUMMARY:Moved talk",
                uid="a",
                start=None,
            ),
            vevent("SUMMARY:Other", uid="b", start="DTSTART:20261105T170000Z"),
            vevent("RECURRENCE-ID:20261101T170000Z", "SUMMARY:Elsewhere", uid="b", start=None),
        )
        assert [(draft.native_id, draft.start, draft.title) for draft in drafts] == [
            ("a", datetime(2026, 11, 4, 17, 0, tzinfo=UTC), "Moved talk"),
            ("b", datetime(2026, 11, 5, 17, 0, tzinfo=UTC), "Other"),
            ("b/20261101T170000Z", datetime(2026, 11, 1, 17, 0, tzinfo=UTC), "Elsewhere"),
        ]

    def test_an_override_without_usable_times_spares_its_series(self) -> None:
        drafts = expand(
            vevent(
                "DTEND:20261021T000000Z",
                "RRULE:FREQ=WEEKLY;COUNT=4",
                "SUMMARY:Weekly",
                start="DTSTART:20261020T230000Z",
            ),
            vevent("RECURRENCE-ID:20261027T230000Z", "STATUS:CANCELLED", start=None),
            vevent("RECURRENCE-ID:20261103T230000Z", "SUMMARY:broken", start="DTSTART:garbage"),
            vevent("RECURRENCE-ID:20261110T230000Z", "DURATION:PT2H", "SUMMARY:Longer", start=None),
        )
        found = [(d.native_id, d.start, d.end, d.title, d.status_hint) for d in drafts]
        hour = timedelta(hours=1)
        assert found == [
            ("talk-1/20261020T230000Z", WEEK[0], WEEK[0] + hour, "Weekly", None),
            ("talk-1/20261027T230000Z", WEEK[1], WEEK[1] + hour, None, "cancelled"),
            ("talk-1/20261103T230000Z", WEEK[2], WEEK[2] + hour, "Weekly", None),
            ("talk-1/20261110T230000Z", WEEK[3], WEEK[3] + 2 * hour, "Longer", None),
        ]

    def test_until_in_utc_with_a_floating_start_and_until_as_a_date(self) -> None:
        floating = expand(
            vevent("RRULE:FREQ=WEEKLY;UNTIL=20261028T000000Z", start="DTSTART:20261013T160000")
        )
        # UNTIL is 2026-10-27 17:00 PDT, so the occurrence at 16:00 that day is the last one
        assert [draft.start for draft in floating] == [
            datetime(2026, 10, 20, 16, 0),
            datetime(2026, 10, 27, 16, 0),
        ]
        by_date = expand(
            vevent(
                "RRULE:FREQ=WEEKLY;UNTIL=20261027",
                start="DTSTART;TZID=America/Los_Angeles:20261013T160000",
            )
        )
        assert [draft.start for draft in by_date] == [
            datetime(2026, 10, 20, 16, 0, tzinfo=PACIFIC),
            datetime(2026, 10, 27, 16, 0, tzinfo=PACIFIC),
        ]
        naive_until = expand(
            vevent("RRULE:FREQ=WEEKLY;UNTIL=20261021T000000", start="DTSTART:20261013T230000Z")
        )
        assert [draft.start for draft in naive_until] == [datetime(2026, 10, 20, 23, 0, tzinfo=UTC)]

    def test_floating_recurrences_use_the_zone_of_the_source(self) -> None:
        drafts = expand(vevent("RRULE:FREQ=DAILY;COUNT=40", start="DTSTART:20261016T230000"))
        # 23:00 on 19 October is before midnight PDT of the 20th, the first day of the window
        assert drafts[0].start == datetime(2026, 10, 20, 23, 0)
        assert drafts[-1].start == datetime(2026, 11, 16, 23, 0)
        assert [draft.native_id for draft in drafts][:1] == ["talk-1/20261020T230000"]

    def test_all_day_recurrences_are_dates(self) -> None:
        drafts = parse(
            calendar(
                vevent(
                    "DTEND;VALUE=DATE:20261003",
                    "RRULE:FREQ=MONTHLY;BYMONTHDAY=1",
                    "EXDATE;VALUE=DATE:20261101",
                    start="DTSTART;VALUE=DATE:20261001",
                )
            ),
            window=TimeWindow(
                start=datetime(2026, 9, 1, 7, tzinfo=UTC), end=datetime(2027, 1, 2, 8, tzinfo=UTC)
            ),
        )
        assert [(draft.start, draft.end, draft.native_id) for draft in drafts] == [
            (date(2026, 10, 1), date(2026, 10, 2), "talk-1/20261001"),
            (date(2026, 12, 1), date(2026, 12, 2), "talk-1/20261201"),
            (date(2027, 1, 1), date(2027, 1, 2), "talk-1/20270101"),
        ]

    def test_rdate_alone_and_periods_are_read(self) -> None:
        drafts = expand(
            vevent(
                "RDATE:20261021T230000Z,20261028T230000Z",
                "RDATE;VALUE=PERIOD:20261030T230000Z/PT1H",
                start="DTSTART:20261014T230000Z",
            )
        )
        assert [draft.start for draft in drafts] == [
            datetime(2026, 10, 21, 23, 0, tzinfo=UTC),
            datetime(2026, 10, 28, 23, 0, tzinfo=UTC),
        ]

    def test_expansion_is_capped(self) -> None:
        two_years = TimeWindow(start=WINDOW.start, end=WINDOW.start + timedelta(days=730))
        daily = vevent("RRULE:FREQ=DAILY", start="DTSTART:20261020T230000Z")
        assert len(parse(calendar(daily), window=two_years)) == MAX_OCCURRENCES
        # from 1990 the window is more than MAX_EXPANSION_STEPS days away
        assert expand(vevent("RRULE:FREQ=DAILY", start="DTSTART:19900101T230000Z")) == []

    def test_a_calendar_that_keeps_its_past_series(self) -> None:
        # Exports keep every series: 55 quarters of weekly talks, ended by COUNT or UNTIL.
        past = [
            vevent(
                f"RRULE:FREQ=WEEKLY;{'COUNT=10' if n % 2 else f'UNTIL={2010 + n // 4}1231'}",
                uid=f"q{n}",
                start=f"DTSTART:{2010 + n // 4}{1 + 3 * (n % 4):02d}05T230000Z",
            )
            for n in range(55)
        ]
        current = vevent("RRULE:FREQ=WEEKLY;COUNT=10", uid="now", start="DTSTART:20260922T230000Z")
        drafts = expand(*past, current)
        assert [draft.native_id for draft in drafts] == [
            f"now/2026{day}T230000Z" for day in ("1020", "1027", "1103", "1110")
        ]

    def test_series_that_ended_before_the_window_are_not_expanded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ics, "MAX_FILTERED_EVENTS", 1)
        monkeypatch.setattr(ics, "MAX_FEED_STEPS", 20)
        old = "DTSTART:19990101"
        ended = [
            vevent(f"RRULE:FREQ=DAILY;BYMONTH=1;UNTIL={2000 + n}1231", uid=f"o{n}", start=old)
            for n in range(5)
        ]
        again = vevent(
            "RRULE:FREQ=WEEKLY;UNTIL=20200101T000000Z",
            "RDATE:20261021T230000Z",
            uid="again",
            start="DTSTART:20191201T230000Z",
        )
        weekly = vevent("RRULE:FREQ=WEEKLY;COUNT=3", uid="now", start="DTSTART:20261020T230000Z")
        monthly = vevent(
            "RRULE:FREQ=MONTHLY;BYMONTHDAY=20;COUNT=2", uid="day", start="DTSTART:20261020T230000Z"
        )
        drafts = expand(*ended, again, weekly, monthly)
        assert [draft.native_id for draft in drafts] == [
            "again/20261021T230000Z",
            "now/20261020T230000Z",
            "now/20261027T230000Z",
            "now/20261103T230000Z",
            "day/20261020T230000Z",
        ]
        second = monthly.replace("UID:day", "UID:other")
        with pytest.raises(ParseError, match=r"example-talks: .*more than 1 recurring events"):
            expand(*ended, again, weekly, monthly, second)

    @pytest.mark.parametrize(
        ("rule", "filtered"),
        [
            ("FREQ=WEEKLY;BYDAY=TU,TH", False),
            ("FREQ=MONTHLY;BYDAY=-1FR", False),
            ("FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR", False),
            ("FREQ=YEARLY", False),
            ("FREQ=MONTHLY;BYMONTHDAY=15", True),
            ("FREQ=YEARLY;BYMONTH=6", True),
            ("FREQ=MONTHLY;BYDAY=MO;BYSETPOS=-1", True),
            ("FREQ=MONTHLY;BYDAY=5TU", True),
        ],
    )
    def test_only_rules_that_filter_are_counted(
        self, monkeypatch: pytest.MonkeyPatch, rule: str, filtered: bool
    ) -> None:
        monkeypatch.setattr(ics, "MAX_FILTERED_EVENTS", 0)
        event = vevent(f"RRULE:{rule}", start="DTSTART:20261020T230000Z")
        if filtered:
            with pytest.raises(ParseError, match="more than 0 recurring events"):
                expand(event)
        else:
            assert expand(event)

    def test_a_feed_whose_recurrences_need_too_many_steps_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ics, "MAX_FEED_STEPS", 12)
        expired = vevent("RRULE:FREQ=WEEKLY;COUNT=5", uid="old", start="DTSTART:20200107T230000Z")
        current = vevent("RRULE:FREQ=DAILY;COUNT=7", uid="now", start="DTSTART:20261020T230000Z")
        assert len(expand(expired, current)) == 7
        more = vevent("RRULE:FREQ=DAILY;COUNT=2", uid="more", start="DTSTART:20261101T230000Z")
        with pytest.raises(ParseError, match=r"more than 12 steps: https://calendar\.example"):
            expand(expired, current, more)

    def test_a_feed_with_too_many_recurring_events_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Every expanded series counts, also one that ends by COUNT or starts after the window,
        # because the expansion may walk to the year 9999 before it knows; one ended by UNTIL
        # before the window is not expanded.
        monkeypatch.setattr(ics, "MAX_RECURRING_EVENTS", 2)
        ended = vevent("RRULE:FREQ=WEEKLY;UNTIL=20200101", uid="u", start="DTSTART:20191001")
        counted = vevent("RRULE:FREQ=WEEKLY;COUNT=5", uid="c", start="DTSTART:20200107T230000Z")
        later = vevent("RRULE:FREQ=WEEKLY", uid="later", start="DTSTART:20270105T230000Z")
        assert expand(ended, counted, later) == []
        current = vevent("RRULE:FREQ=WEEKLY;COUNT=2", uid="now", start="DTSTART:20261020T230000Z")
        with pytest.raises(ParseError, match=r"more than 2 recurring events that do not end"):
            expand(ended, counted, later, current)

    @pytest.mark.parametrize(
        "rule",
        [
            "FREQ=HOURLY",
            "FREQ=SECONDLY;BYMONTH=2;BYMONTHDAY=30",
            "BYDAY=MO",
            "FREQ=YEARLY;BYYEARDAY=100",
            "FREQ=YEARLY;BYWEEKNO=20",
            "FREQ=WEEKLY;BYDAY=MO;BYSETPOS=2",
            "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30",
            "FREQ=YEARLY;BYMONTH=4,6;BYMONTHDAY=31,-31",
            "FREQ=MONTHLY;BYDAY=MO;BYSETPOS=6",
            "FREQ=MONTHLY;BYMONTHDAY=1,15;BYSETPOS=3",
            "FREQ=YEARLY;BYMONTH=3;BYDAY=1MO,-1MO;BYSETPOS=-3",
            "FREQ=MONTHLY;BYDAY=6MO",
            "FREQ=YEARLY;BYDAY=54MO",
            "FREQ=YEARLY;BYMONTH=2;BYDAY=-6FR",
            "FREQ=WEEKLY;BYDAY=2MO",
            "FREQ=YEARLY;BYMONTH=13",
            "FREQ=WEEKLY;BYMONTHDAY=13",
            "FREQ=MONTHLY;BYMONTHDAY=32",
            "FREQ=MONTHLY;BYMONTHDAY=0",
            "FREQ=MONTHLY;BYDAY=-1FR;BYMONTHDAY=13",
            "FREQ=YEARLY;BYDAY=5MO;BYMONTHDAY=13",
            # RFC 5545 takes the union of plain and ordinal weekdays, the library the
            # intersection: no day at all when the weekdays differ
            "FREQ=MONTHLY;BYDAY=TH,1FR",
            "FREQ=YEARLY;BYDAY=TU,4WE",
            "FREQ=YEARLY;BYMONTH=3;BYDAY=-1MO,FR",
            "FREQ=MONTHLY;BYDAY=TH,1TH;COUNT=5",
        ],
    )
    def test_rules_the_expansion_cannot_bound_are_skipped(self, rule: str) -> None:
        assert expand(vevent(f"RRULE:{rule}", start="DTSTART:20261020T230000Z")) == []
        with pytest.raises(ValueError, match=r"unsupported|BYSETPOS|BYMONTH|BYDAY"):
            check_rule(dict(icalendar.vRecur.from_ical(rule)))

    @pytest.mark.parametrize(
        ("rule", "refused"),
        [
            ("FREQ=DAILY;INTERVAL=7;BYDAY=TU", True),
            ("FREQ=DAILY;INTERVAL=14;BYDAY=WE,TH", True),
            ("FREQ=DAILY;INTERVAL=2;BYDAY=TU;BYMONTHDAY=13", True),
            ("FREQ=MONTHLY;INTERVAL=2;BYMONTH=2", True),
            ("FREQ=MONTHLY;INTERVAL=12;BYMONTH=6,12", True),
            ("FREQ=DAILY;INTERVAL=0", True),
            ("FREQ=DAILY;INTERVAL=7;BYDAY=MO,TU", False),
            ("FREQ=DAILY;INTERVAL=3;BYDAY=TU", False),
            ("FREQ=DAILY;INTERVAL=2;BYMONTHDAY=13", False),
            ("FREQ=DAILY;BYDAY=TU;BYMONTHDAY=13", False),
            ("FREQ=MONTHLY;INTERVAL=2;BYMONTH=1,2", False),
            ("FREQ=MONTHLY;INTERVAL=3;BYMONTH=2", False),
        ],
    )
    def test_an_interval_that_never_meets_the_filters_is_refused(
        self, rule: str, refused: bool
    ) -> None:
        # DTSTART is Monday, 16 November 2026: the daily rules step on Mondays only when
        # INTERVAL is a multiple of 7, and every second month from November skips February.
        drafts = expand(vevent(f"RRULE:{rule}", start="DTSTART:20261116T090000"))
        assert (drafts == []) == refused
        parts: dict[str, list[Any]] = dict(icalendar.vRecur.from_ical(rule))
        check_rule(parts)
        if refused:
            with pytest.raises(ValueError, match=r"INTERVAL|BYDAY|BYMONTH"):
                check_rule(parts, start=date(2026, 11, 16))
        else:
            check_rule(parts, start=date(2026, 11, 16))

    @pytest.mark.parametrize(
        "rule",
        [
            "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29",
            "FREQ=MONTHLY;BYDAY=TU;BYSETPOS=-1",
            "FREQ=MONTHLY;BYMONTH=4,5;BYMONTHDAY=31",
            "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;WKST=SU",
            "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1",
            "FREQ=YEARLY;BYDAY=MO;BYSETPOS=50",
            "FREQ=MONTHLY;BYDAY=MO;BYHOUR=9,14;BYSETPOS=10",
            "FREQ=MONTHLY;BYDAY=-1FR",
            "FREQ=YEARLY;BYDAY=20TH",
        ],
    )
    def test_rules_of_talk_calendars_are_accepted(self, rule: str) -> None:
        check_rule(dict(icalendar.vRecur.from_ical(rule)))

    def test_a_recurring_event_without_uid_has_no_native_id(self) -> None:
        drafts = expand(
            vevent("RRULE:FREQ=WEEKLY;COUNT=2", uid=None, start="DTSTART:20261020T230000Z")
        )
        assert [draft.native_id for draft in drafts] == [None, None]

    def test_instance_keys(self) -> None:
        assert instance_key(date(2026, 11, 17)) == "20261117"
        assert instance_key(datetime(2026, 11, 17, 16, 0)) == "20261117T160000"
        assert instance_key(datetime(2026, 11, 17, 16, 0, tzinfo=PACIFIC)) == "20261118T000000Z"

    def test_occurrences_of_a_calendar(self) -> None:
        feed = read_calendar(
            make_document(
                url=FEED,
                text=calendar(
                    vevent("RRULE:FREQ=WEEKLY;COUNT=3", start="DTSTART:20261020T230000Z")
                ),
            )
        )
        found = occurrences(feed, WINDOW, timezone="UTC")
        assert [item.native_id for item in found] == [
            "talk-1/20261020T230000Z",
            "talk-1/20261027T230000Z",
            "talk-1/20261103T230000Z",
        ]
        assert found[0].end is None
        assert isinstance(feed, IcsCalendar)


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

    def test_a_single_pattern_is_a_list_of_one(self) -> None:
        params = ics_params(example_source(patterns={"SUMMARY": "^(?P<title>.+)$"}))
        assert [pattern.pattern for pattern in params.patterns["SUMMARY"]] == ["^(?P<title>.+)$"]


def test_the_adapter_keeps_no_state() -> None:
    assert vars(IcsAdapter()) == {}
    assert ics.MAX_OCCURRENCES == MAX_OCCURRENCES
