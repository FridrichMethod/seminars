"""The generic ``rss`` adapter: recorded template B sites and hand-written feeds."""

from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from factories import make_source, make_window
from fakes import FakeHttpClient
from seminars.config import load_sources
from seminars.http import offline_client
from seminars.models import TBA_TITLE, EventDraft, SourceConfig
from seminars.sources.base import ForeignHostError, ParseError, get_adapter
from seminars.sources.generic import rss as rss_module
from seminars.sources.generic.html_list import AdapterConfigError
from seminars.sources.generic.rss import RssAdapter, RssParams, feed_moment, load_params

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
WINDOW = make_window(NOW)
PST = timezone(timedelta(hours=-8))
SOURCES = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
RSS_SOURCES = sorted(sid for sid, source in SOURCES.items() if source.adapter == "rss")


def run(source: SourceConfig, http: Any = None) -> list[EventDraft]:
    adapter = get_adapter(source.adapter)
    client = http if http is not None else offline_client(source.id)
    return adapter.parse(adapter.fetch(client, source, window=WINDOW), source)


def by_title(drafts: list[EventDraft], title: str) -> EventDraft:
    matches = [draft for draft in drafts if draft.title == title]
    assert len(matches) == 1, [draft.title for draft in drafts]
    return matches[0]


def at(drafts: list[EventDraft], start: datetime) -> list[EventDraft]:
    return [draft for draft in drafts if draft.start == start]


def with_params(source: SourceConfig, **changes: Any) -> SourceConfig:
    return source.evolve(params={**source.params, **changes})


def names(draft: EventDraft) -> list[tuple[str, str | None]]:
    return [(speaker.name, speaker.affiliation) for speaker in draft.speakers]


# --- recorded sources ----------------------------------------------------------------------


