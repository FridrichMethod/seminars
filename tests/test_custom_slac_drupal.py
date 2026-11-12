"""The slac_drupal adapter: card lists and detail pages of the SLAC Drupal theme.

The recorded pages of the two configured sources come first, then hand-written
pages for the edge cases (times, identity, abstracts, fetching, parameters).
"""

from __future__ import annotations

import base64
import functools
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest

from factories import make_document, make_payload, make_source, make_window
from fakes import FakeHttpClient
from seminars.config.loader import load_sources
from seminars.http import offline_client
from seminars.models import TBA_TITLE, EventDraft, SourceConfig, TimeWindow
from seminars.sources import base
from seminars.sources.base import ForeignHostError, ParseError
from seminars.sources.custom import slac_drupal
from seminars.sources.custom.slac_drupal import (
    SlacDrupalAdapter,
    SlacDrupalParams,
    calendar_text,
    card_times,
    without_page_link,
)
from seminars.sources.generic.common import AdapterConfigError, load_params

REPO_ROOT = Path(__file__).resolve().parents[1]
PACIFIC = ZoneInfo("America/Los_Angeles")
RECORDED = datetime(2026, 11, 11, 17, 28, tzinfo=UTC)
COLLOQUIUM = "bayarea-slac-colloquium"
THEORY = "bayarea-slac-theory"
SITE = "https://colloquium.slac.stanford.edu"


@functools.cache
def configured() -> dict[str, SourceConfig]:
    sources = load_sources(REPO_ROOT / "config" / "sources.d")
    return {source.id: source for source in sources if source.adapter == "slac_drupal"}


def run(
    source: SourceConfig, http: Any, window: TimeWindow | None = None
) -> tuple[list[str], list[EventDraft]]:
    adapter = SlacDrupalAdapter()
    payload = adapter.fetch(http, source, window=window or make_window(RECORDED))
    return list(http.requests), adapter.parse(payload, source)


@functools.cache
def recorded() -> tuple[tuple[str, ...], tuple[EventDraft, ...]]:
    requests, drafts = run(configured()[COLLOQUIUM], offline_client(COLLOQUIUM))
    return tuple(requests), tuple(drafts)


def by_title(drafts: Any, title: str) -> list[EventDraft]:
    return [draft for draft in drafts if draft.title == title]


def speakers(draft: EventDraft) -> list[tuple[str, str | None]]:
    return [(speaker.name, speaker.affiliation) for speaker in draft.speakers]


# --- registration and configuration --------------------------------------------------------


def test_the_module_registers_the_adapter() -> None:
    assert isinstance(base.get_adapter("slac_drupal"), SlacDrupalAdapter)


def test_the_configured_sources_keep_their_state_and_their_params_load() -> None:
    sources = configured()
    assert set(sources) == {COLLOQUIUM, THEORY}
    assert sources[COLLOQUIUM].enabled
    assert not sources[THEORY].enabled
    assert sources[COLLOQUIUM].default_fields == ["physics"]
    colloquium = load_params(SlacDrupalParams, sources[COLLOQUIUM])
    assert colloquium.native_id == "vevent_uid"
    assert colloquium.detail is not None
    assert colloquium.max_detail_pages == 12
    theory = load_params(SlacDrupalParams, sources[THEORY])
    assert theory.detail is None
    assert theory.native_id is None


# --- the recorded SLAC Colloquium ----------------------------------------------------------


