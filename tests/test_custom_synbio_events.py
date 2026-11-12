"""The custom ``synbio_events`` adapter: the recorded page and hand-written edge cases."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from factories import make_window
from fakes import FakeHttpClient
from seminars.config import load_sources
from seminars.http import offline_client
from seminars.models import EventDraft, SourceConfig
from seminars.sources.base import ParseError, get_adapter
from seminars.sources.custom.synbio_events import SynbioEventsAdapter, SynbioParams
from seminars.sources.generic.html_list import AdapterConfigError, load_params

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCES = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
SOURCE = SOURCES["custom-synbio-events"]
PAGE_URL = "https://sb.stanford.edu/events"
EN_DASH = "\u2013"


def run(source: SourceConfig, http: Any) -> list[EventDraft]:
    adapter = get_adapter(source.adapter)
    return adapter.parse(adapter.fetch(http, source, window=make_window()), source)


def by_title(drafts: list[EventDraft], title: str) -> EventDraft:
    matches = [draft for draft in drafts if draft.title == title]
    assert len(matches) == 1, [draft.title for draft in drafts]
    return matches[0]


def with_params(**changes: Any) -> SourceConfig:
    return SOURCE.evolve(params={**SOURCE.params, **changes})


def block(key: str | None, heading: str, *lines: str, tag: str = "h2") -> str:
    ident = f' id="h.{key}_l"' if key else ""
    paragraphs = "".join(f"<p><span>{line}</span></p>" for line in lines)
    return f'<div id="h.{key}"></div><{tag}{ident}><span>{heading}</span></{tag}>{paragraphs}'


def page(*blocks: str) -> str:
    groups = '<h2 id="h.up_l">Upcoming Events</h2>'
    body = "".join(blocks)
    return f'<html><body><section><div class="tyJCtd">{groups}{body}</div></section></body></html>'


def parse(text: str, source: SourceConfig = SOURCE) -> list[EventDraft]:
    return run(source, FakeHttpClient({PAGE_URL: text}))


WHEN = "When: 12:00 pm - 1:00 pm on Tuesday, November 17, 2026"


# --- the recorded page ---------------------------------------------------------------------


class TestRecordedPage:
    def test_the_adapter_is_registered_under_its_module_name(self) -> None:
        assert isinstance(get_adapter("synbio_events"), SynbioEventsAdapter)

    def test_the_configured_params_validate(self) -> None:
        params = load_params(SynbioParams, SOURCE)
        assert len(params.speaker_heading_patterns) == 3

    def test_every_event_block_becomes_a_draft(self) -> None:
        http = offline_client(SOURCE.id)
        drafts = run(SOURCE, http)
        assert http.requests == [PAGE_URL]
        # 94 blocks, one of which holds a second event; the footer is not an event.
        assert len(drafts) == 95
        assert all(draft.source_id == SOURCE.id and draft.extraction == "html" for draft in drafts)
        assert len({draft.native_id for draft in drafts}) == 95
        assert not [draft for draft in drafts if draft.title == "Stanford|Synthetic Biology"]
        assert all(draft.stream_url is None for draft in drafts)

    def test_a_talk_with_a_title_line(self) -> None:
        drafts = run(SOURCE, offline_client(SOURCE.id))
        talk = by_title(drafts, "Deep learning-guided design of cell type-specific AAV vectors")
        assert talk.start == datetime(2026, 10, 27, 12, 0)  # naive wall time of the source
        assert talk.end == datetime(2026, 10, 27, 13, 0)
        assert talk.speaker_text == "Dr. Sean Wang"
        assert talk.speakers == []
        assert talk.location == "Shriram Tea Room"
        assert talk.native_id == "s2bo6qrnxu6w"
        assert talk.url == "https://sb.stanford.edu/events#h.s2bo6qrnxu6w"

    def test_an_upcoming_talk_without_a_title_is_named_by_its_heading(self) -> None:
        drafts = run(SOURCE, offline_client(SOURCE.id))
        talk = by_title(drafts, "SB.Talk with Dr. Ayush Pandey from UC Merced")
        assert talk.start == datetime(2026, 12, 1, 12, 0)
        assert [(s.name, s.affiliation) for s in talk.speakers] == [
            ("Dr. Ayush Pandey", "UC Merced")
        ]
        assert talk.speaker_text is None
        assert talk.abstract == "Lunch provided"

    def test_linked_names_are_speakers_with_their_page(self) -> None:
        drafts = run(SOURCE, offline_client(SOURCE.id))
        talk = by_title(
            drafts,
            "Development of Photoswitchable DARPins to control the activity of target proteins",
        )
        # The heading also links "Lin Lab", which is not a person.
        assert [(s.name, s.url) for s in talk.speakers] == [
            ("Daesun Song", "https://profiles.stanford.edu/daesun-song?tab=publications")
        ]
        assert talk.speaker_text == "Daesun Song (Lin Lab)"
        pair = by_title(drafts, "Let\u2019s get a NSF Biofoundry at Stanford")
        assert [s.name for s in pair.speakers] == ["Jenn Brophy", "Drew Endy"]
        company = by_title(drafts, "Fireside Chat with Jeffrey Kim (Slingshot Biosciences)")
        assert company.speakers == []
        assert company.speaker_text == "Jeffrey Kim (Slingshot Biosciences)"

    def test_a_second_when_line_starts_another_event(self) -> None:
        drafts = run(SOURCE, offline_client(SOURCE.id))
        expo = by_title(drafts, "2025 Stanford Synthetic Biology Expo")
        assert expo.start == date(2025, 9, 12)
        assert expo.is_all_day
        assert "Poster Setup" in (expo.abstract or "")
        assert "iGEM" not in (expo.abstract or "")
        talk = next(d for d in drafts if d.native_id == "mm237rpco6n6/2")
        assert talk.title == "Meet the Stanford iGEM Team / Undergraduate Research Talk"
        assert talk.start == datetime(2025, 7, 29, 12, 0)
        assert talk.end is None
        assert talk.location == "Shriram Tea Room"
        assert talk.url == expo.url

    def test_the_hand_written_when_lines(self) -> None:
        drafts = run(SOURCE, offline_client(SOURCE.id))
        workshop = by_title(drafts, "International Mammalian Synthetic Biology Workshop")
        assert (workshop.start, workshop.end) == (date(2023, 6, 22), date(2023, 6, 23))
        assert "Come listen to leading experts" in (workshop.abstract or "")  # a long "Topic"
        misspelt = by_title(drafts, "Synthetic biology for discovery, therapeutics and fun")
        assert misspelt.start == datetime(2026, 10, 13, 12, 0)  # "Ocober 13, 2026"
        dashed = next(d for d in drafts if d.native_id == "4j7sw235hhiz")
        assert (dashed.start, dashed.end) == (datetime(2026, 5, 7, 15), datetime(2026, 5, 7, 16))
        remark = next(d for d in drafts if d.native_id == "qoskl4enzmxj")
        assert remark.start == datetime(2024, 4, 15, 16, 30)  # "(Refreshments served at 4pm)"
        no_comma = next(d for d in drafts if d.native_id == "wesskl8u6ssw")
        assert no_comma.start == datetime(2024, 9, 24, 15, 0)  # "3 pm on Tuesday, Sept 24 2024"

    def test_a_where_line_gives_its_text_and_no_link(self) -> None:
        drafts = run(SOURCE, offline_client(SOURCE.id))
        chat = by_title(drafts, "Fireside Chat with Austin Che, Ginkgo Bioworks")
        assert chat.location == "RVSP for location"
        assert "docs.google.com" not in chat.model_dump_json()


# --- hand-written pages --------------------------------------------------------------------


class TestHandWrittenPages:
    def test_a_meeting_link_in_the_where_line_is_never_copied(self) -> None:
        link = '<a href="https://stanford.zoom.us/j/123?pwd=REDACTED">Zoom</a>'
        [talk] = parse(page(block("a1", "SB.Talk with Ada Lovelace", WHEN, f"Where: {link}")))
        assert talk.location == "Zoom"
        assert talk.stream_url is None
        assert "pwd" not in talk.model_dump_json()

    @pytest.mark.parametrize(
        ("heading", "lines", "title", "location"),
        [
            (
                "CANCELLED: SB.Talk with Ada Lovelace",
                ["Where: Shriram 104"],
                "CANCELLED: SB.Talk with Ada Lovelace",
                "Shriram 104",
            ),
            ("SB.Talk", ["Title: Engines (cancelled)"], "Engines (cancelled)", None),
            (
                "SB.Talk",
                ["Title: Engines", "Where: Shriram 104 &amp; Zoom (link by email)"],
                "Engines",
                "Shriram 104 & Zoom (link by email)",
            ),
            ("SB.Talk", ["Title: Engines", "Where: Zoom"], "Engines", "Zoom"),
        ],
    )
    def test_cancelled_and_hybrid_events_pass_their_text_through(
        self, heading: str, lines: list[str], title: str, location: str | None
    ) -> None:
        # Normalization reads the status and the online part from the text.
        [talk] = parse(page(block("a1", heading, WHEN, *lines)))
        assert (talk.title, talk.location) == (title, location)
        assert (talk.status_hint, talk.is_online, talk.stream_url) == (None, None, None)

    def test_blocks_without_a_when_line_are_not_events(self) -> None:
        drafts = parse(page(block("a1", "Lab news", "We moved."), block("b2", "Talk", WHEN)))
        assert [draft.title for draft in drafts] == ["Talk"]

    def test_a_page_without_any_event_is_not_understood(self) -> None:
        with pytest.raises(ParseError, match="no block on the page has a When line"):
            parse(page(block("a1", "Lab news", "We moved.")))

    def test_a_page_without_the_container_is_not_understood(self) -> None:
        with pytest.raises(ParseError, match="matches nothing"):
            parse("<html><body><p>When: soon</p></body></html>")

    def test_an_unreadable_when_line_skips_the_event(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        blocks = page(block("a1", "Soon", "When: to be announced"), block("b2", "Talk", WHEN))
        with caplog.at_level(logging.WARNING):
            drafts = parse(blocks)
        assert [draft.title for draft in drafts] == ["Talk"]
        assert "unreadable When line" in caplog.text

    def test_impossible_dates_skip_the_event(self) -> None:
        lines = ["When: noon on Monday, February 30, 2026", "When: Smarch 3, 2026"]
        drafts = parse(page(*(block(f"k{i}", "Talk", line) for i, line in enumerate(lines))))
        assert drafts == []

    def test_times_that_cannot_be_read_give_an_all_day_event(self) -> None:
        [talk] = parse(page(block("a1", "Social", "When: noonish on Tuesday, Nov 17, 2026")))
        assert talk.start == date(2026, 11, 17)
        assert talk.is_all_day

    def test_one_meridiem_completes_the_other_time(self) -> None:
        when = f"When: 11:30 {EN_DASH} 1:00pm on Tuesday, November 17, 2026"
        [talk] = parse(page(block("a1", "Talk", when)))
        assert (talk.start, talk.end) == (
            datetime(2026, 11, 17, 11, 30),
            datetime(2026, 11, 17, 13),
        )

    def test_a_range_of_days_over_new_year(self) -> None:
        when = "When: Wednesday, December 30 to Saturday, January 2, 2027"
        [retreat] = parse(page(block("a1", "Retreat", when)))
        assert (retreat.start, retreat.end) == (date(2026, 12, 30), date(2027, 1, 2))

    def test_a_backwards_range_of_days_is_unreadable(self) -> None:
        when = "When: Friday, June 23 to Thursday, June 22, 2023"
        assert parse(page(block("a1", "Retreat", when), block("b2", "Talk", WHEN)))[0].title == (
            "Talk"
        )

    def test_a_second_when_line_without_a_name_keeps_the_heading(self) -> None:
        second = "When: 12:00 pm on Tuesday, November 24, 2026"
        drafts = parse(page(block("a1", "Series", WHEN, second, "Where: Room 1")))
        assert [(d.title, d.native_id, d.location) for d in drafts] == [
            ("Series", "a1", None),
            ("Series", "a1/2", "Room 1"),
        ]

    def test_a_heading_without_an_id_has_no_native_id_and_no_url(self) -> None:
        [talk] = parse(page(block(None, "Talk", WHEN)))
        assert (talk.native_id, talk.url) == (None, None)

    def test_paragraphs_before_any_event_heading_belong_to_nothing(self) -> None:
        text = page("<p>When: 1pm on Monday, November 16, 2026</p>", block("a1", "Talk", WHEN))
        assert [draft.title for draft in parse(text)] == ["Talk"]

    def test_title_labels_are_compared_without_case(self) -> None:
        lines = (WHEN, "TALK TITLE: Engines", "Lunch provided")
        [talk] = parse(page(block("a1", "SB Talk by Ada Lovelace", *lines, tag="h1")))
        assert talk.title == "Engines"
        assert talk.abstract == "Lunch provided"
        assert talk.speaker_text == "Ada Lovelace"

    def test_tba_titles_and_overlong_titles_fall_back_to_the_name(self) -> None:
        long_topic = "Topic: " + "Join us for a long afternoon of talks. " * 6
        drafts = parse(
            page(block("a1", "First", WHEN, "Title: TBD"), block("b2", "Second", WHEN, long_topic))
        )
        assert [draft.title for draft in drafts] == ["First", "Second"]
        assert "long afternoon" in (drafts[1].abstract or "")

    def test_entities_and_names_outside_ascii(self) -> None:
        heading = 'SB.Talk with <a class="XqQF9c" href="/p/zurcher">Jerome Z\u00fcrcher</a> (MRC)'
        [talk] = parse(page(block("a1", heading, WHEN, "Title: Cells &amp; circuits")))
        assert talk.title == "Cells & circuits"
        assert [(s.name, s.url) for s in talk.speakers] == [
            ("Jerome Z\u00fcrcher", "https://sb.stanford.edu/p/zurcher")
        ]
        assert talk.speaker_text == "Jerome Z\u00fcrcher (MRC)"

    def test_text_split_over_spans_is_joined_without_a_separator(self) -> None:
        parts = ["When: 1", "0:00 a", "m - 4:00 pm on Friday, Nov 20, 2026"]
        when = "".join(f"<span>{part}</span>" for part in parts)
        [talk] = parse(page(block("a1", "Workshop", when)))
        assert (talk.start, talk.end) == (datetime(2026, 11, 20, 10), datetime(2026, 11, 20, 16))

    def test_a_block_of_many_paragraphs_takes_linear_time(self) -> None:
        # The block used to be rebuilt for every paragraph: about 6 s for this page.
        text = page(block("big", "Big", WHEN, *(["x"] * 50_000)))
        started = time.perf_counter()
        [talk] = parse(text)
        assert time.perf_counter() - started < 1.5
        assert talk.title == "Big"
        assert (talk.abstract or "").count("x") == 50_000

    def test_a_payload_without_the_page_is_not_understood(self) -> None:
        adapter = SynbioEventsAdapter()
        http = FakeHttpClient({PAGE_URL: page(block("a1", "Talk", WHEN))})
        payload = adapter.fetch(http, SOURCE, window=make_window())
        other = payload.evolve(documents=[payload.documents[0].evolve(label="other")])
        with pytest.raises(ParseError, match="no page"):
            adapter.parse(other, SOURCE)


# --- params --------------------------------------------------------------------------------


class TestParams:
    def test_an_unknown_key_is_an_error_that_names_it(self) -> None:
        with pytest.raises(AdapterConfigError, match=r"params\.require_line: unknown key"):
            load_params(SynbioParams, with_params(require_line="when"))

    def test_the_old_single_speaker_pattern_key_is_unknown(self) -> None:
        with pytest.raises(AdapterConfigError, match=r"params\.speaker_heading_pattern: unknown"):
            load_params(SynbioParams, with_params(speaker_heading_pattern="(?P<speaker>.+)"))

    @pytest.mark.parametrize(
        ("key", "value", "message"),
        [
            ("heading_id_pattern", "h", "heading_id_pattern needs"),
            ("when_pattern", "When:", "when_pattern needs"),
            ("when_range_pattern", "(?P<year>.)", "when_range_pattern needs"),
            ("times_pattern", ".", "times_pattern needs"),
            ("speaker_heading_patterns", ["with (.+)"], "speaker_heading_patterns needs"),
            ("anchor_url_template", "https://sb.stanford.edu/events", "needs {key}"),
            ("max_title_length", 0, "max_title_length"),
        ],
    )
    def test_bad_values_are_errors(self, key: str, value: Any, message: str) -> None:
        with pytest.raises(AdapterConfigError, match=message):
            load_params(SynbioParams, with_params(**{key: value}))

    def test_fetch_checks_the_params_before_any_request(self) -> None:
        http = FakeHttpClient({PAGE_URL: page()})
        with pytest.raises(AdapterConfigError):
            SynbioEventsAdapter().fetch(http, with_params(nope=1), window=make_window())
        assert http.requests == []