class TestRecordedSources:
    def test_the_adapter_is_registered_under_its_module_name(self) -> None:
        assert isinstance(get_adapter("rss"), RssAdapter)

    @pytest.mark.parametrize("source_id", RSS_SOURCES)
    def test_every_source_of_the_adapter_validates_its_params(self, source_id: str) -> None:
        assert load_params(RssParams, SOURCES[source_id]).feed.start == "pubDate"

    @pytest.mark.parametrize("source_id", [sid for sid in RSS_SOURCES if SOURCES[sid].enabled])
    def test_every_enabled_source_yields_drafts_from_its_fixtures(self, source_id: str) -> None:
        drafts = run(SOURCES[source_id])
        assert drafts
        assert all(draft.source_id == source_id and draft.extraction == "feed" for draft in drafts)

    def test_bioengineering_cards_link_to_the_campus_calendar(self) -> None:
        http = offline_client("sites-bioengineering")
        drafts = run(SOURCES["sites-bioengineering"], http)
        # Every card links off the site, so no detail page is fetched.
        assert http.requests == [
            "https://bioengineering.stanford.edu/events/all/rss",
            "https://bioengineering.stanford.edu/events",
        ]
        assert len(drafts) == 4
        defense = by_title(drafts, "PhD Dissertation Defense: Danielle Klinger")
        assert defense.start == datetime(2026, 11, 13, 13, 0, tzinfo=PST)
        assert defense.end is None
        assert defense.location == "Clark Center"
        assert defense.native_id == "9c161082-f718-4872-96bf-d89a57d08ac7"
        assert (
            defense.url
            == "https://events.stanford.edu/event/phd-dissertation-defense-danielle-klinger"
        )
        assert defense.event_type_hint == "defense"
        assert defense.categories == ["PhD Defense"]

    def test_cheme_colloquium_fields_and_the_detail_page(self) -> None:
        http = offline_client("sites-cheme")
        drafts = run(SOURCES["sites-cheme"], http)
        detail = "https://cheme.stanford.edu/events/chemical-engineering-colloquium/amy-e-herr"
        assert http.requests[2:] == [detail]
        talk = by_title(
            drafts,
            "What Genes (and Transcripts) Can't Tell You: Single-Cell Proteomic Tools for "
            "Embryos to Ecosystems",
        )
        assert names(talk) == [("Amy E. Herr", "University of California, Berkeley")]
        assert talk.start == datetime(2026, 11, 16, 16, 30, tzinfo=PST)
        assert talk.end == datetime(2026, 11, 16, 17, 30, tzinfo=PST)
        assert talk.location == "(In person) Turing Auditorium"
        assert talk.event_type_hint == "colloquium"
        assert talk.url == detail
        assert "Abstract" in (talk.abstract_html or "")
        # The "more events" block below the event belongs to other events.
        assert "L.A.S.E.R" not in (talk.abstract_html or "")
        assert talk.categories[0] == "Chemical Engineering Colloquium"
        other = by_title(
            drafts, "Three L.A.S.E.R. talks: Media Art, AI, The Self and Consciousness"
        )
        assert other.speakers == []
        assert other.event_type_hint == "seminar"

    def test_biochemistry_title_patterns(self) -> None:
        http = offline_client("sites-biochemistry")
        drafts = run(SOURCES["sites-biochemistry"], http)
        assert len(http.requests) == 2  # detail_page.fetch is never
        talk = by_title(drafts, "Molecular mechanisms of signal transduction")
        assert names(talk) == [("Steve Blacklow", "Harvard")]
        assert talk.series == "Frontiers in Biology"
        assert talk.start == datetime(2026, 11, 18, 16, 0, tzinfo=PST)
        assert talk.abstract == "11/18/26, @Clark Auditorium, 4PM"

    def test_chemh_events_point_elsewhere(self) -> None:
        drafts = run(SOURCES["sites-chemh"])
        assert len(drafts) == 4
        talk = by_title(drafts, "Breaking Bacterial Resistance via Chemical Dysregulation")
        assert (talk.url or "").startswith("https://chemsysbio.stanford.edu/events/")
        assert talk.start == datetime(2026, 12, 3, 12, 30, tzinfo=PST)
        assert talk.location is None

    def test_cs_without_detail_pages(self) -> None:
        source = with_params(SOURCES["sites-cs"], detail_page={"fetch": "never"})
        http = offline_client("sites-cs")
        drafts = run(source, http)
        assert len(http.requests) == 2
        assert len(drafts) == 9
        talk = by_title(
            drafts, "HAI Seminar with Ang\u00e8le Christin in Conversation with Hannah Zeavin"
        )
        assert talk.start == datetime(2026, 12, 9, 12, 0, tzinfo=PST)
        assert talk.location == "Gates Computer Science Building"
        sessions = [d for d in drafts if d.title == "CS Program Sheet Workshop & Advising"]
        assert [d.event_type_hint for d in sessions].count(None) == 1  # the card without a type
        assert {d.event_type_hint for d in sessions} == {"other", None}

    def test_cs_detail_pages_that_fail_do_not_lose_the_events(self) -> None:
        http = offline_client("sites-cs")
        drafts = run(SOURCES["sites-cs"], http)
        # Native cards inside the window are asked for; none is recorded.
        assert len(http.requests) == 6
        assert all(url.startswith("https://www.cs.stanford.edu/events") for url in http.requests)
        assert len(drafts) == 9

    def test_icme_schedule_expands_into_sessions(self) -> None:
        source = with_params(SOURCES["sites-icme"], skip_session_patterns=[r"^No class\b"])
        drafts = run(source.evolve(enabled=True))
        sessions = [d for d in drafts if d.series == "First Year Seminar Series 2026 (CME 300)"]
        assert len(sessions) == 9
        unexpanded = run(with_params(SOURCES["sites-icme"], expand_schedule=False))
        series_id = by_title(unexpanded, "First Year Seminar Series 2026 (CME 300)").native_id
        assert series_id
        [session] = at(sessions, datetime(2026, 11, 17, 13, 30))
        assert session.end == datetime(2026, 11, 17, 14, 45)
        assert session.title == "Seminar Session 8"
        assert "Alison Marsden" in (session.speaker_text or "")
        assert session.native_id == f"{series_id}/2026-11-17"
        symposium = by_title(drafts, "ICME Research Symposium 2027")
        assert symposium.start == datetime(2027, 5, 18, 9, 0, tzinfo=timezone(timedelta(hours=-7)))

    def test_icme_without_expansion_is_one_all_day_event(self) -> None:
        drafts = run(with_params(SOURCES["sites-icme"], expand_schedule=False))
        series = by_title(drafts, "First Year Seminar Series 2026 (CME 300)")
        assert (series.start, series.end) == (date(2026, 9, 22), date(2026, 12, 1))
        assert series.location == "Stanford Campus, United States"

    def test_kipac_pages_through_the_list(self) -> None:
        http = offline_client("bayarea-kipac")
        drafts = run(SOURCES["bayarea-kipac"], http)
        assert http.requests == [
            "https://kipac.stanford.edu/events/all/rss",
            "https://kipac.stanford.edu/events/upcoming-events",
            "https://kipac.stanford.edu/events/upcoming-events?page=1",
            "https://kipac.stanford.edu/events/upcoming-events?page=2",
        ]
        talk = by_title(drafts, "What JWST Spectra are Teaching us About Atmospheric Physics")
        assert talk.series == "Astrophysics Colloquium"
        assert names(talk) == [("Natasha Batalha", "NASA Ames")]
        assert talk.start == datetime(2026, 11, 12, 11, 0, tzinfo=PST)
        assert talk.location == "Campus, PAB 102/103"
        assert talk.native_id is None  # identity is the detail URL
        assert talk.abstract is None  # the dek repeats the location
        assert talk.event_type_hint == "colloquium"
        categories = {category for draft in drafts for category in draft.categories}
        assert not categories & {"Other", "AP/Physics Colloquium"}

    def test_kipac_speakers_and_titles(self) -> None:
        drafts = run(SOURCES["bayarea-kipac"])
        tea = [d for d in drafts if len(d.speakers) == 2 and d.series == "KIPAC Tea Talk"]
        assert names(tea[0]) == [
            ("Chenze Dong", "Kavli IPMU"),
            ("Aniruddh Herle", "Leiden Observatory"),
        ]
        affiliations = {s.affiliation for d in drafts for s in d.speakers}
        assert "GSFC / UMD" in affiliations
        [group] = [d for d in drafts if d.speakers and d.speakers[0].name == "Meghan Marangola"]
        # "Meghan Marangola (KIPAC) / Xinnan Du, Alex Mitchell, Leo Sajkov, and Toby
        # Satterthwaite (KIPAC)": one match per bracket, the group of names is split.
        assert [s.name for s in group.speakers] == [
            "Meghan Marangola",
            "Xinnan Du",
            "Alex Mitchell",
            "Leo Sajkov",
            "Toby Satterthwaite",
        ]
        assert {s.affiliation for s in group.speakers} == {"KIPAC"}
        [untitled] = at(drafts, datetime(2026, 11, 30, 11, 0, tzinfo=PST))
        assert untitled.title == TBA_TITLE
        assert untitled.series == "KIPAC Seminar"
        same_time = at(drafts, datetime(2027, 1, 21, 11, 0, tzinfo=PST))
        assert len(same_time) == 2
        assert len({draft.url for draft in same_time}) == 2
        workshop = [d for d in drafts if d.event_type_hint == "workshop"]
        assert [(d.start, d.end) for d in workshop] == [(date(2026, 12, 2), date(2026, 12, 4))]

    def test_pulse(self) -> None:
        drafts = run(SOURCES["bayarea-pulse"])
        talk = by_title(drafts, "X-ray Spectroscopy of Many-Body States and Interactions")
        assert names(talk) == [("Ke-Jun Xu", "UC Berkeley")]
        assert talk.start == datetime(2026, 11, 11, 15, 0, tzinfo=PST)
        assert talk.location == "United States"
        assert talk.series == "Photon Science Seminars"

    def test_glam_placeholder_titles_and_hosts(self) -> None:
        drafts = run(SOURCES["bayarea-glam"])
        assert len(drafts) == 8
        assert sum(draft.title == TBA_TITLE for draft in drafts) == 6
        talk = by_title(
            drafts,
            "Non-equilibrium dynamics of polaron formation and polaron-polaron interaction in NiO",
        )
        assert names(talk) == [("Uwe Bovensiepen", "University of Duisburg-Essen, Germany")]
        assert talk.location == (
            "Stanford University, 476 Lomita Mall, Room 115, Stanford, CA 94305, United States"
        )
        assert talk.abstract == "Host: Zhi-Xun Shen"
        special = [d for d in drafts if d.series == "GLAM Special Seminar"]
        assert names(special[0]) == [("Minho \u201cLuke\u201d Kim", "MIT")]

    def test_qfarm_cards_are_the_record(self) -> None:
        http = offline_client("bayarea-qfarm")
        drafts = run(SOURCES["bayarea-qfarm"], http)
        assert len(http.requests) == 2  # page 0 already runs past the window
        assert len(drafts) == 20
        # The stray feed item at 11:00 has no card and is dropped.
        assert at(drafts, datetime(2026, 11, 11, 11, 0, tzinfo=PST)) == []
        [talk] = at(drafts, datetime(2026, 11, 11, 11, 30, tzinfo=PST))
        assert (talk.title or "").startswith("Modulated Circuit-QED Systems")
        assert names(talk) == [("Alicia Koll\u00e1r", "University of Maryland")]
        [pair] = at(drafts, datetime(2026, 11, 18, 11, 0, tzinfo=PST))
        assert pair.title == TBA_TITLE
        assert [s.name for s in pair.speakers] == ["Tian Xie", "Yugo Onishi"]
        workshop = by_title(
            drafts, "Quantum Sensing for the Detection of Coherent Neutrino Scattering Workshop"
        )
        assert (workshop.start, workshop.end) == (date(2026, 11, 19), date(2026, 11, 20))
        assert workshop.speakers == []
        assert workshop.event_type_hint == "workshop"