class TestRecordedColloquium:
    def test_fetch_requests_the_list_and_the_pages_of_the_talks(self) -> None:
        requests, _ = recorded()
        assert requests == (
            f"{SITE}/upcoming-events",
            f"{SITE}/events/2026-11-09-single-crystal-halide-perovskite-heterostructures",
            f"{SITE}/events/2026-10-22-how-spot-supernova-deep-underground",
            f"{SITE}/events/2026-10-22-talk-phil-bucksbaum",
            f"{SITE}/events/2026-10-22-talk-thomas-feurer",
        )

    def test_four_talks_and_no_placeholder(self) -> None:
        _, drafts = recorded()
        assert len(drafts) == 4
        assert {draft.extraction for draft in drafts} == {"html"}
        assert not [draft for draft in drafts if "No SLAC Colloquium" in (draft.title or "")]
        assert all(draft.location == "Kavli Auditorium and Zoom" for draft in drafts)  # default
        assert all(draft.native_id for draft in drafts)

    def test_a_talk_with_speaker_and_affiliation(self) -> None:
        (draft,) = by_title(recorded()[1], "Single-Crystal Halide Perovskite Heterostructures")
        assert draft.native_id == "6d15b0f6-d808-4a70-b407-7c7140db97d1"
        assert draft.start == datetime(2026, 11, 16, 16, 0, tzinfo=PACIFIC)
        assert isinstance(draft.start, datetime)
        assert draft.start.utcoffset() == timedelta(hours=-8)
        assert draft.end == datetime(2026, 11, 16, 17, 0, tzinfo=PACIFIC)
        assert speakers(draft) == [("Hemamala (Hema) Karunadasa", "Stanford Univerisity")]
        assert draft.location == "Kavli Auditorium and Zoom"
        assert draft.url == (
            f"{SITE}/events/2026-11-09-single-crystal-halide-perovskite-heterostructures"
        )
        assert draft.abstract is None
        assert draft.abstract_html is not None
        assert "Layered heterostructures, formed one monolayer at a time" in draft.abstract_html
        assert "Directed assembly of layered perovskite heterostructures" in draft.abstract_html

    def test_the_related_events_block_is_ignored(self) -> None:
        (draft,) = by_title(recorded()[1], "How to Spot a Supernova from Deep Underground")
        assert draft.native_id == "cf88ac64-2c05-4eb1-a2bd-dcddd36e368d"
        assert draft.start == datetime(2026, 11, 30, 16, 0, tzinfo=PACIFIC)
        assert speakers(draft) == [("Kate Scholberg", None)]
        assert draft.abstract_html is not None
        assert "Stellar core collapses" in draft.abstract_html
        assert "Layered heterostructures" not in draft.abstract_html

    def test_talks_without_a_title_yet(self) -> None:
        drafts = by_title(recorded()[1], TBA_TITLE)
        assert [(draft.start, speakers(draft)) for draft in drafts] == [
            (datetime(2026, 12, 7, 16, 0, tzinfo=PACIFIC), [("Phil Bucksbaum", None)]),
            (datetime(2026, 12, 14, 16, 0, tzinfo=PACIFIC), [("Thomas Feurer", None)]),
        ]
        assert all(draft.abstract is None and draft.abstract_html is None for draft in drafts)
        assert [draft.native_id for draft in drafts] == [
            "2b677546-32fe-4ece-b4f9-18fa1af28886",
            "652094f3-b782-4e1e-bdc5-9bdc3c58b198",
        ]


class TestRecordedTheoryGroup:
    def test_the_empty_list_gives_no_drafts(self) -> None:
        requests, drafts = run(configured()[THEORY], offline_client(THEORY))
        assert requests == ["https://theory.slac.stanford.edu/events"]
        assert drafts == []

    def test_past_cards_are_read_from_the_list_alone(self) -> None:
        source = configured()[THEORY].evolve(
            url="https://theory.slac.stanford.edu/events/past-events"
        )
        window = TimeWindow(
            start=datetime(2024, 1, 1, tzinfo=UTC), end=datetime(2025, 1, 1, tzinfo=UTC)
        )
        requests, drafts = run(source, offline_client(THEORY), window)
        assert requests == ["https://theory.slac.stanford.edu/events/past-events"]
        assert len(drafts) == 9
        first = drafts[0]
        # A card names only its speaker: the talk has no title yet.
        assert first.title == "TBA"
        assert first.start == datetime(2024, 12, 4, 12, 30)
        assert first.end == datetime(2024, 12, 4, 14, 30)
        assert first.native_id is None
        assert first.url == (
            "https://theory.slac.stanford.edu/events/2024-09-20-roberto-bruschini-ohio-state-u"
        )
        assert [(s.name, s.affiliation) for s in first.speakers] == [
            ("Roberto Bruschini", "Ohio State U.")
        ]
        assert {draft.title for draft in drafts} == {"TBA"}
        assert ("Anthony M Morales", "SLAC") in [
            (s.name, s.affiliation) for draft in drafts for s in draft.speakers
        ]


# --- hand-written pages --------------------------------------------------------------------

LIST = "https://events.example.org/upcoming"
EXAMPLE = "https://events.example.org"
CARD_DATE = "Monday, November 16, 2026 \u00b7 4:00 p.m.\u20135:00 p.m.\u00a0\u00a0PT"
ICON = '<svg class="c-icon"><title>Date</title><use></use></svg>'


