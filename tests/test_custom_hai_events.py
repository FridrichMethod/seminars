"""The custom ``hai_events`` adapter: the recorded pages and hand-written payloads."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from factories import make_window
from fakes import FakeClock, FakeHttpClient
from seminars.config import load_sources
from seminars.http import offline_client
from seminars.models import EventDraft, SourceConfig, SpeakerDraft
from seminars.sources.base import ParseError, get_adapter
from seminars.sources.custom.hai_events import (
    MAX_EVENTS,
    MAX_LIST_ITEMS,
    Flight,
    HaiEventsAdapter,
    HaiParams,
    TextRow,
    cut_rows,
    looks_like_person,
    split_title,
)
from seminars.sources.generic.html_list import AdapterConfigError, load_params

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "custom-hai-events"
SOURCES = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
SOURCE = SOURCES["custom-hai-events"]
SITE = "https://hai.stanford.edu"
LIST_URL = f"{SITE}/events"
AI100 = f"{SITE}/events/hai-seminar-the-ai-100"
CHRISTIN = (
    f"{SITE}/events/angele-christin-hannah-zeavin-gurus-hucksters-entertainers-how-influencers"
    "-reshaped-social-media"
)
PHYSTAT = f"{SITE}/events/2027-conference-on-physics-and-ai-phystat-2027"
RECORDED = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
PARAMS = load_params(HaiParams, SOURCE)


def run(source: SourceConfig, http: Any, now: datetime = RECORDED, **window: int) -> list[Any]:
    adapter = get_adapter(source.adapter)
    payload = adapter.fetch(http, source, window=make_window(now, **window))
    return adapter.parse(payload, source)


def by_title(drafts: list[EventDraft], title: str) -> EventDraft:
    matches = [draft for draft in drafts if draft.title == title]
    assert len(matches) == 1, [draft.title for draft in drafts]
    return matches[0]


def with_params(**changes: Any) -> SourceConfig:
    return SOURCE.evolve(params={**SOURCE.params, **changes})


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def serve(pages: dict[str, str], now: datetime = RECORDED) -> FakeHttpClient:
    return FakeHttpClient(pages, clock=FakeClock(now))


# --- hand-written payloads -----------------------------------------------------------------


def event(title: str, start: str, **fields: Any) -> dict[str, Any]:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return {
        "title": title,
        "href": fields.pop("href", f"/events/{slug}"),
        "event_start": start,
        "event_end": fields.pop("end", None),
        "dek": fields.pop("dek", "<p>About it.</p>"),
        "tag": {"label": fields.pop("tag", "Seminar")},
        "contentMeta": {"topics": [{"title": topic} for topic in fields.pop("topics", ["AI"])]},
        "promo_url": fields.pop("promo", "$undefined"),
        **fields,
    }


def payload_page(rows: list[str], *, chunks: int = 3) -> str:
    """A page whose inline scripts carry ``rows`` as the flight payload, split into chunks."""
    text = "".join(rows)
    size = max(1, len(text) // chunks + 1)
    scripts = "".join(
        f"<script>self.__next_f.push([1,{json.dumps(text[i : i + size])}])</script>"
        for i in range(0, len(text), size)
    )
    return f"<html><body><main>No cards</main>{scripts}</body></html>"


def events_row(upcoming: list[Any], previous: list[Any], ident: str = "25") -> str:
    tree = [
        "$",
        "main",
        None,
        {"children": [{"entries": upcoming}, {"initialEntries": {"data": previous}}]},
    ]
    return f"{ident}:{json.dumps(tree)}\n"


def text_row(ident: str, text: str) -> str:
    return f"{ident}:T{len(text.encode('utf-8')):x},{text}"


def parse_rows(rows: list[str], source: SourceConfig = SOURCE) -> list[EventDraft]:
    return run(source, serve({LIST_URL: payload_page(rows)}))


def count_values(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    """Make ``Flight.value`` fail once it has been called more than ``limit`` times."""
    original = Flight.value
    calls = 0

    def counted(self: Flight, value: Any, depth: int = 0) -> Any:
        nonlocal calls
        calls += 1
        if calls > limit:
            raise AssertionError(f"more than {limit} values resolved")
        return original(self, value, depth)

    monkeypatch.setattr(Flight, "value", counted)


# --- the recorded pages --------------------------------------------------------------------


class TestRecordedPages:
    def test_the_adapter_is_registered_under_its_module_name(self) -> None:
        assert isinstance(get_adapter("hai_events"), HaiEventsAdapter)

    def test_the_configured_params_validate(self) -> None:
        assert PARAMS.fetch_details == "upcoming"
        assert PARAMS.keep_type_labels == ["Seminar", "Conference"]

    def test_the_list_and_the_detail_pages_of_its_upcoming_seminars(self) -> None:
        http = offline_client(SOURCE.id)
        drafts = run(SOURCE, http)
        # Events with a promo_url forward to other hosts: no request for them. The
        # conference in May 2027 is after the run window.
        assert http.requests == [LIST_URL, AI100, CHRISTIN]
        # 20 events in the payload; 5 are tagged Event or Event Promo and are left out.
        assert len(drafts) == 15
        assert all(draft.source_id == SOURCE.id and draft.extraction == "html" for draft in drafts)
        assert {draft.event_type_hint for draft in drafts} == {"seminar", "conference"}
        left_out = ("Special Seminar", "Center for Decoding", "NVIDIA", "Christopher Ong")
        assert not [d for d in drafts if (d.title or "").startswith(left_out)]

    def test_a_seminar_with_its_detail_page(self) -> None:
        drafts = run(SOURCE, offline_client(SOURCE.id))
        talk = by_title(drafts, "AI Goes Mainstream: The Scaling Era and the Rise of Generative AI")
        assert talk.series == "AI100"  # "AI100 | ..." names a series, not the speaker
        # "2026-11-18T12:00:00.000000Z" is Pacific wall time: the "Z" is dropped.
        assert talk.start == datetime(2026, 11, 18, 12, 0)
        assert talk.end == datetime(2026, 11, 18, 13, 15)
        assert [(s.name, s.affiliation, s.url) for s in talk.speakers] == [
            (
                "Michael Wooldridge",
                "Ashall Professor of the Foundations of Artificial Intelligence, "
                "University of Oxford",
                "https://www.cs.ox.ac.uk/people/michael.wooldridge/",
            )
        ]  # the moderator is not a speaker
        assert talk.location == "353 Jane Stanford Way, Stanford, CA, 94305 | Room 119"
        assert (
            talk.stream_url == "https://stanford.zoom.us/webinar/register/WN_OeOA7YGwQ8qGOfBdrTXx6w"
        )
        assert talk.is_online is True
        assert talk.url == AI100
        assert "third AI100 report" in (talk.abstract_html or "")
        assert talk.categories == ["Foundation Models", "Industry, Innovation", "Democracy"]

    def test_people_in_conversation_are_speakers(self) -> None:
        drafts = run(SOURCE, offline_client(SOURCE.id))
        talk = by_title(
            drafts, "Gurus, Hucksters, Entertainers: How Influencers Reshaped Social Media"
        )
        assert [(s.name, s.url) for s in talk.speakers] == [
            ("Ang\u00e8le Christin", f"{SITE}/people/angele-christin"),
            ("Hannah Zeavin", "https://www.zeavin.org/"),
        ]
        assert (talk.series, talk.speaker_text) == (None, None)

    def test_an_event_with_a_promo_url_comes_from_the_list_alone(self) -> None:
        drafts = run(SOURCE, offline_client(SOURCE.id))
        talk = by_title(drafts, "Best Practices for the Use of LLMs as Judges")
        assert talk.speaker_text == "Khaoula Chehbouni"
        assert talk.speakers == []
        assert (talk.start, talk.end) == (
            datetime(2026, 11, 13, 13),
            datetime(2026, 11, 13, 14, 15),
        )
        assert talk.location is None
        assert (
            talk.url
            == f"{SITE}/events/khaoula-chehbouni-best-practices-for-the-use-of-llms-as-judges"
        )
        assert talk.abstract_html == "<p>An AI Measurement Science (AIMS) Seminar Series</p>"

    def test_events_sharing_a_promo_url_keep_their_own_urls(self) -> None:
        drafts = run(SOURCE, offline_client(SOURCE.id))
        marlowe = [d for d in drafts if d.series == "Marlowe"]
        assert len(marlowe) == 5
        assert len({draft.url for draft in marlowe}) == 5
        yamins = by_title(drafts, "AI + Data for Science with Dan Yamins")
        assert (yamins.start, yamins.end) == (datetime(2026, 10, 28, 16, 30), None)

    def test_the_conference_detail_page_in_a_wider_window(self) -> None:
        http = offline_client(SOURCE.id)
        drafts = run(SOURCE, http, days_ahead=240)
        assert http.requests == [LIST_URL, AI100, CHRISTIN, PHYSTAT]
        conference = by_title(drafts, "2027 Conference on Physics and AI (PhyStat 2027)")
        assert (conference.start, conference.end) == (
            datetime(2027, 5, 24, 9),
            datetime(2027, 5, 26, 17),
        )
        assert conference.location == (
            "Simonyi Conference Center, 389 Jane Stanford Way, Stanford, CA 94305"
        )
        assert conference.speakers == []
        assert conference.event_type_hint == "conference"

    def test_a_listed_promo_host_is_requested_and_a_forwarding_page_is_ignored(self) -> None:
        source = with_params(extra_hosts=["events.stanford.edu"])
        target = "https://events.stanford.edu/event/cnds-seminar-youngju-jo"
        pages = {
            LIST_URL: fixture("events-ba2e38a4.html"),
            AI100: fixture("events-hai-seminar-the-ai-100-43da3f5a.html"),
            CHRISTIN: fixture("events-angele-christin-hannah-zeavin-gur-29ce1f27.html"),
            target: fixture("events-youngju-jo-data-driven-discovery-c915b628.html"),
        }
        http = serve(pages)
        drafts = run(source, http)
        assert target in http.requests
        talk = by_title(drafts, "Data-driven Discovery and Control of Neural Computations")
        assert (talk.speaker_text, talk.location) == ("YoungJu Jo", None)

    def test_the_cards_when_the_payload_cannot_be_read(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        text = re.sub(
            r"<script>self\.__next_f\.push.*?</script>",
            "",
            fixture("events-ba2e38a4.html"),
            flags=re.S,
        )
        with caplog.at_level(logging.WARNING):
            drafts = run(with_params(fetch_details="never"), serve({LIST_URL: text}))
        assert "the cards are used" in caplog.text
        assert len(drafts) == 15
        talk = by_title(drafts, "Best Practices for the Use of LLMs as Judges")
        assert (talk.start, talk.end) == (
            datetime(2026, 11, 13, 13),
            datetime(2026, 11, 13, 14, 15),
        )
        assert talk.speaker_text == "Khaoula Chehbouni"
        conference = by_title(drafts, "2027 Conference on Physics and AI (PhyStat 2027)")
        assert conference.start == date(2027, 5, 24)  # no time on the card: all day

    def test_forwarding_pages_found_through_the_cards_are_ignored(self) -> None:
        text = re.sub(
            r"<script>self\.__next_f\.push.*?</script>",
            "",
            fixture("events-ba2e38a4.html"),
            flags=re.S,
        )
        forward = f"{SITE}/events/khaoula-chehbouni-best-practices-for-the-use-of-llms-as-judges"
        pages = {
            LIST_URL: text,
            forward: fixture("events-khaoula-chehbouni-best-practices-b9e1089d.html"),
        }
        http = serve(pages)
        drafts = run(SOURCE, http)
        assert forward in http.requests  # the cards do not know the promo_url
        talk = by_title(drafts, "Best Practices for the Use of LLMs as Judges")
        assert (talk.location, talk.speakers) == (None, [])


# --- hand-written payloads -----------------------------------------------------------------


class TestPayload:
    def test_rows_are_cut_by_length_on_utf8_bytes(self) -> None:
        # A text row with a character of two bytes, directly followed by the events row.
        logo = text_row("c", "M0 0h\u00e9")
        rows = [
            '0:["$","$Lc"]\n',
            ':HL["/style.css"]\n',
            logo,
            events_row([event("T", "2026-11-17")], []),
        ]
        [talk] = parse_rows(rows)
        assert talk.start == date(2026, 11, 17)  # a date only: all day
        assert talk.title == "T"

    def test_references_escapes_and_markers(self) -> None:
        long_title = "A" * 1100
        upcoming = [
            event("$3", "2026-11-17T16:00:00.000000Z", dek="$$5 lunch", end="$undefined"),
            event("$$ matters", "2026-11-18T16:00:00.000000Z", dek="$L1a"),
            event("$4:props:children", "2026-11-19T16:00:00.000000Z"),
            event("$9", "2026-11-20T16:00:00.000000Z"),  # no such row
        ]
        rows = [
            text_row("3", long_title),
            '4:["$","h1",null,{"children":"From a tree"}]\n',
            '8:I[123,[],"x"]\n',
            '9:E{"digest":"boom"}\n',
            events_row(upcoming, []),
        ]
        drafts = parse_rows(rows)
        assert [draft.title for draft in drafts] == [long_title, "$ matters", "From a tree", None]
        assert drafts[0].abstract_html == "$5 lunch"
        assert drafts[0].end is None
        assert drafts[1].abstract_html is None  # a component reference is no text

    def test_a_reference_cycle_counts_as_missing(self) -> None:
        rows = ['6:"$7"\n', '7:"$6"\n', events_row([event("$6", "2026-11-17")], [])]
        assert [draft.title for draft in parse_rows(rows)] == [None]

    def test_nested_references_are_resolved_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Every step of the path of R(j) resolves R(j + 1) again: without memory the
        # work grows as steps ** levels (about 35 million values here).
        steps, levels = 12, 7

        def ref(j: int) -> str:
            return f"$f{j}" if j == levels else f"$d{j}" + ":x" * steps + ":y"

        rows = [f"d{j}:{json.dumps({'x': ref(j + 1)})}\n" for j in range(levels)]
        rows += [
            f"f{j}:{json.dumps({'x': ref(j), 'y': f'$f{j - 1}' if j else 'leaf'})}\n"
            for j in range(levels + 1)
        ]
        count_values(monkeypatch, limit=5_000)
        [talk] = parse_rows([*rows, events_row([event(ref(0), "2026-11-17")], [])])
        assert talk.title is None  # the reference is an object, not a text

    def test_a_list_that_many_objects_refer_to_is_read_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        items = [event(f"T{n}", "2026-11-17") for n in range(MAX_LIST_ITEMS)]
        holders = json.dumps([{"entries": "$5"} for _ in range(2_000)])
        rows = [f"5:{json.dumps(items)}\n", f"6:{holders}\n", events_row([], [])]
        count_values(monkeypatch, limit=100_000)
        assert len(parse_rows(rows, with_params(fetch_details="never"))) == MAX_LIST_ITEMS

    def test_at_most_max_events_are_taken(self) -> None:
        lists = [
            [event(f"T{n}-{m}", "2026-11-17") for m in range(MAX_LIST_ITEMS)]
            for n in range(MAX_EVENTS // MAX_LIST_ITEMS + 1)
        ]
        rows = [f"{n}:{json.dumps({'entries': items})}\n" for n, items in enumerate(lists)]
        drafts = parse_rows([*rows, events_row([], [])], with_params(fetch_details="never"))
        assert len(drafts) == MAX_EVENTS

    def test_rows_that_are_not_json_are_skipped(self) -> None:
        rows = ["5:{not json\n", events_row([], [event("Old", "2026-10-01T12:00:00Z")])]
        assert [draft.title for draft in parse_rows(rows)] == ["Old"]

    def test_lists_without_events_are_a_page_without_events(self) -> None:
        assert parse_rows([events_row([], [])]) == []

    def test_the_same_event_in_two_lists_is_one_draft(self) -> None:
        item = event("Twice", "2026-11-17T12:00:00Z")
        assert len(parse_rows([events_row([item], [item])])) == 1

    def test_items_without_the_marker_or_a_start_are_not_events(self) -> None:
        broken = event("Broken", "soon")
        rows = [events_row([{"title": "No start"}, broken, event("Ok", "2026-11-17")], [])]
        assert [draft.title for draft in parse_rows(rows)] == ["Ok"]

    def test_an_end_before_the_start_or_of_another_kind_is_dropped(self) -> None:
        upcoming = [
            event("Back", "2026-11-17T12:00:00Z", end="2026-11-17T11:00:00Z"),
            event("Mixed", "2026-11-17T12:00:00Z", end="2026-11-18"),
        ]
        assert [draft.end for draft in parse_rows([events_row(upcoming, [])])] == [None, None]

    def test_times_with_an_offset_when_they_are_not_local(self) -> None:
        upcoming = [event("Utc", "2026-11-17T20:00:00Z", end="2026-11-17T21:00:00Z")]
        [talk] = parse_rows([events_row(upcoming, [])], with_params(times_are_local=False))
        assert talk.start == datetime(2026, 11, 17, 20, tzinfo=UTC)
        assert talk.end == datetime(2026, 11, 17, 21, tzinfo=UTC)

    def test_a_payload_that_cannot_be_cut_falls_back_to_the_cards(self) -> None:
        page = payload_page(["?? not a row\n"]).replace("No cards", CARD)
        [talk] = run(with_params(fetch_details="never"), serve({LIST_URL: page}))
        assert talk.title == "From a card"

    def test_a_payload_without_the_lists_falls_back_to_the_cards(self) -> None:
        page = payload_page(['0:{"other":[]}\n']).replace("No cards", CARD)
        [talk] = run(with_params(fetch_details="never"), serve({LIST_URL: page}))
        assert (talk.start, talk.end) == (datetime(2026, 11, 17, 16, 30), None)
        assert talk.abstract_html == "<p>Teaser</p>"

    def test_a_page_with_neither_payload_nor_cards_is_not_understood(self) -> None:
        adapter = HaiEventsAdapter()
        http = serve({LIST_URL: "<html><body>Maintenance</body></html>"})
        payload = adapter.fetch(http, SOURCE, window=make_window(RECORDED))  # fetch succeeds
        assert http.requests == [LIST_URL]
        with pytest.raises(ParseError, match="neither the payload nor cards"):
            adapter.parse(payload, SOURCE)

    def test_a_payload_without_the_list_page_is_not_understood(self) -> None:
        adapter = HaiEventsAdapter()
        http = serve({LIST_URL: payload_page([events_row([], [])])})
        payload = adapter.fetch(http, SOURCE, window=make_window(RECORDED))
        other = payload.evolve(documents=[payload.documents[0].evolve(label="other")])
        with pytest.raises(ParseError, match="no list page"):
            adapter.parse(other, SOURCE)

    def test_a_bad_text_row_length_cannot_be_cut(self) -> None:
        params = PARAMS.flight.evolve(text_row_pattern=r"(?P<id>[0-9a-f]+):T(?P<length>\w+),")
        with pytest.raises(ValueError, match="text row length"):
            cut_rows(params, "c:Tzz,abc")

    def test_reference_steps_into_lists_and_elements(self) -> None:
        rows = {
            "1": ["$", "p", None, {"children": ["a", "b"]}],
            "2": [1],
            "3": TextRow("$2 is text"),
        }
        flight = Flight(PARAMS.flight, rows)
        assert flight.value("$1:props:children:1") == "b"
        assert flight.value("$1:props:children:9") is None
        assert flight.value("$2:0:x") is None
        assert flight.value("$" + "1" * 300) is None
        assert flight.value("$3") == "$2 is text"  # a text row is never a reference
        assert flight.value("$3:x") is None
        assert flight.at({"a": {"b": "$2"}}, "a.b") == [1]


CARD = (
    '<div class="ContentItem_root__x"><a class="ContentItem_titleLink__y" href="/events/c">'
    'From a card</a><span class="EventContentMeta_type__z">Seminar</span>'
    '<div class="EventContentMeta_root__w"><span>Nov 17, 2026</span>'
    '<span class="EventContentMeta_time__v">4:30 PM</span></div>'
    '<div class="ContentItemDek_root__u"><p>Teaser</p></div></div>'
)


# --- detail pages --------------------------------------------------------------------------

DETAIL = """<html><body><h1 class="DetailHero_heading__a">T</h1>
<div class="DetailMeta_row__b"><div>Status</div><div><span>Cancelled</span></div></div>
<div class="DetailMeta_row__b"><div>Date</div>
<div>Tuesday, November 17, 2026 4:00 PM - 5:30 PM</div></div>
<div class="DetailMeta_row__b"><div>Location</div><div>Gates 104</div></div>
<div class="DetailMeta_row__b"><div>Topics</div><div>Robotics</div></div>
<div class="DetailMeta_row__b"><div>Lonely</div></div>
</body></html>"""


class TestDetailPages:
    def rows(self, **fields: Any) -> list[str]:
        return [events_row([event("Ada Lovelace | Engines", "2026-11-17T16:00:00Z", **fields)], [])]

    def test_status_end_and_topics_from_the_detail_page(self) -> None:
        detail = f"{SITE}/events/ada-lovelace-engines"
        http = serve({LIST_URL: payload_page(self.rows()), detail: DETAIL})
        [talk] = run(SOURCE, http)
        assert http.requests == [LIST_URL, detail]
        assert talk.status_hint == "cancelled"
        assert talk.end == datetime(2026, 11, 17, 17, 30)  # the list has no end
        assert talk.location == "Gates 104"
        assert talk.categories == ["AI", "Robotics"]
        # No speakers on the detail page: the prefix reads like a name.
        assert (talk.title, talk.speaker_text, talk.series) == ("Engines", "Ada Lovelace", None)

    def test_a_failed_detail_request_keeps_the_list_data(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            [talk] = run(SOURCE, serve({LIST_URL: payload_page(self.rows())}))
        assert "detail page skipped" in caplog.text
        assert (talk.title, talk.location) == ("Engines", None)

    def test_a_detail_page_without_a_title_is_not_used(self) -> None:
        detail = f"{SITE}/events/ada-lovelace-engines"
        page = DETAIL.replace("DetailHero_heading__a", "Other")
        [talk] = run(SOURCE, serve({LIST_URL: payload_page(self.rows()), detail: page}))
        assert (talk.location, talk.status_hint) == (None, None)

    def test_only_kept_upcoming_events_in_the_window_on_the_source_host(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        upcoming = [
            event("Past", "2026-11-01T12:00:00Z"),
            event("Promo", "2026-11-20T12:00:00Z", promo="https://other.example.org/x"),
            event("Elsewhere", "2026-11-21T12:00:00Z", href="https://other.example.org/y"),
            event("Policy", "2026-11-22T12:00:00Z", tag="Event"),
            event("Far", "2027-09-01T12:00:00Z"),
            event("Soon", "2026-11-23T12:00:00Z"),
            event("Later", "2026-11-24T12:00:00Z"),
        ]
        http = serve({LIST_URL: payload_page([events_row(upcoming, [])])})
        with caplog.at_level(logging.WARNING):
            drafts = run(with_params(max_detail_pages=1), http)
        assert http.requests == [LIST_URL, f"{SITE}/events/soon"]
        assert "2 event(s) have their details on a host" in caplog.text
        assert [d.title for d in drafts] == ["Past", "Promo", "Elsewhere", "Far", "Soon", "Later"]

    def test_no_detail_requests_when_they_are_not_wanted(self) -> None:
        http = serve({LIST_URL: payload_page(self.rows())})
        run(with_params(fetch_details="never"), http)
        assert http.requests == [LIST_URL]


# --- titles --------------------------------------------------------------------------------


class TestTitles:
    @pytest.mark.parametrize(
        ("text", "person"),
        [
            ("Khaoula Chehbouni", True),
            ("Olawale (Wale) Salaudeen", True),
            ("Marcos Santiago Rojas Pino", True),
            ("J. Robert Oppenheimer", True),
            ("Ludwig van Beethoven", True),
            ("Ang\u00e8le Christin", True),
            ("Marlowe", False),
            ("AI100", False),
            ("HAI", False),
            ("AI Governance Lab", False),
            ("Digital Economy", False),
            ("Agent 007", False),
            ("one two", False),
            ("A B C D E F", False),
        ],
    )
    def test_looks_like_person(self, text: str, person: bool) -> None:
        assert looks_like_person(text) is person

    @pytest.mark.parametrize(
        ("title", "speakers", "expected"),
        [
            ("No separator", [], ("No separator", None, None)),
            (" | Rest", [], (" | Rest", None, None)),
            ("Jane Doe & John Roe | Talk", [], ("Talk", None, "Jane Doe & John Roe")),
            ("NVIDIA & Marlowe | Talk", [], ("Talk", "NVIDIA & Marlowe", None)),
            ("jane doe | Talk", ["Jane Doe"], ("Talk", None, None)),
            ("Series | Talk", ["Jane Doe"], ("Talk", "Series", None)),
            (None, [], (None, None, None)),
        ],
    )
    def test_split_title(
        self, title: str | None, speakers: list[str], expected: tuple[Any, ...]
    ) -> None:
        people = [SpeakerDraft(name=name) for name in speakers]
        assert split_title(PARAMS, title, people) == expected

    def test_without_a_separator_titles_stay_whole(self) -> None:
        params = PARAMS.evolve(title_separator=None)
        assert split_title(params, "Jane Doe | Talk", []) == ("Jane Doe | Talk", None, None)


# --- params --------------------------------------------------------------------------------


class TestParams:
    def test_an_unknown_key_is_an_error_that_names_it(self) -> None:
        with pytest.raises(AdapterConfigError, match=r"params\.skip_details_when_set: unknown key"):
            load_params(HaiParams, with_params(skip_details_when_set="promo_url"))

    def test_an_unknown_key_of_the_flight_block_is_named(self) -> None:
        flight = {**SOURCE.params["flight"], "row_end": "\n"}
        with pytest.raises(AdapterConfigError, match=r"params\.flight\.row_end: unknown key"):
            load_params(HaiParams, with_params(flight=flight))

    @pytest.mark.parametrize(
        ("key", "value", "message"),
        [
            ("script_pattern", "push", "script_pattern needs"),
            ("text_row_pattern", "(?P<id>x):T", "text_row_pattern needs"),
            ("row_start_pattern", "(?P<id>\u00e9):", "must be ASCII"),
            ("upcoming_key", "entries..x", "invalid key path"),
            ("keys", {"title": "a[b]", "start": "s", "link": "l"}, "invalid key path"),
        ],
    )
    def test_bad_flight_values(self, key: str, value: Any, message: str) -> None:
        flight = {**SOURCE.params["flight"], key: value}
        with pytest.raises(AdapterConfigError, match=message):
            load_params(HaiParams, with_params(flight=flight))

    def test_detail_requests_need_detail_selectors(self) -> None:
        source = SOURCE.evolve(
            params={k: v for k, v in SOURCE.params.items() if k != "detail_selectors"}
        )
        with pytest.raises(AdapterConfigError, match="fetch_details needs detail_selectors"):
            load_params(HaiParams, source)

    def test_the_card_time_pattern_needs_a_start(self) -> None:
        with pytest.raises(AdapterConfigError, match="card_time_pattern needs"):
            load_params(HaiParams, with_params(card_time_pattern=r"\d+"))

    def test_without_keep_labels_every_label_is_kept(self) -> None:
        params = PARAMS.evolve(keep_type_labels=[])
        assert params.keeps("Event Promo")
        assert params.event_type("Event Promo") == "other"
        assert params.event_type(None) is None