# --- hand-written feeds --------------------------------------------------------------------

FEED_URL = "https://dept.example.edu/events/feed"
LIST_URL = "https://dept.example.edu/events"


def rss(*items: str, extra_ns: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<rss version="2.0" xmlns:ev="http://purl.org/rss/1.0/modules/event/"{extra_ns}>'
        "<channel><title>Events</title><link>https://dept.example.edu/</link>"
        + "".join(items)
        + "</channel></rss>"
    )


def item(title: str = "A talk", when: str = "Mon, 16 Nov 2026 16:30:00 -0800", **more: str) -> str:
    fields = "".join(f"<{key}>{value}</{key}>" for key, value in more.items())
    link = f"<link>/talks/{len(title)}</link>"
    return f"<item><title>{title}</title>{link}{fields}<pubDate>{when}</pubDate></item>"


def feed_source(**params: Any) -> SourceConfig:
    base: dict[str, Any] = {"feed": {"start": "pubDate"}}
    return make_source(
        id="custom-example", group="stanford-custom", url=FEED_URL, params={**base, **params}
    )


def serve(**pages: str) -> FakeHttpClient:
    http = FakeHttpClient()
    for url, text in pages.items():
        http.add(url, text, content_type="application/rss+xml")
    return http


class TestFeedOnly:
    def test_entries_become_drafts(self) -> None:
        text = rss(item(guid="g-1", description="&lt;p&gt;An abstract&lt;/p&gt;"))
        [draft] = run(feed_source(), serve(**{FEED_URL: text}))
        assert draft.title == "A talk"
        assert draft.start == datetime(2026, 11, 16, 16, 30, tzinfo=PST)
        assert draft.native_id == "g-1"
        assert draft.url == "https://dept.example.edu/talks/6"
        assert draft.abstract_html == "<p>An abstract</p>"

    def test_extension_fields(self) -> None:
        source = feed_source(
            feed={
                "start": "ev_startdate",
                "end": "ev_enddate",
                "location": "ev_location",
                "speaker": "ev_speaker",
                "native_id": "link",
            }
        )
        text = rss(
            item(
                **{
                    "ev:startdate": "2026-11-17",
                    "ev:enddate": "2026-11-19",
                    "ev:location": "Room 1",
                    "ev:speaker": "Jane Doe (MIT)",
                }
            )
        )
        [draft] = run(source, serve(**{FEED_URL: text}))
        assert (draft.start, draft.end) == (date(2026, 11, 17), date(2026, 11, 19))
        assert draft.is_all_day
        assert draft.location == "Room 1"
        assert draft.speaker_text == "Jane Doe (MIT)"
        assert draft.native_id == draft.url

    def test_atom_with_html_titles(self) -> None:
        atom = (
            '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>'
            '<entry><title type="html">Q&amp;amp;A on L&amp;eacute;vy flights</title><id>urn:1</id>'
            '<link href="https://dept.example.edu/1"/><published>2026-11-17T16:00:00Z</published>'
            "<summary>S</summary></entry></feed>"
        )
        [draft] = run(feed_source(feed={"start": "published"}), serve(**{FEED_URL: atom}))
        assert draft.title == "Q&A on L\u00e9vy flights"
        assert draft.start == datetime(2026, 11, 17, 16, tzinfo=UTC)
        assert draft.native_id == "urn:1"

    def test_markup_titles_and_names_outside_ascii(self) -> None:
        title = '<h2><a href="/events/x">Ang\u00e8le &amp; Co</a></h2>'
        [draft] = run(
            feed_source(feed={"start": "pubDate", "title_is_markup": True}),
            serve(**{FEED_URL: rss(item(title=title))}),
        )
        assert draft.title == "Ang\u00e8le & Co"

    def test_items_without_a_start_are_skipped(self) -> None:
        text = rss(
            item(title="Good"), item(title="No date", when=""), item(title="Odd", when="soon")
        )
        drafts = run(feed_source(), serve(**{FEED_URL: text}))
        assert [(d.title, d.start) for d in drafts] == [
            ("Good", datetime(2026, 11, 16, 16, 30, tzinfo=PST)),
            ("Odd", "soon"),  # left to normalization, which reports it as invalid
        ]

    def test_an_empty_channel_is_no_event(self) -> None:
        assert run(feed_source(), serve(**{FEED_URL: rss()})) == []

    @pytest.mark.parametrize("body", ["<html><body>Moved</body></html>", "/etc/passwd"])
    def test_a_page_that_is_no_feed_is_a_parse_error(self, body: str) -> None:
        with pytest.raises(ParseError, match="not an RSS or Atom feed"):
            run(feed_source(), serve(**{FEED_URL: body}))

    def test_every_url_is_a_feed_without_a_list_page(self) -> None:
        source = feed_source().evolve(urls=["https://dept.example.edu/more/feed"])
        http = serve(
            **{
                FEED_URL: rss(item(title="One")),
                "https://dept.example.edu/more/feed": rss(item(title="Two")),
            }
        )
        assert [d.title for d in run(source, http)] == ["One", "Two"]

    def test_payload_without_a_feed(self) -> None:
        source = feed_source()
        payload = RssAdapter().fetch(serve(**{FEED_URL: rss()}), source, window=WINDOW)
        relabelled = payload.evolve(documents=[d.evolve(label="x") for d in payload.documents])
        with pytest.raises(ParseError, match="no feed"):
            RssAdapter().parse(relabelled, source)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Mon, 16 Nov 2026 16:30:00 -0800", datetime(2026, 11, 16, 16, 30, tzinfo=PST)),
        ("2026-11-16T16:30:00-08:00", datetime(2026, 11, 16, 16, 30, tzinfo=PST)),
        # -0000 is UTC with no local zone known; no zone at all is the source's local time.
        ("Mon, 16 Nov 2026 16:30:00 -0000", datetime(2026, 11, 16, 16, 30, tzinfo=UTC)),
        ("Mon, 16 Nov 2026 16:30:00 GMT", datetime(2026, 11, 16, 16, 30, tzinfo=UTC)),
        ("Mon, 16 Nov 2026 16:30:00", datetime(2026, 11, 16, 16, 30)),
        ("2026-11-16", date(2026, 11, 16)),
        ("20261116", date(2026, 11, 16)),
        ("next week", "next week"),
        ("", None),
        (None, None),
    ],
)
def test_feed_moments(text: str | None, expected: object) -> None:
    moment = feed_moment(text)
    assert moment == expected
    assert type(moment) is type(expected)
    if isinstance(moment, datetime) and isinstance(expected, datetime):
        assert moment.utcoffset() == expected.utcoffset()