def card(
    title: str = "A talk",
    link: str | None = "/events/a-talk",
    date_text: str | None = CARD_DATE,
    *,
    speaker: str | None = None,
    affiliation: str | None = None,
    abstract: str | None = None,
) -> str:
    parts = [f'<h3 class="c-card__title"><a href="{link}">{title}</a></h3>' if link else ""]
    if not link:
        parts.append(f'<h3 class="c-card__title"><a>{title}</a></h3>')
    if abstract is not None:
        parts.append(f'<div class="c-field--name-field-body"><div>{abstract}</div></div>')
    if speaker is not None:
        parts.append(
            f'<div class="slac-speaker"><div class="speaker-name"><p>{speaker}</p></div>'
            f'<div class="speaker-affiliation"><p>{affiliation or ""}</p></div></div>'
        )
    if date_text is not None:
        parts.append(f'<div class="c-card__date">{ICON} {date_text}</div>')
    return f'<div class="c-view__row"><div class="c-card">{"".join(parts)}</div></div>'


def list_page(*cards: str, empty: bool = False) -> str:
    marker = '<div class="c-view__empty"><div>No items to display.</div></div>' if empty else ""
    return f"<html><body><div class='c-view'>{''.join(cards)}{marker}</div></body></html>"


def vcalendar(
    uid: str | None = "uid-1",
    start: str = "DTSTART;TZID=America/Los_Angeles:20261116T160000",
    end: str | None = "DTEND;TZID=America/Los_Angeles:20261116T170000",
    description: str | None = None,
    summary: str = "A talk",
) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:Colloquium Series at SLAC",
        "BEGIN:VTIMEZONE",
        "TZID:America/Los_Angeles",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:-0800",
        "TZOFFSETTO:-0800",
        "END:STANDARD",
        "END:VTIMEZONE",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        *([f"UID:{uid}"] if uid else []),
        f"SUMMARY:{summary}",
        "DTSTAMP:20261110T131300Z",
        start,
        *([end] if end else []),
        *([f"DESCRIPTION:{description}"] if description else []),
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines)


def data_uri(calendar: str) -> str:
    return "data:text/calendar;charset=utf8," + quote(calendar, safe=":;=/-")


def detail_page(
    title: str = "A talk",
    calendar: str | None = None,
    *,
    lede: str | None = None,
    abstract: str | None = "<p>Abstract: The real one.</p>",
    href: str | None = None,
) -> str:
    link = href if href is not None else (data_uri(calendar) if calendar else None)
    ical = f'<a href="{link}" class="addtocal__ical">iCal</a>' if link else ""
    head = f'<header class="c-page-title"><h1><span>{title}</span></h1>'
    head += f'<div class="c-lede"><p>{lede}</p></div></header>' if lede else "</header>"
    body = (
        f'<div class="event-detail__content"><div class="c-wysiwyg">{abstract}</div></div>'
        if abstract is not None
        else ""
    )
    related = (
        '<div class="c-view--pre-filtered-content-lists"><div class="c-card">'
        '<div class="event-detail__content"><div class="c-wysiwyg">Another talk</div></div>'
        f'<a href="{data_uri(vcalendar(uid="other-uid"))}">iCal</a>'
        '<header class="c-page-title"><h1>Other</h1></header></div></div>'
    )
    return f"<html><body>{head}{body}{ical}{related}</body></html>"


# icalendar 7.3 raises AttributeError (not ValueError) on this one.
BROKEN_VTIMEZONE = (
    vcalendar()
    .replace("BEGIN:VTIMEZONE", "BEGIN:VTIMEZO;E")
    .replace("TZID:America/Los_Angeles\r\nBEGIN:STANDARD", "TZID:Custom/Zone\r\nBEGIN:STANDARD")
)
# A zone name too long for a file name: the zone lookup raises OSError. icalendar
# caches the VTIMEZONEs it has read by TZID for the whole process, so the zone a
# test defines has a name of its own.
LONG_TZID = "Z" * 300
DEFINED_LONG_TZID = "Y" * 300


COLLOQUIUM_PARAMS: dict[str, Any] = {
    "native_id": "vevent_uid",
    "list": {
        "card": "div.c-view__row div.c-card",
        "title": "h3.c-card__title a",
        "link": "h3.c-card__title a",
        "speaker": ".slac-speaker .speaker-name",
        "affiliation": ".slac-speaker .speaker-affiliation",
        "abstract": ".c-field--name-field-body",
        "date_text": ".c-card__date",
        "empty": "div.c-view__empty",
    },
    "detail": {
        "exclude": "div.c-view--pre-filtered-content-lists",
        "ical_link": 'a[href^="data:text/calendar"]',
        "title": ".c-page-title h1",
        "speaker": ".c-page-title .c-lede",
        "abstract": ".event-detail__content .c-wysiwyg",
    },
    "max_detail_pages": 5,
    "skip_title_patterns": ["^No (SLAC )?Colloquium\\b"],
    "tba_title_patterns": ["^Talk by (?P<speaker>.+)$"],
    "tba_abstract_patterns": ["^Info coming soon\\b"],
}


