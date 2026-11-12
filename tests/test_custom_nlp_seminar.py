"""The custom ``nlp_seminar`` adapter: the recorded schedule and hand-written edge cases."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from factories import make_window
from fakes import FakeClock, FakeHttpClient
from seminars.config import load_sources
from seminars.http import offline_client
from seminars.models import EventDraft, SourceConfig
from seminars.sources.base import ParseError, get_adapter
from seminars.sources.custom import nlp_seminar
from seminars.sources.custom.nlp_seminar import NlpParams, NlpSeminarAdapter, text_lines
from seminars.sources.generic.html_list import AdapterConfigError, load_params

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "custom-nlp-seminar"
SOURCES = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
SOURCE = SOURCES["custom-nlp-seminar"]
SCHEDULE_URL = "https://nlp.stanford.edu/seminar/"
DETAILS_URL = "https://nlp.stanford.edu/seminar/details/greta_tuckute_2026.shtml"
RECORDED = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
EM_DASH = "\u2014"


def run(source: SourceConfig, http: Any, now: datetime = RECORDED) -> list[EventDraft]:
    adapter = get_adapter(source.adapter)
    return adapter.parse(adapter.fetch(http, source, window=make_window(now)), source)


def by_title(drafts: list[EventDraft], title: str) -> EventDraft:
    matches = [draft for draft in drafts if draft.title == title]
    assert len(matches) == 1, [draft.title for draft in drafts]
    return matches[0]


def with_params(**changes: Any) -> SourceConfig:
    return SOURCE.evolve(params={**SOURCE.params, **changes})


def heading(term: str = "Fall 2026", *, time: bool = True, venue: str = "Gates 287") -> str:
    lines = [f"NLP Seminar Schedule {EM_DASH} {term}"]
    if time:
        lines.append("Thursday <strong>11:00 am -- 12:00 noon</strong>")
    if venue:
        lines.append(f"Venue: {venue}")
    return "<h2>" + "<br>\n".join(lines) + "<br></h2>"


def row(day: str, speaker: str, title: str, link: str | None = "details/talk.shtml") -> str:
    details = f' (<a href="{link}">details</a>)' if link else ""
    return (
        f"<tr><td>{day}</td><td><a href='https://example.org/{speaker[:3]}'>{speaker}</a></td>"
        f"<td>{title}{details}</td></tr>"
    )


def schedule(*sections: tuple[str, list[str]]) -> str:
    body = "".join(
        f"{head}<table class='p'><tr><th>Date</th><th>Speaker</th><th>Title</th></tr>"
        f"{''.join(rows)}</table><h2></h2>"
        for head, rows in sections
    )
    return f"<html><body><div><table><tr><td>{body}</td></tr></table></div></body></html>"


def serve(pages: dict[str, str], now: datetime = RECORDED) -> FakeHttpClient:
    return FakeHttpClient(pages, clock=FakeClock(now))


def parse_page(text: str, source: SourceConfig = SOURCE, now: datetime = RECORDED) -> list[Any]:
    return run(source, serve({SCHEDULE_URL: text}, now), now)


# --- the recorded schedule -----------------------------------------------------------------


class TestRecordedSchedule:
    def test_the_adapter_is_registered_under_its_module_name(self) -> None:
        assert isinstance(get_adapter("nlp_seminar"), NlpSeminarAdapter)

    def test_the_configured_params_validate(self) -> None:
        params = load_params(NlpParams, SOURCE)
        assert params.max_sections == 2
        assert params.selectors.section == "table.p"

    def test_the_newest_two_terms_become_drafts(self) -> None:
        http = offline_client(SOURCE.id)
        drafts = run(SOURCE, http)
        # No talk on or after the recording day: only the schedule is requested.
        assert http.requests == [SCHEDULE_URL]
        assert len(drafts) == 16
        assert all(draft.source_id == SOURCE.id and draft.extraction == "html" for draft in drafts)
        talk = by_title(drafts, "Learning and Representing Language in Brains and LLMs")
        # Year from the heading "Spring 2026", time from "11:00 am -- 12:00 noon": wall time.
        assert talk.start == datetime(2026, 5, 28, 11, 0)
        assert talk.end == datetime(2026, 5, 28, 12, 0)
        assert [(s.name, s.affiliation, s.url) for s in talk.speakers] == [
            ("Greta Tuckute", None, "http://www.tuckute.com/")
        ]
        assert talk.location == "Zoom & Gates 287 (for in-person speakers)"
        assert talk.url == DETAILS_URL
        assert talk.abstract_html is None
        # "Jan 22nd" in the table headed "Winter 2026".
        first = by_title(drafts, "Working with Machines")
        assert first.start == datetime(2026, 2, 5, 11, 0)
        # The ordinal suffix can be wrong ("April 9nd") and the title loses "(details)".
        odd = [draft for draft in drafts if draft.start == datetime(2026, 4, 9, 11, 0)]
        assert [draft.title for draft in odd] == [
            "How and Why Speakers Code-Switch: Insights for Naturalistic Multilingual "
            "Speech Generation"
        ]

    def test_the_recorded_table_headed_with_a_wrong_year_moves_to_the_right_one(self) -> None:
        # The fourth table is headed "Winter 2024"; its dates are Thursdays of 2025.
        drafts = run(with_params(max_sections=4), offline_client(SOURCE.id))
        winter = [d.start for d in drafts if isinstance(d.start, datetime) and d.start.month <= 3]
        assert {start.year for start in winter} == {2025, 2026}
        assert min(winter) == datetime(2025, 1, 9, 11)
        assert all(start.weekday() == 3 for start in winter if start.year == 2025)

    def test_a_schedule_without_upcoming_talks_parses_to_past_talks_only(self) -> None:
        window = make_window(RECORDED)
        drafts = run(SOURCE, offline_client(SOURCE.id))
        assert drafts
        assert all(
            isinstance(d.start, datetime) and d.start < window.start.replace(tzinfo=None)
            for d in drafts
        )

    def test_the_details_page_completes_an_upcoming_talk(self) -> None:
        now = datetime(2026, 5, 22, 18, 0, tzinfo=UTC)  # after May 21, before May 28
        pages = {
            SCHEDULE_URL: (FIXTURES / "seminar-019d54b1.html").read_text(encoding="utf-8"),
            DETAILS_URL: (
                FIXTURES / "seminar-details-greta-tuckute-2026-shtml-b376d2f2.html"
            ).read_text(encoding="utf-8"),
        }
        http = serve(pages, now)
        drafts = run(SOURCE, http, now)
        assert http.requests == [SCHEDULE_URL, DETAILS_URL]
        talk = by_title(drafts, "Learning and Representing Language in Brains and LLMs")
        assert [(s.name, s.affiliation, s.url) for s in talk.speakers] == [
            (
                "Greta Tuckute",
                "Kempner Institute for Natural and Artificial Intelligence, Harvard University",
                "http://www.tuckute.com/",
            )
        ]
        assert talk.location == "Room 287, Gates Computer Science Building"
        assert (talk.start, talk.end) == (datetime(2026, 5, 28, 11), datetime(2026, 5, 28, 12))
        assert "For the first time in history" in (talk.abstract_html or "")
        assert "Research Fellow" not in (talk.abstract_html or "")  # the bio is not the abstract
        earlier = by_title(drafts, "Evaluation is Power. How can we use it well?")
        assert earlier.location == "Zoom & Gates 287 (for in-person speakers)"


# --- hand-written schedules ----------------------------------------------------------------


class TestHandWrittenSchedules:
    def test_the_weekday_of_the_heading_corrects_a_wrong_year(self) -> None:
        # Jan 23 was a Thursday in 2025, not in 2024: the heading's year is a typo.
        page = schedule((heading("Winter 2024"), [row("Jan 23rd", "Ada Lovelace", "Engines")]))
        [talk] = parse_page(page)
        assert talk.start == datetime(2025, 1, 23, 11, 0)

    def test_a_date_on_another_weekday_keeps_the_year_of_the_heading(self) -> None:
        page = schedule((heading("Fall 2026"), [row("Nov 17", "Ada Lovelace", "Engines")]))
        [talk] = parse_page(page)
        assert talk.start == datetime(2026, 11, 17, 11, 0)  # a Tuesday: a special session

    def test_talks_on_other_weekdays_keep_the_year_of_their_table(self) -> None:
        # Nov 18 is a Wednesday in 2026 and a Thursday in 2027, Nov 20 a Friday in 2026
        # and a Thursday in 2025. Most dates of the table fit 2026, so none moves, and
        # both are upcoming talks whose details pages are requested.
        rows = [
            row("Nov 5", "A B", "Thursday", "details/a"),
            row("Nov 12", "A B", "Thursday too", "details/b"),
            row("Nov 18", "A B", "Wednesday", "details/c"),
            row("Nov 20", "A B", "Friday", "details/d"),
        ]
        now = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)
        http = serve({SCHEDULE_URL: schedule((heading("Fall 2026"), rows))}, now)
        drafts = run(SOURCE, http, now)
        assert [d.start for d in drafts] == [datetime(2026, 11, d, 11) for d in (5, 12, 18, 20)]
        assert http.requests[1:] == [f"{SCHEDULE_URL}details/{name}" for name in "bcd"]

    @pytest.mark.parametrize(
        ("days", "year"),
        [
            (["Nov 5", "Nov 20"], 2026),  # one of two fits 2025: a tie keeps the heading
            (["Nov 20", "Nov 27", "Dec 4"], 2025),  # Thursdays in 2025, not in 2026
            (["Nov 19", "Nov 26", "Nov 20"], 2026),  # two Thursdays of 2026 outvote one
            (["Feb 29", "Mar 5"], 2026),  # no Feb 29 in 2026: the row is skipped
        ],
    )
    def test_the_year_is_decided_once_per_table(self, days: list[str], year: int) -> None:
        rows = [row(day, "A B", day) for day in days]
        drafts = parse_page(schedule((heading("Fall 2026"), rows)))
        assert {draft.start.year for draft in drafts} == {year}

    def test_a_year_that_most_dates_contradict_moves_the_whole_table(self) -> None:
        # Jan 9, 16 and 23 are Thursdays in 2025, Jan 25 is one in 2024.
        rows = [row(day, "A B", day) for day in ("Jan 9", "Jan 16", "Jan 23", "Jan 25")]
        drafts = parse_page(schedule((heading("Winter 2024"), rows)))
        assert [d.start for d in drafts] == [datetime(2025, 1, d, 11) for d in (9, 16, 23, 25)]

    @pytest.mark.parametrize(
        ("title", "venue", "location"),
        [
            ("CANCELLED: Engines", "Gates 287", "Gates 287"),
            ("Engines (cancelled)", "Gates 287", "Gates 287"),
            ("Engines", "Zoom &amp; Gates 287", "Zoom & Gates 287"),
            ("Engines", "Zoom only", "Zoom only"),
        ],
    )
    def test_cancelled_and_hybrid_talks_pass_their_text_through(
        self, title: str, venue: str, location: str
    ) -> None:
        # Normalization reads the status and the online part from the text.
        [talk] = parse_page(schedule((heading(venue=venue), [row("Nov 5", "A B", title)])))
        assert (talk.title, talk.location) == (title, location)
        assert (talk.status_hint, talk.is_online, talk.stream_url) == (None, None, None)

    def test_rows_that_are_notes_are_skipped(self) -> None:
        rows = [
            row("Nov 5th", "Ada Lovelace", "Engines"),
            row("Nov 12th", "", "Semester Break", link=None),
            row("", "Grace Hopper", "Compilers"),
            row("Feb 30th", "Alan Turing", "Machines"),
            row("Smarch 3", "Alan Turing", "Machines"),
        ]
        drafts = parse_page(schedule((heading(), rows)))
        assert [draft.title for draft in drafts] == ["Engines"]

    def test_a_heading_without_a_time_gives_all_day_talks(self) -> None:
        page = schedule((heading("Fall 2026", time=False, venue=""), [row("Nov 5", "A B", "T")]))
        [talk] = parse_page(page)
        assert talk.start == date(2026, 11, 5)
        assert talk.is_all_day
        assert talk.end is None
        assert talk.location is None

    def test_an_empty_newest_term_is_a_success(self) -> None:
        assert parse_page(schedule((heading("Fall 2026"), []))) == []

    def test_only_the_newest_sections_are_read(self) -> None:
        page = schedule(
            (heading("Fall 2026"), [row("Nov 5", "A B", "New")]),
            (heading("Spring 2026"), [row("May 7", "C D", "Middle")]),
            (heading("Winter 2026"), [row("Jan 8", "E F", "Old")]),
        )
        assert [draft.title for draft in parse_page(page)] == ["New", "Middle"]
        assert [d.title for d in parse_page(page, with_params(max_sections=1))] == ["New"]

    def test_entities_and_names_outside_ascii_are_passed_on(self) -> None:
        names = "Ang\u00e8le Christin and Jos\u00e9 Ruiz"
        page = schedule((heading(), [row("Nov 5", names, "Q &amp; A")]))
        [talk] = parse_page(page)
        assert talk.title == "Q & A"
        assert [(s.name, s.url) for s in talk.speakers] == [
            ("Ang\u00e8le Christin", None),
            ("Jos\u00e9 Ruiz", None),
        ]

    def test_relative_details_links_resolve_against_the_schedule(self) -> None:
        [talk] = parse_page(schedule((heading(), [row("Nov 5", "A B", "T", "details/x.shtml")])))
        assert talk.url == "https://nlp.stanford.edu/seminar/details/x.shtml"

    def test_a_page_without_any_table_is_not_understood(self) -> None:
        adapter = NlpSeminarAdapter()
        http = serve({SCHEDULE_URL: "<html><body><p>Moved</p></body></html>"})
        payload = adapter.fetch(http, SOURCE, window=make_window(RECORDED))  # fetch succeeds
        with pytest.raises(ParseError, match=r"table\.p"):
            adapter.parse(payload, SOURCE)

    def test_tables_whose_headings_name_no_year_are_not_understood(self) -> None:
        page = schedule(("<h2>Schedule</h2>", [row("Nov 5", "A B", "T")]))
        with pytest.raises(ParseError, match="names a year"):
            parse_page(page)

    def test_a_table_without_any_heading_is_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        page = schedule(
            (heading("Fall 2026"), [row("Nov 5", "A B", "Kept")]),
            ("", [row("May 7", "C D", "Lost")]),
        )
        with caplog.at_level(logging.WARNING):
            assert [draft.title for draft in parse_page(page)] == ["Kept"]
        assert "without a year" in caplog.text

    def test_a_payload_without_the_schedule_is_not_understood(self) -> None:
        adapter = NlpSeminarAdapter()
        page = schedule((heading(), []))
        payload = adapter.fetch(serve({SCHEDULE_URL: page}), SOURCE, window=make_window())
        empty = payload.evolve(documents=[payload.documents[0].evolve(label="other")])
        with pytest.raises(ParseError, match="no schedule page"):
            adapter.parse(empty, SOURCE)


# --- details pages -------------------------------------------------------------------------

DETAILS = """<html><body><div class="container content">
<h2>Reading Minds</h2>
<b><a href="/people/ada">Ada Lovelace</a></b>, <i>Analytical Engines Ltd.</i><br>
Date: 4:00pm - 5:30 pm PT, Tuesday, November 17th <br>
Venue: Room 104<br>
<h3>Abstract</h3><p>About <em>minds</em>.</p><h3>Bio</h3><p>A bio.</p>
</div></body></html>"""


class TestDetailsPages:
    NOW = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)

    def test_details_override_the_time_and_the_venue_of_the_heading(self) -> None:
        page = schedule((heading(), [row("Nov 17", "Ada Lovelace", "Minds", "details/a")]))
        http = serve({SCHEDULE_URL: page, f"{SCHEDULE_URL}details/a": DETAILS}, self.NOW)
        [talk] = run(SOURCE, http, self.NOW)
        assert http.requests == [SCHEDULE_URL, f"{SCHEDULE_URL}details/a"]
        assert (talk.start, talk.end) == (
            datetime(2026, 11, 17, 16),
            datetime(2026, 11, 17, 17, 30),
        )
        assert talk.location == "Room 104"
        assert talk.abstract_html == "About <em>minds</em>."
        assert [(s.affiliation, s.url) for s in talk.speakers] == [
            ("Analytical Engines Ltd.", "https://example.org/Ada")
        ]

    def test_a_details_page_without_lines_keeps_the_heading(self) -> None:
        page = schedule((heading(), [row("Nov 17", "", "Minds", "details/a")]))
        details = "<html><body><b><a href='/p'>Ada Lovelace</a></b></body></html>"
        http = serve({SCHEDULE_URL: page, f"{SCHEDULE_URL}details/a": details}, self.NOW)
        [talk] = run(with_params(detail_selectors={"speaker": "b > a"}), http, self.NOW)
        assert (talk.start, talk.end) == (datetime(2026, 11, 17, 11), datetime(2026, 11, 17, 12))
        assert talk.location == "Gates 287"
        assert [(s.name, s.url) for s in talk.speakers] == [
            ("Ada Lovelace", "https://nlp.stanford.edu/p")
        ]

    def test_a_failed_details_request_keeps_the_talk(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        page = schedule((heading(), [row("Nov 17", "A B", "Minds", "details/missing")]))
        http = serve({SCHEDULE_URL: page}, self.NOW)
        with caplog.at_level(logging.WARNING):
            [talk] = run(SOURCE, http, self.NOW)
        assert talk.title == "Minds"
        assert "details page skipped" in caplog.text

    def test_only_upcoming_talks_in_the_window_on_the_source_host(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        rows = [
            row("Nov 5", "A B", "Past", "details/past"),
            row("Nov 12", "A B", "Soon", "details/soon"),
            row("Nov 19", "A B", "Elsewhere", "https://other.example.org/x"),
            row("Nov 26", "A B", "Later", "details/later"),
        ]
        page = schedule((heading(), rows))
        pages = {SCHEDULE_URL: page, f"{SCHEDULE_URL}details/soon": DETAILS}
        http = serve(pages, self.NOW)
        source = with_params(max_detail_pages=1)
        with caplog.at_level(logging.WARNING):
            drafts = run(source, http, self.NOW)
        assert http.requests == [SCHEDULE_URL, f"{SCHEDULE_URL}details/soon"]
        assert len(drafts) == 4
        assert "1 details page(s) on a host the source does not name" in caplog.text

    def test_a_details_page_that_two_rows_share_is_requested_and_read_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [
            row("Nov 12", "A B", "Part one", "details/a"),
            row("Nov 19", "A B", "Part two", "details/a"),
        ]
        details = f"{SCHEDULE_URL}details/a"
        http = serve({SCHEDULE_URL: schedule((heading(), rows)), details: DETAILS}, self.NOW)
        read: list[str] = []
        original = nlp_seminar.read_details

        def counted(params: NlpParams, document: Any) -> Any:
            read.append(document.url)
            return original(params, document)

        monkeypatch.setattr(nlp_seminar, "read_details", counted)
        drafts = run(SOURCE, http, self.NOW)
        assert http.requests == [SCHEDULE_URL, details]
        assert read == [details]  # many rows may share a large page
        assert [(d.title, d.location) for d in drafts] == [
            ("Part one", "Room 104"),
            ("Part two", "Room 104"),
        ]

    def test_talks_after_the_window_get_no_details_request(self) -> None:
        page = schedule((heading("Fall 2027"), [row("Nov 4", "A B", "Far", "details/far")]))
        http = serve({SCHEDULE_URL: page}, self.NOW)
        [talk] = run(SOURCE, http, self.NOW)
        assert http.requests == [SCHEDULE_URL]
        assert talk.start == datetime(2027, 11, 4, 11, 0)

    def test_max_detail_pages_zero_requests_none(self) -> None:
        page = schedule((heading(), [row("Nov 12", "A B", "Soon", "details/soon")]))
        http = serve({SCHEDULE_URL: page}, self.NOW)
        run(with_params(max_detail_pages=0), http, self.NOW)
        assert http.requests == [SCHEDULE_URL]


# --- params --------------------------------------------------------------------------------


class TestParams:
    def test_an_unknown_key_is_an_error_that_names_it(self) -> None:
        with pytest.raises(AdapterConfigError, match=r"params\.max_pages: unknown key"):
            load_params(NlpParams, with_params(max_pages=3))

    def test_a_nested_unknown_key_is_named_too(self) -> None:
        selectors = {**SOURCE.params["selectors"], "time": "td"}
        with pytest.raises(AdapterConfigError, match=r"params.selectors.time: unknown key"):
            load_params(NlpParams, with_params(selectors=selectors))

    @pytest.mark.parametrize(
        ("key", "value", "message"),
        [
            ("date_pattern", r"(?P<month>\w+)", "date_pattern needs the group"),
            ("heading_patterns", {"term": r"\d{4}"}, "heading_patterns.term needs"),
            ("detail_patterns", {"date_line": "Date:"}, "detail_patterns.date_line needs"),
        ],
    )
    def test_patterns_need_their_groups(self, key: str, value: Any, message: str) -> None:
        with pytest.raises(AdapterConfigError, match=message):
            load_params(NlpParams, with_params(**{key: value}))

    def test_the_extra_hosts_key_is_left_to_the_host_check(self) -> None:
        assert load_params(NlpParams, with_params(extra_hosts=["example.org"]))


class TestTextLines:
    def test_lines_end_at_breaks_and_line_breaks(self) -> None:
        from selectolax.lexbor import LexborHTMLParser

        node = LexborHTMLParser("<p>a <b>b</b><br>c\nd<br><br> </p>").css_first("p")
        assert text_lines(node) == ["a b", "c", "d"]
        assert text_lines(None) == []