# --- hand-written list pages ---------------------------------------------------------------


def card(title: str, start: str, end: str | None = None, **parts: str) -> str:
    href = parts.pop("href", f"/events/{title.lower().replace(' ', '-')}")
    css = parts.pop("css", "su-link")
    kind = parts.pop("kind", "Seminar")
    times = f'<time datetime="{start}">x</time>' + (
        f'<time datetime="{end}">y</time>' if end else ""
    )
    extra = "".join(f'<div class="{key}">{value}</div>' for key, value in parts.items())
    return (
        f'<article class="ev"><span class="type">{kind}</span>'
        f'<h3><a class="{css}" href="{href}">{title}</a></h3>{times}{extra}</article>'
    )


def listing(*cards: str, pager: str | None = None) -> str:
    more = f'<nav><a class="more" href="{pager}">More</a></nav>' if pager else ""
    return "<html><body><div class='list'>" + "".join(cards) + f"</div>{more}</body></html>"


def list_source(**params: Any) -> SourceConfig:
    list_page = {
        "item": "article.ev",
        "title": "h3 a",
        "link": "h3 a",
        "external_link_class": "ext",
        "type": ".type",
        "times": "time[datetime]",
        "location": ".place",
        "subheadline": ".sub",
        "dek": ".dek",
        **params.pop("list_page", {}),
    }
    base: dict[str, Any] = {
        "feed": {"start": "pubDate", "native_id": "guid"},
        "list_page": list_page,
    }
    return make_source(
        id="custom-example",
        group="stanford-custom",
        url=FEED_URL,
        urls=[LIST_URL],
        params={**base, **params},
    )