def example_source(**params: Any) -> SourceConfig:
    return make_source(
        id="bayarea-example",
        name="Example colloquium",
        group="bayarea",
        adapter="slac_drupal",
        url=LIST,
        institution="slac",
        host_org="Example",
        default_fields=["physics"],
        params={**COLLOQUIUM_PARAMS, **params},
    )


def site(listing: str, details: dict[str, str]) -> FakeHttpClient:
    http = FakeHttpClient({LIST: listing})
    for path, text in details.items():
        http.add(f"{EXAMPLE}{path}", text)
    return http


def one(listing: str, details: dict[str, str], **params: Any) -> EventDraft:
    (draft,) = run(example_source(**params), site(listing, details))[1]
    return draft


class TestTimes:
    def test_a_summer_talk_is_on_daylight_time_whatever_the_vtimezone_says(self) -> None:
        calendar = vcalendar(
            start="DTSTART;TZID=America/Los_Angeles:20260715T160000",
            end="DTEND;TZID=America/Los_Angeles:20260715T170000",
        )
        window = TimeWindow(
            start=datetime(2026, 7, 1, tzinfo=UTC), end=datetime(2026, 8, 1, tzinfo=UTC)
        )
        listing = list_page(card(date_text="Wednesday, July 15, 2026"))
        source = example_source()
        (draft,) = run(
            source, site(listing, {"/events/a-talk": detail_page("A talk", calendar)}), window
        )[1]
        assert isinstance(draft.start, datetime)
        assert draft.start.utcoffset() == timedelta(hours=-7)
        assert draft.start.astimezone(UTC) == datetime(2026, 7, 15, 23, 0, tzinfo=UTC)
        assert draft.end == datetime(2026, 7, 15, 17, 0, tzinfo=PACIFIC)

    def test_utc_and_unknown_zones(self) -> None:
        calendar = vcalendar(
            start="DTSTART:20261117T000000Z", end="DTEND;TZID=Not/AZone:20261116T170000"
        )
        draft = one(list_page(card()), {"/events/a-talk": detail_page(calendar=calendar)})
        assert draft.start == datetime(2026, 11, 17, 0, 0, tzinfo=UTC)
        assert draft.end == datetime(2026, 11, 16, 17, 0)  # floating: the source's zone

    def test_a_utc_end_keeps_its_instant_after_a_zoned_start(self) -> None:
        calendar = vcalendar(end="DTEND:20261117T010000Z")
        draft = one(list_page(card()), {"/events/a-talk": detail_page(calendar=calendar)})
        assert draft.start == datetime(2026, 11, 16, 16, 0, tzinfo=PACIFIC)
        assert draft.end == datetime(2026, 11, 17, 1, 0, tzinfo=UTC)
        assert isinstance(draft.end, datetime)
        assert draft.end.utcoffset() == timedelta(0)

    def test_an_over_long_tzid_defined_in_the_calendar_is_floating(self) -> None:
        calendar = (
            vcalendar(
                start=f"DTSTART;TZID={DEFINED_LONG_TZID}:20261116T160000",
                end=f"DTEND;TZID={DEFINED_LONG_TZID}:20261116T170000",
            )
            .replace("TZID:America/Los_Angeles", f"TZID:{DEFINED_LONG_TZID}")
            .replace("BEGIN:STANDARD", "BEGIN:STANDARD\r\nDTSTART:19701101T020000")
        )
        draft = one(list_page(card()), {"/events/a-talk": detail_page(calendar=calendar)})
        assert draft.start == datetime(2026, 11, 16, 16, 0)  # floating: the source's zone
        assert draft.end == datetime(2026, 11, 16, 17, 0)

    def test_no_dtend_means_no_end(self) -> None:
        draft = one(
            list_page(card()), {"/events/a-talk": detail_page(calendar=vcalendar(end=None))}
        )
        assert draft.start == datetime(2026, 11, 16, 16, 0, tzinfo=PACIFIC)
        assert draft.end is None

    def test_a_duration_ends_in_the_zone_of_the_start(self) -> None:
        calendar = vcalendar(
            start="DTSTART;TZID=America/Los_Angeles:20260715T160000", end="DURATION:PT90M"
        )
        window = TimeWindow(
            start=datetime(2026, 7, 1, tzinfo=UTC), end=datetime(2026, 8, 1, tzinfo=UTC)
        )
        listing = list_page(card(date_text="Wednesday, July 15, 2026"))
        page = detail_page("A talk", calendar)
        (draft,) = run(example_source(), site(listing, {"/events/a-talk": page}), window)[1]
        assert isinstance(draft.end, datetime)
        assert draft.end.utcoffset() == timedelta(hours=-7)
        assert draft.end == datetime(2026, 7, 15, 17, 30, tzinfo=PACIFIC)

    def test_an_all_day_event_ends_on_its_last_day(self) -> None:
        calendar = vcalendar(start="DTSTART;VALUE=DATE:20261116", end="DTEND;VALUE=DATE:20261119")
        draft = one(list_page(card()), {"/events/a-talk": detail_page(calendar=calendar)})
        assert draft.start == date(2026, 11, 16)
        assert draft.end == date(2026, 11, 18)
        assert draft.is_all_day

    def test_without_a_vevent_the_card_date_is_local_wall_time(self) -> None:
        draft = one(list_page(card()), {}, native_id=None, detail=None)
        assert draft.start == datetime(2026, 11, 16, 16, 0)
        assert draft.end == datetime(2026, 11, 16, 17, 0)
        assert draft.native_id is None
        assert draft.url == f"{EXAMPLE}/events/a-talk"

    def test_a_card_without_any_date_gives_no_draft_without_a_vevent(self) -> None:
        listing = list_page(card(date_text=None), card("Other", "/events/other"))
        _, drafts = run(example_source(native_id=None, detail=None), site(listing, {}))
        assert [draft.title for draft in drafts] == ["Other"]

    def test_a_card_date_without_time_is_all_day(self) -> None:
        listing = list_page(card(date_text="Monday, November 16, 2026"))
        draft = one(listing, {}, native_id=None, detail=None)
        assert draft.start == date(2026, 11, 16)
        assert draft.is_all_day

    @pytest.mark.parametrize(
        ("text", "start", "end"),
        [
            (CARD_DATE, datetime(2026, 11, 16, 16, 0), datetime(2026, 11, 16, 17, 0)),
            (
                "Friday, November 22, 2024 \u00b7 12:30 p.m.\u20132:30 p.m. PT",
                datetime(2024, 11, 22, 12, 30),
                datetime(2024, 11, 22, 14, 30),
            ),
            ("Monday, November 16, 2026 \u00b7 4 pm", datetime(2026, 11, 16, 16, 0), None),
            ("Monday, November 16, 2026 \u00b7 TBA", date(2026, 11, 16), None),
            ("Monday, November 16, 2026 \u00b7 5 pm - 4 pm", datetime(2026, 11, 16, 17, 0), None),
            ("November 16", date(2026, 11, 16), None),
            ("Coming soon", None, None),
            ("", None, None),
            (None, None, None),
        ],
    )
    def test_card_times(self, text: str | None, start: Any, end: Any) -> None:
        assert card_times(text, date(2026, 11, 11)) == (start, end)


class TestIdentityAndDropping:
    def test_the_vevent_uid_is_the_native_id(self) -> None:
        draft = one(list_page(card()), {"/events/a-talk": detail_page(calendar=vcalendar())})
        assert draft.native_id == "uid-1"

    @pytest.mark.parametrize(
        "page",
        [
            pytest.param(detail_page(), id="no-ical-link"),
            pytest.param(detail_page(href="data:text/calendar,not a calendar"), id="no-calendar"),
            pytest.param(detail_page(href="data:text/calendar;base64,@@@"), id="bad-base64"),
            pytest.param(detail_page(href="data:text/calendar;base64,abc"), id="bad-padding"),
            pytest.param(detail_page(calendar=BROKEN_VTIMEZONE), id="broken-vtimezone"),
            pytest.param(
                detail_page(calendar=vcalendar(start=f"DTSTART;TZID={LONG_TZID}:20261116T160000")),
                id="over-long-tzid",
            ),
            pytest.param(detail_page(calendar=vcalendar(uid=None)), id="no-uid"),
            pytest.param(
                detail_page(calendar=vcalendar(start="DTSTART:garbage")), id="broken-start"
            ),
        ],
    )
    def test_a_card_without_a_usable_vevent_is_dropped(self, page: str) -> None:
        listing = list_page(card(), card("Other", "/events/other"))
        details = {"/events/a-talk": page, "/events/other": detail_page("Other", vcalendar())}
        _, drafts = run(example_source(), site(listing, details))
        assert [draft.title for draft in drafts] == ["Other"]

    def test_without_native_id_a_card_without_vevent_still_counts(self) -> None:
        draft = one(list_page(card()), {"/events/a-talk": detail_page()}, native_id=None)
        assert draft.native_id is None
        assert draft.start == datetime(2026, 11, 16, 16, 0)
        assert draft.abstract_html == "<p>Abstract: The real one.</p>"

    def test_a_base64_calendar_uri(self) -> None:
        encoded = base64.b64encode(vcalendar(uid="b64").encode()).decode()
        page = detail_page(href=f"data:text/calendar;base64,{encoded}")
        assert one(list_page(card()), {"/events/a-talk": page}).native_id == "b64"

    def test_a_detail_page_that_fails_is_skipped_and_the_rest_kept(self) -> None:
        listing = list_page(card(), card("Other", "/events/other"))
        http = site(listing, {"/events/other": detail_page("Other", vcalendar(uid="u2"))})
        http.add(f"{EXAMPLE}/events/a-talk", "gone", status=404)
        requests, drafts = run(example_source(), http)
        assert requests == [LIST, f"{EXAMPLE}/events/a-talk", f"{EXAMPLE}/events/other"]
        assert [draft.native_id for draft in drafts] == ["u2"]

    def test_placeholders_are_neither_fetched_nor_returned(self) -> None:
        listing = list_page(card("No SLAC Colloquium 11/23", "/events/none"), card())
        requests, drafts = run(
            example_source(),
            site(listing, {"/events/a-talk": detail_page(calendar=vcalendar())}),
        )
        assert requests == [LIST, f"{EXAMPLE}/events/a-talk"]
        assert [draft.title for draft in drafts] == ["A talk"]

    def test_a_placeholder_title_on_the_detail_page_is_dropped_too(self) -> None:
        page = detail_page("No Colloquium this week", vcalendar())
        _, drafts = run(example_source(), site(list_page(card()), {"/events/a-talk": page}))
        assert drafts == []

    def test_a_malformed_card_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = slac_drupal.card_draft

        def refuse_first(params: Any, source: Any, document: Any, item: Any, *rest: Any) -> Any:
            if item.title == "A talk":
                raise ValueError("broken")
            return real(params, source, document, item, *rest)

        monkeypatch.setattr(slac_drupal, "card_draft", refuse_first)
        listing = list_page(card(), card("Other", "/events/other"))
        details = {
            "/events/a-talk": detail_page(calendar=vcalendar()),
            "/events/other": detail_page("Other", vcalendar(uid="u2")),
        }
        _, drafts = run(example_source(), site(listing, details))
        assert [draft.native_id for draft in drafts] == ["u2"]