class TestListPages:
    def test_the_feed_is_the_index_and_cards_complete_it(self) -> None:
        feed = rss(item(title="Talk One", guid="g1"), item(title="Only in feed", guid="g2"))
        page = listing(
            card(
                "Talk One", "2026-11-16T16:30:00-08:00", "2026-11-16T17:30:00-08:00", place="Room 9"
            ),
            card("Not in feed", "2026-11-20T10:00:00-08:00"),
        )
        drafts = run(list_source(), serve(**{FEED_URL: feed, LIST_URL: page}))
        assert [(d.title, d.native_id) for d in drafts] == [
            ("Talk One", "g1"),
            ("Only in feed", "g2"),
        ]
        assert drafts[0].end == datetime(2026, 11, 16, 17, 30, tzinfo=PST)
        assert drafts[0].location == "Room 9"
        assert drafts[1].end is None

    def test_primary_lists_page_through_the_pager(self) -> None:
        source = list_source(list_page={"primary": True, "pager": "a.more"}, max_list_pages=3)
        page0 = listing(card("A", "2026-11-16T16:30:00-08:00"), pager="?page=1")
        page1 = listing(card("B", "2026-12-16T16:30:00-08:00"), pager="?page=2")
        page2 = listing(card("C", "2027-06-16T16:30:00-07:00"), pager="?page=3")
        http = serve(
            **{
                FEED_URL: rss(item(title="A")),
                LIST_URL: page0,
                f"{LIST_URL}?page=1": page1,
                f"{LIST_URL}?page=2": page2,
            }
        )
        drafts = run(source, http)
        assert http.requests == [FEED_URL, LIST_URL, f"{LIST_URL}?page=1", f"{LIST_URL}?page=2"]
        assert [d.title for d in drafts] == ["A", "B", "C"]
        assert all(d.native_id is None for d in drafts)

    def test_the_pager_stops_at_the_page_limit_and_at_repeated_links(self) -> None:
        source = list_source(list_page={"primary": True, "pager": "a.more"}, max_list_pages=5)
        page = listing(card("A", "2026-11-16T16:30:00-08:00"), pager=LIST_URL)
        http = serve(**{FEED_URL: rss(), LIST_URL: page})
        assert [d.title for d in run(source, http)] == ["A"]
        assert http.requests == [FEED_URL, LIST_URL]

    def test_a_pager_on_another_host_is_refused(self) -> None:
        source = list_source(list_page={"primary": True, "pager": "a.more"}, max_list_pages=2)
        page = listing(card("A", "2026-11-16T16:30:00-08:00"), pager="https://other.org/p2")
        with pytest.raises(ForeignHostError):
            run(source, serve(**{FEED_URL: rss(), LIST_URL: page}))

    def test_a_list_without_cards_while_the_feed_has_items_is_a_parse_error(self) -> None:
        http = serve(**{FEED_URL: rss(item()), LIST_URL: listing()})
        with pytest.raises(ParseError, match="matches nothing, although the feed lists 1 items"):
            run(list_source(), http)

    def test_both_empty_is_no_event(self) -> None:
        assert run(list_source(), serve(**{FEED_URL: rss(), LIST_URL: listing()})) == []

    def test_detail_pages_of_native_cards(self) -> None:
        detail_page = {
            "fetch": "native_only",
            "root": "section.event",
            "exclude": ".more",
            "end_attribute": "data-end",
            "body": ".body",
            "location": ".where",
            "audience": ".aud",
        }
        source = list_source(detail_page=detail_page)
        feed = rss(item(title="Here"), item(title="There"), item(title="Gone"))
        page = listing(
            card("Here", "2026-11-16T16:30:00-08:00", href="/events/here"),
            card(
                "There", "2026-11-16T16:30:00-08:00", href="https://other.org/t", css="su-link ext"
            ),
            card("Gone", "2026-11-16T16:30:00-08:00", href="/events/gone"),
        )
        body = (
            '<section class="event" data-end="1794879000"><div class="body"><p>Abstract</p></div>'
            '<p class="where">Room 1<br>Main Quad</p><span class="aud">Students</span>'
            '<div class="more"><div class="body">Other event</div><span class="aud">Public</span>'
            "</div></section>"
        )
        http = serve(
            **{FEED_URL: feed, LIST_URL: page, "https://dept.example.edu/events/here": body}
        )
        drafts = run(source, http)
        assert http.requests[2:] == [
            "https://dept.example.edu/events/here",
            "https://dept.example.edu/events/gone",
        ]
        here, there, gone = drafts
        assert here.end == datetime(2026, 11, 17, 1, 30, tzinfo=UTC)
        assert here.abstract_html == "<p>Abstract</p>"
        assert here.location == "Room 1, Main Quad"
        assert here.categories == ["Seminar", "Students"]
        assert there.url == "https://other.org/t"
        assert gone.abstract_html is None  # its detail page failed; the event stays

    def test_excluded_blocks_are_skipped_in_one_pass(self) -> None:
        # Matches deep inside an excluded block cost one set lookup each, not a walk
        # up the tree: 4000 nested matches would otherwise take seconds. The root
        # itself is never excluded.
        detail_page = {"fetch": "native_only", "root": ".page", "exclude": ".more"}
        source = list_source(detail_page={**detail_page, "audience": ".aud"})
        nested = '<div class="aud">x' * 4000 + "</div>" * 4000
        body = f'<p class="aud">Students</p><div class="more">{nested}</div>'
        page = listing(card("A talk", "2026-11-16T16:30:00-08:00", href="/events/a"))
        detail = f'<html><body><div class="page more">{body}</div></body></html>'
        http = serve(
            **{FEED_URL: rss(item()), LIST_URL: page, "https://dept.example.edu/events/a": detail}
        )
        [draft] = run(source, http)
        assert draft.categories == ["Seminar", "Students"]

    @pytest.mark.parametrize(
        ("start", "text", "end"),
        [
            ("2026-11-16T16:30:00-08:00", "4:30 - 5:30pm", datetime(2026, 11, 16, 17, 30)),
            # A start in UTC falls on the next day there; the end is on the local day.
            ("2026-11-17T00:30:00Z", "4:30 - 5:30pm PT", datetime(2026, 11, 16, 17, 30)),
            ("2026-11-16T16:30:00-08:00", "4:30pm", None),
            ("2026-11-16T16:30:00-08:00", "4:30 - 3:00pm", None),
        ],
    )
    def test_the_end_from_the_time_text_of_a_detail_page(
        self, start: str, text: str, end: datetime | None
    ) -> None:
        source = list_source(detail_page={"fetch": "native_only", "time_text": ".when"})
        page = listing(card("A talk", start, href="/events/a"))
        detail = f'<html><body><p class="when">{text}</p></body></html>'
        http = serve(
            **{FEED_URL: rss(item()), LIST_URL: page, "https://dept.example.edu/events/a": detail}
        )
        [draft] = run(source, http)
        expected = end.replace(tzinfo=ZoneInfo("America/Los_Angeles")) if end else None
        assert draft.end == expected
        if isinstance(draft.end, datetime):
            assert draft.end.utcoffset() == timedelta(hours=-8)

    def test_a_card_at_the_end_of_the_calendar_does_not_fail_the_source(self) -> None:
        source = list_source(list_page={"primary": True}, detail_page={"fetch": "native_only"})
        page = listing(card("Far", "9999-12-31T23:00:00-08:00"), card("A", "2026-11-17"))
        http = serve(**{FEED_URL: rss(), LIST_URL: page})
        assert [d.title for d in run(source, http)] == ["Far", "A"]
        assert http.requests[2:] == ["https://dept.example.edu/events/a"]

    def test_a_draft_the_model_rejects_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        original = rss_module.card_fields

        def fields(params: Any, entry: Any, detail: Any, location: str | None) -> Any:
            found = original(params, entry, detail, location)
            bad = [{"name": ""}] if found.title == "Bad" else found.speakers
            return dataclasses.replace(found, speakers=bad)

        monkeypatch.setattr(rss_module, "card_fields", fields)
        page = listing(card("Bad", "2026-11-17"), card("Good", "2026-11-17"))
        http = serve(**{FEED_URL: rss(), LIST_URL: page})
        with caplog.at_level(logging.WARNING, logger=rss_module.__name__):
            drafts = run(list_source(list_page={"primary": True}), http)
        assert [d.title for d in drafts] == ["Good"]
        assert "a malformed feed item was skipped (ValidationError)" in caplog.text

    def test_native_cards_on_other_hosts_keep_their_draft(self) -> None:
        source = list_source(detail_page={"fetch": "native_only"})
        page = listing(card("A talk", "2026-11-16T16:30:00-08:00", href="https://other.org/a"))
        http = serve(**{FEED_URL: rss(item()), LIST_URL: page})
        [draft] = run(source, http)
        assert http.requests == [FEED_URL, LIST_URL]
        assert draft.url == "https://other.org/a"

    def test_a_list_page_redirected_to_another_host_raises(self) -> None:
        source = list_source(detail_page={"fetch": "native_only"})
        http = serve(**{FEED_URL: rss(item())})
        page = listing(card("A talk", "2026-11-16T16:30:00-08:00"))
        http.add(LIST_URL, page, final_url="https://www.dept.example.edu/events")
        with pytest.raises(ForeignHostError, match=r"www\.dept\.example\.edu"):
            run(source, http)

    def test_detail_pages_outside_the_window_are_not_fetched(self) -> None:
        source = list_source(detail_page={"fetch": "native_only", "max_pages": 1})
        feed = rss(
            item(title="Old", when="Mon, 05 Jan 2026 10:00:00 -0800"),
            item(title="A"),
            item(title="B"),
        )
        page = listing(
            card("Old", "2026-01-05T10:00:00-08:00"),
            card("A", "2026-11-16T16:30:00-08:00"),
            card("B", "2026-11-16T16:30:00-08:00"),
        )
        http = serve(**{FEED_URL: feed, LIST_URL: page})
        RssAdapter().fetch(http, source, window=WINDOW)
        assert http.requests == [FEED_URL, LIST_URL, "https://dept.example.edu/events/a"]

    def test_card_fields_patterns_and_exclusions(self) -> None:
        source = list_source(
            list_page={"primary": True},
            card_fields={"speakers": "subheadline", "host": "dek"},
            type_card_fields={"Lab Talk": {"speakers": "title", "title": "subheadline"}},
            speaker_patterns=[r"(?P<speaker>[^()]+?)\s*\((?P<affiliation>[^()]*)\)"],
            title_patterns=[r"^(?P<series>Physics Seminar):\s*(?P<title>.+)$"],
            tba_title_patterns=["^Coming soon$"],
            exclude_types=["Social"],
            series_from="type",
            event_type_names={"Seminar": "seminar", " Lab  Talk ": "lecture"},
        )
        page = listing(
            card(
                "Physics Seminar: Spins",
                "2026-11-16T16:30:00-08:00",
                sub="A One (MIT), B Two and C Three (KIPAC)",
                dek="Host: Someone",
            ),
            card("Coming soon", "2026-11-17T16:30:00-08:00", sub="D Four (Caltech)"),
            card("E Five (UCLA)", "2026-11-18T16:30:00-08:00", kind="Lab Talk"),
            card("Party", "2026-11-19T16:30:00-08:00", kind="Social"),
        )
        drafts = run(source, serve(**{FEED_URL: rss(), LIST_URL: page}))
        spins, soon, lab = drafts
        assert (spins.title, spins.series) == ("Spins", "Physics Seminar")
        assert names(spins) == [("A One", "MIT"), ("B Two", "KIPAC"), ("C Three", "KIPAC")]
        assert spins.abstract == "Host: Someone"
        assert (soon.title, soon.series) == (TBA_TITLE, "Seminar")
        assert (lab.title, names(lab), lab.event_type_hint) == (
            TBA_TITLE,
            [("E Five", "UCLA")],
            "lecture",
        )

    def test_colloquium_fields_apply_to_colloquium_cards_only(self) -> None:
        source = list_source(
            list_page={"primary": True},
            event_type_names={"Colloquium": "colloquium"},
            colloquium_fields={
                "speaker": "title",
                "affiliation": "subheadline",
                "talk_title": "dek",
            },
        )
        page = listing(
            card(
                "Jane Doe", "2026-11-16T16:30:00-08:00", kind="Colloquium", sub="MIT", dek="Spins"
            ),
            card("A seminar", "2026-11-17T16:30:00-08:00", sub="Note"),
        )
        colloquium, seminar = run(source, serve(**{FEED_URL: rss(), LIST_URL: page}))
        assert (colloquium.title, names(colloquium)) == ("Spins", [("Jane Doe", "MIT")])
        assert (seminar.title, seminar.speakers, seminar.abstract) == ("A seminar", [], "Note")

    def test_all_day_cards(self) -> None:
        source = list_source(list_page={"primary": True})
        page = listing(card("Workshop", "2026-11-19", "2026-11-20"))
        [draft] = run(source, serve(**{FEED_URL: rss(), LIST_URL: page}))
        assert (draft.start, draft.end, draft.is_all_day) == (
            date(2026, 11, 19),
            date(2026, 11, 20),
            True,
        )

    def test_an_all_day_card_joins_a_feed_item_at_midnight(self) -> None:
        feed = rss(item(title="Workshop", when="Thu, 19 Nov 2026 00:00:00 -0800", guid="w"))
        page = listing(card("Workshop", "2026-11-19"))
        [draft] = run(list_source(), serve(**{FEED_URL: feed, LIST_URL: page}))
        assert (draft.native_id, draft.start) == ("w", date(2026, 11, 19))