class TestFields:
    def test_title_entities_and_a_non_ascii_speaker(self) -> None:
        listing = list_page(
            card("Q&amp;A on <em>lasers</em>", speaker="Jos\u00e9 Garc\u00eda", affiliation="")
        )
        page = detail_page("Q&amp;A on <em>lasers</em>", vcalendar())
        draft = one(listing, {"/events/a-talk": page})
        assert draft.title == "Q&A on lasers"
        assert speakers(draft) == [("Jos\u00e9 Garc\u00eda", None)]

    def test_the_detail_title_wins_over_the_card_and_the_summary(self) -> None:
        page = detail_page("The full title", vcalendar(summary="Summary"))
        assert one(list_page(card("Short")), {"/events/a-talk": page}).title == "The full title"
        page = detail_page("", vcalendar(summary="Summary"))
        assert one(list_page(card("")), {"/events/a-talk": page}).title == "Summary"

    def test_the_speaker_line_of_the_detail_page(self) -> None:
        page = detail_page(calendar=vcalendar(), lede="Jane Doe, Stanford University")
        assert speakers(one(list_page(card()), {"/events/a-talk": page})) == [
            ("Jane Doe", "Stanford University")
        ]
        page = detail_page(calendar=vcalendar(), lede="Jane Doe and John Roe")
        assert speakers(one(list_page(card()), {"/events/a-talk": page})) == [
            ("Jane Doe", None),
            ("John Roe", None),
        ]

    def test_the_card_speaker_wins(self) -> None:
        listing = list_page(card(speaker="Jane Doe", affiliation="MIT"))
        page = detail_page(calendar=vcalendar(), lede="Someone Else, Elsewhere")
        assert speakers(one(listing, {"/events/a-talk": page})) == [("Jane Doe", "MIT")]

    def test_a_tba_title_names_the_speaker(self) -> None:
        page = detail_page("Talk by Jane Doe", vcalendar(), abstract="<p>Info coming soon!</p>")
        draft = one(list_page(card("Talk by Jane Doe")), {"/events/a-talk": page})
        assert draft.title == TBA_TITLE
        assert speakers(draft) == [("Jane Doe", None)]
        assert draft.abstract is None
        assert draft.abstract_html is None

    def test_a_tba_title_without_a_speaker_group(self) -> None:
        page = detail_page("Seminar", vcalendar())
        draft = one(
            list_page(card("Seminar")), {"/events/a-talk": page}, tba_title_patterns=["^Seminar$"]
        )
        assert draft.title == TBA_TITLE
        assert draft.speakers == []

    def test_the_abstract_falls_back_to_the_description_then_to_the_card(self) -> None:
        description = "The abstract\\, in full. Link to event page: https://events.example.org/x"
        page = detail_page(calendar=vcalendar(description=description), abstract=None)
        draft = one(list_page(card(abstract="Teaser...")), {"/events/a-talk": page})
        assert draft.abstract == "The abstract, in full."
        assert draft.abstract_html is None
        page = detail_page(calendar=vcalendar(), abstract=None)
        draft = one(list_page(card(abstract="Teaser...")), {"/events/a-talk": page})
        assert draft.abstract is None
        assert draft.abstract_html == "<div>Teaser...</div>"

    def test_no_location_and_no_stream(self) -> None:
        draft = one(list_page(card()), {"/events/a-talk": detail_page(calendar=vcalendar())})
        assert draft.location is None
        assert draft.stream_url is None
        assert draft.is_online is None

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Abstract. Link to event page: https://x.example/a", "Abstract."),
            ("Abstract.Link to event page:https://x.example/a", "Abstract."),
            ("Link to event page: https://x.example/a", None),
            (
                "Mentions Link to event page: twice and more words",
                "Mentions Link to event page: twice and more words",
            ),
            ("No link at all", "No link at all"),
            (None, None),
        ],
    )
    def test_without_page_link(self, text: str | None, expected: str | None) -> None:
        assert without_page_link(text) == expected

    @pytest.mark.parametrize(
        ("uri", "expected"),
        [
            ("data:text/calendar;charset=utf8,BEGIN%3AVCALENDAR%0D%0A", "BEGIN:VCALENDAR\r\n"),
            ("DATA:TEXT/CALENDAR,A%20B", "A B"),
            ("data:text/calendar;base64," + base64.b64encode(b"BEGIN").decode(), "BEGIN"),
            ("data:text/calendar", None),
            ("data:text/html,<p>x</p>", None),
            ("https://example.org/event.ics", None),
            (None, None),
        ],
    )
    def test_calendar_text(self, uri: str | None, expected: str | None) -> None:
        assert calendar_text(uri) == expected


class TestFetching:
    def test_at_most_max_detail_pages_in_list_order(self, caplog: pytest.LogCaptureFixture) -> None:
        listing = list_page(*(card(f"Talk {n}", f"/events/{n}") for n in range(4)))
        details = {
            f"/events/{n}": detail_page(f"Talk {n}", vcalendar(uid=f"u{n}")) for n in range(4)
        }
        with caplog.at_level(logging.WARNING):
            requests, drafts = run(example_source(max_detail_pages=2), site(listing, details))
        assert requests == [LIST, f"{EXAMPLE}/events/0", f"{EXAMPLE}/events/1"]
        assert [draft.native_id for draft in drafts] == ["u0", "u1"]
        assert "beyond max_detail_pages" in caplog.text

    def test_cards_outside_the_window_are_not_fetched(self) -> None:
        listing = list_page(
            card("Old", "/events/old", "Monday, March 2, 2026 \u00b7 4:00 p.m."),
            card("Far", "/events/far", "Monday, November 15, 2027 \u00b7 4:00 p.m."),
            card(),
        )
        http = site(listing, {"/events/a-talk": detail_page(calendar=vcalendar())})
        requests, drafts = run(example_source(), http)
        assert requests == [LIST, f"{EXAMPLE}/events/a-talk"]
        assert len(drafts) == 1

    def test_links_to_other_hosts_are_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        listing = list_page(card("Elsewhere", "https://other.example.net/e"), card())
        http = site(listing, {"/events/a-talk": detail_page(calendar=vcalendar())})
        with caplog.at_level(logging.WARNING):
            requests, drafts = run(example_source(), http)
        assert requests == [LIST, f"{EXAMPLE}/events/a-talk"]
        assert [draft.title for draft in drafts] == ["A talk"]
        assert "on a host the source does not name" in caplog.text

    def test_only_foreign_links_is_a_foreign_host_error(self) -> None:
        listing = list_page(card("Elsewhere", "https://other.example.net/e"))
        with pytest.raises(ForeignHostError):
            run(example_source(), site(listing, {}))

    def test_only_foreign_links_without_native_id_keep_the_cards(self) -> None:
        listing = list_page(card("Elsewhere", "https://other.example.net/e"))
        requests, drafts = run(example_source(native_id=None), site(listing, {}))
        assert requests == [LIST]
        assert [draft.url for draft in drafts] == ["https://other.example.net/e"]

    def test_a_list_redirected_to_another_host(self) -> None:
        http = FakeHttpClient()
        http.add(LIST, list_page(card()), final_url="https://www.example.net/upcoming")
        with pytest.raises(ForeignHostError):
            run(example_source(), http)

    def test_a_link_listed_twice_is_fetched_once(self) -> None:
        listing = list_page(card(), card())
        http = site(listing, {"/events/a-talk": detail_page(calendar=vcalendar())})
        requests, drafts = run(example_source(), http)
        assert requests == [LIST, f"{EXAMPLE}/events/a-talk"]
        assert len(drafts) == 2  # the pipeline merges drafts with the same native id

    def test_detail_selectors_without_exclude(self) -> None:
        detail = {**COLLOQUIUM_PARAMS["detail"], "exclude": None}
        page = detail_page(calendar=vcalendar(uid="first"))
        assert one(list_page(card()), {"/events/a-talk": page}, detail=detail).native_id == "first"

    def test_a_card_without_link_is_not_fetched(self) -> None:
        requests, drafts = run(example_source(), site(list_page(card(link=None)), {}))
        assert requests == [LIST]
        assert drafts == []

    def test_without_detail_selectors_only_the_list_is_requested(self) -> None:
        requests, drafts = run(
            example_source(native_id=None, detail=None), site(list_page(card()), {})
        )
        assert requests == [LIST]
        assert len(drafts) == 1