SERIES_DETAIL = "https://dept.example.edu/events/seminar-series"
SESSIONS = (
    ("Tuesday, November 10, 2026", "1:30pm - 2:45pm", "Session 1"),
    ("Tuesday, November 17, 2026", "1:30pm - 2:45pm", "Session 2"),
    ("Tuesday, November 17, 2026", "3:00pm - 4:00pm", "Session 3"),
    ("Tuesday, November 24, 2026", "1:30pm - 2:45pm", "No class - Thanksgiving"),
    ("Tuesday, December 1, 2026", "TBA", "Session 4"),
    ("Tuesday, December 1, 2026", "TBA", "Session 5"),
)


def session_ids(sessions: tuple[tuple[str, str, str], ...]) -> dict[str | None, str | None]:
    """Title -> native id of the sessions of a hand-written series with a schedule."""
    schedule = {"date": "h3.day", "item": "div.s", "time": ".t", "headline": ".h"}
    source = list_source(
        detail_page={"fetch": "native_only", "schedule": schedule},
        expand_schedule=True,
        skip_session_patterns=[r"^No class\b"],
    )
    feed = rss(item(title="Seminar Series", when="Tue, 22 Sep 2026 00:00:00 -0700", guid="g1"))
    page = listing(card("Seminar Series", "2026-09-22", "2026-12-01"))
    blocks = "".join(
        f'<h3 class="day">{day}</h3><div class="s"><span class="t">{clock}</span>'
        f'<span class="h">{headline}</span></div>'
        for day, clock, headline in sessions
    )
    detail = f'<html><body><section class="event">{blocks}</section></body></html>'
    http = serve(**{FEED_URL: feed, LIST_URL: page, SERIES_DETAIL: detail})
    return {draft.title: draft.native_id for draft in run(source, http)}