class TestPages:
    def test_a_list_without_cards_is_not_understood(self) -> None:
        with pytest.raises(ParseError, match=r"bayarea-example.*matches nothing"):
            run(example_source(), site("<html><body>Maintenance</body></html>", {}))

    def test_a_list_that_says_it_is_empty(self) -> None:
        requests, drafts = run(example_source(), site(list_page(empty=True), {}))
        assert requests == [LIST]
        assert drafts == []

    def test_a_payload_without_list_page(self) -> None:
        source = example_source()
        document = make_document(url=LIST, text=list_page(card()), label="detail")
        payload = make_payload(document, source_id=source.id)
        with pytest.raises(ParseError, match="no list page"):
            SlacDrupalAdapter().parse(payload, source)


class TestParams:
    @pytest.mark.parametrize(
        ("params", "message"),
        [
            ({"lists": {}}, r"params\.lists: unknown key"),
            ({"list": {"card": "div", "speakers": "p"}}, r"params\.list\.speakers: unknown key"),
            ({"list": {}}, r"params\.list\.card"),
            ({"list": {"card": "div["}}, r"params\.list\.card"),
            ({"native_id": "uid"}, r"params\.native_id"),
            ({"detail": None}, "native_id vevent_uid needs detail.ical_link"),
            ({"detail": {"title": "h1"}}, "native_id vevent_uid needs detail.ical_link"),
            ({"max_detail_pages": 20}, r"params\.max_detail_pages"),
            ({"tba_title_patterns": ["(?P<title>.+)"]}, "unknown named group"),
            ({"skip_title_patterns": ["("]}, r"params\.skip_title_patterns"),
        ],
    )
    def test_bad_params_are_named(self, params: dict[str, Any], message: str) -> None:
        source = example_source(**params)
        with pytest.raises(AdapterConfigError, match=message):
            SlacDrupalAdapter().fetch(FakeHttpClient(), source, window=make_window())
        with pytest.raises(AdapterConfigError, match=message):
            SlacDrupalAdapter().parse(make_payload(source_id=source.id), source)

    def test_extra_hosts_is_accepted(self) -> None:
        source = example_source(extra_hosts=["other.example.net"])
        listing = list_page(card("Elsewhere", "https://other.example.net/e"))
        http = FakeHttpClient({LIST: listing})
        http.add("https://other.example.net/e", detail_page("Elsewhere", vcalendar()))
        requests, drafts = run(source, http)
        assert requests == [LIST, "https://other.example.net/e"]
        assert [draft.native_id for draft in drafts] == ["uid-1"]


def test_the_module_has_no_site_specific_names() -> None:
    text = Path(slac_drupal.__file__).read_text(encoding="utf-8")
    for name in ("colloquium.slac", "theory.slac", "bayarea-", "stanford.edu"):
        assert name not in text