class TestSchedules:
    def test_a_session_id_is_the_series_id_and_the_session_day(self) -> None:
        assert session_ids(SESSIONS) == {
            "Session 1": "g1/2026-11-10",
            "Session 2": "g1/2026-11-17T1330",
            "Session 3": "g1/2026-11-17T1500",
            "Session 4": "g1/2026-12-01",
            "Session 5": "g1/2026-12-01-2",
        }

    def test_removing_or_adding_a_session_keeps_the_ids_of_the_others(self) -> None:
        full = session_ids(SESSIONS)
        assert session_ids(SESSIONS[1:]) == {k: v for k, v in full.items() if k != "Session 1"}
        extra = ("Tuesday, November 3, 2026", "1:30pm - 2:45pm", "Session 0")
        assert session_ids((extra, *SESSIONS)) == {"Session 0": "g1/2026-11-03", **full}


class TestParams:
    def test_an_unknown_key_is_named(self) -> None:
        with pytest.raises(AdapterConfigError, match=r"params\.feed\.colour: unknown key"):
            run(feed_source(feed={"start": "pubDate", "colour": "red"}), serve(**{FEED_URL: rss()}))

    def test_the_start_element_is_required(self) -> None:
        with pytest.raises(AdapterConfigError, match=r"params\.feed\.start: Field required"):
            run(feed_source(feed={}), serve(**{FEED_URL: rss()}))

    def test_a_list_page_needs_a_second_url(self) -> None:
        source = list_source().evolve(urls=[])
        with pytest.raises(AdapterConfigError, match="list_page needs the list"):
            run(source, serve(**{FEED_URL: rss()}))

    def test_expand_schedule_needs_a_schedule(self) -> None:
        with pytest.raises(AdapterConfigError, match="expand_schedule needs"):
            run(list_source(expand_schedule=True), serve(**{FEED_URL: rss()}))

    def test_extra_hosts_is_left_to_the_host_check(self) -> None:
        assert run(feed_source(extra_hosts=["a.example.edu"]), serve(**{FEED_URL: rss()})) == []
