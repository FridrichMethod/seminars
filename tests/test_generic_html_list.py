"""The generic ``html_list`` adapter: recorded list pages and hand-written edge cases."""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from factories import make_source, make_window
from fakes import FakeHttpClient
from seminars.config import load_sources
from seminars.http import offline_client
from seminars.models import EventDraft, SourceConfig
from seminars.sources.base import ForeignHostError, ParseError, get_adapter
from seminars.sources.generic import html_list
from seminars.sources.generic.html_list import (
    AdapterConfigError,
    HtmlListAdapter,
    parse_clock,
    parse_day,
    parse_moment,
    split_names,
    split_range,
    time_range,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
WINDOW = make_window(NOW)
PST = timezone(timedelta(hours=-8))
SOURCES = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
HTML_LIST_SOURCES = sorted(sid for sid, source in SOURCES.items() if source.adapter == "html_list")


def run(source: SourceConfig, http: Any = None) -> list[EventDraft]:
    adapter = get_adapter(source.adapter)
    client = http if http is not None else offline_client(source.id)
    return adapter.parse(adapter.fetch(client, source, window=WINDOW), source)


def by_title(drafts: list[EventDraft], title: str) -> EventDraft:
    matches = [draft for draft in drafts if draft.title == title]
    assert len(matches) == 1, [draft.title for draft in drafts]
    return matches[0]


def with_params(source: SourceConfig, **changes: Any) -> SourceConfig:
    params = {
        key: value for key, value in {**source.params, **changes}.items() if value is not None
    }
    return source.evolve(params=params)


# --- recorded sources ----------------------------------------------------------------------


class TestRecordedSources:
    def test_the_adapter_is_registered_under_its_module_name(self) -> None:
        assert isinstance(get_adapter("html_list"), HtmlListAdapter)

    @pytest.mark.parametrize("source_id", HTML_LIST_SOURCES)
    def test_every_source_of_the_adapter_validates_its_params(self, source_id: str) -> None:
        source = SOURCES[source_id]
        assert html_list.load_params(html_list.HtmlListParams, source).selectors.item

    @pytest.mark.parametrize(
        "source_id", [sid for sid in HTML_LIST_SOURCES if SOURCES[sid].enabled]
    )
    def test_every_enabled_source_yields_drafts_from_its_fixtures(self, source_id: str) -> None:
        drafts = run(SOURCES[source_id])
        assert drafts
        assert all(draft.source_id == source_id and draft.extraction == "html" for draft in drafts)

    def test_rain_seminar(self) -> None:
        http = offline_client("custom-rain-seminar")
        drafts = run(SOURCES["custom-rain-seminar"], http)
        assert http.requests == ["https://rain.stanford.edu/"]
        assert len(drafts) == 11
        talk = by_title(drafts, "Inference Auctions")
        # "4:30-5:30 PM PT": one PM for both times, naive wall time of the source.
        assert talk.start == datetime(2026, 11, 17, 16, 30)
        assert talk.end == datetime(2026, 11, 17, 17, 30)
        assert [(s.name, s.affiliation) for s in talk.speakers] == [
            ("Keegan Harris", "University of California, Berkeley")
        ]
        assert talk.location == "Y2E2 299"
        assert talk.url is None  # no page per talk: identity falls back to start and speaker
        assert "When inference demand exceeds" in (talk.abstract_html or "")
        assert "Talk details" not in (talk.abstract_html or "")
        untitled = [draft for draft in drafts if draft.start == datetime(2026, 12, 1, 16, 30)]
        assert [(draft.title, draft.speakers[0].name) for draft in untitled] == [
            (None, "Susan Murphy")
        ]

    def test_theory_lunch(self) -> None:
        drafts = run(SOURCES["custom-theory-lunch"])
        assert len(drafts) == 11
        talk = by_title(drafts, "Low-error extractors")
        assert (talk.start, talk.end) == (datetime(2026, 11, 12, 12), datetime(2026, 11, 12, 13))
        assert [(s.name, s.affiliation) for s in talk.speakers] == [
            ("Jesse Goodman", "UC Berkeley")
        ]
        assert talk.location == "Gates 403"
        assert "two-source extractors" in (talk.abstract_html or "")
        pairs = [d for d in drafts if len(d.speakers) == 2]
        assert [(s.name, s.affiliation) for s in pairs[0].speakers] == [
            ("Rahul Saha", "UT Austin"),
            ("Alan Li", "UT Austin"),
        ]

    def test_mlsys_rows_without_a_time_are_all_day(self) -> None:
        drafts = run(SOURCES["custom-mlsys-seminar"])
        assert len(drafts) == 87  # 88 rows, "Thanksgiving" has no title and no date
        talk = by_title(drafts, "Coding Agents")
        assert talk.start == date(2025, 10, 13)
        assert talk.is_all_day
        assert talk.end is None
        assert [(s.name, s.affiliation) for s in talk.speakers] == [("Beyang Liu", "Sourcegraph")]
        assert talk.location is None
        assert talk.stream_url == "https://youtu.be/hR6zyA-EUPo"
        names = {s.name for draft in drafts for s in draft.speakers}
        assert {"Xinyu Hu", "Olcay Cirit"} <= names

    def test_biohub_takes_times_and_place_from_the_detail_page(self) -> None:
        http = offline_client("bayarea-biohub")
        drafts = run(SOURCES["bayarea-biohub"], http)
        detail = "https://biohub.org/events/webinar-esmfold2-structure-prediction-long-context/"
        assert http.requests == ["https://biohub.org/events/", detail]
        [webinar] = drafts
        assert webinar.title == "Long-Context Structure Prediction with ESMFold2 Webinar"
        assert webinar.start == datetime(2026, 11, 18, 13, 0, tzinfo=PST)
        assert webinar.end == datetime(2026, 11, 18, 14, 0, tzinfo=PST)
        assert webinar.location == "Zoom"
        assert webinar.native_id == "52744"
        assert webinar.url == detail
        assert webinar.event_type_hint == "seminar"
        assert webinar.speakers == []
        assert (webinar.abstract or "").startswith("Watch our webinar on long-context")

    def test_suncat_rows_and_their_tags(self) -> None:
        source = SOURCES["bayarea-suncat"]
        detail = {**source.params["detail_page"], "max_pages": 2}
        http = offline_client(source.id)
        kept = run(with_params(source, detail_page=detail), http)
        # Both recorded detail pages carry the tag "Group meetings", not "Seminars".
        assert kept == []
        assert http.requests[1:] == [
            "https://suncat.stanford.edu/events/dr-hyeonjung-jung",
            "https://suncat.stanford.edu/events/dr-ruchika-mahajan",
        ]
        drafts = run(with_params(source, detail_page=detail, keep_tags=None))
        talk = by_title(drafts, "Dr. Hyeonjung Jung")
        assert (talk.start, talk.end) == (
            datetime(2026, 11, 20, 10),
            datetime(2026, 11, 20, 11),
        )
        assert talk.location == "Sycamore Conference room, building 40 room 195"
        assert talk.categories == ["Group meetings"]
        assert talk.url == "https://suncat.stanford.edu/events/dr-hyeonjung-jung"


# --- hand-written pages --------------------------------------------------------------------

LIST_URL = "https://talks.example.edu/seminar/"


def page(*rows: str) -> str:
    return "<html><body><ul class='talks'>" + "".join(rows) + "</ul></body></html>"


def slug(title: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", title.lower())) or "x"


def talk_url(title: str) -> str:
    return f"https://talks.example.edu/talks/{slug(title)}"


def row(
    title: str = "A talk",
    day: str = "2026-11-17",
    clock: str | None = "4:30 - 5:30pm PT",
    speaker: str | None = "Jane Doe (MIT)",
    extra: str = "",
) -> str:
    parts = [f"<h3><a href='/talks/{slug(title)}'>{title}</a></h3>"]
    parts.append(f"<span class='date'>{day}</span>")
    if clock is not None:
        parts.append(f"<span class='time'>{clock}</span>")
    if speaker is not None:
        parts.append(f"<p class='who'>{speaker}</p>")
    return "<li>" + "".join(parts) + extra + "</li>"


def list_source(**params: Any) -> SourceConfig:
    base: dict[str, Any] = {
        "selectors": {
            "item": "ul.talks > li",
            "title": "h3",
            "link": "h3 a",
            "date": ".date",
            "time": ".time",
            "speaker": ".who",
        },
        "link_attribute": "href",
        "speaker_pattern": r"^(?P<speaker>.+?)(?:\s*\((?P<affiliation>[^()]*)\))?$",
    }
    merged = {key: value for key, value in {**base, **params}.items() if value is not None}
    return make_source(
        id="custom-example",
        group="stanford-custom",
        adapter="html_list",
        url=LIST_URL,
        params=merged,
    )


def serve(text: str, url: str = LIST_URL, **pages: str) -> FakeHttpClient:
    http = FakeHttpClient()
    http.add(url, text)
    for target, body in pages.items():
        http.add(target, body)
    return http


class TestRows:
    def test_a_row_becomes_a_draft(self) -> None:
        [draft] = run(list_source(), serve(page(row())))
        assert draft.title == "A talk"
        assert draft.start == datetime(2026, 11, 17, 16, 30)
        assert draft.end == datetime(2026, 11, 17, 17, 30)
        assert draft.speakers[0].name == "Jane Doe"
        assert draft.speakers[0].affiliation == "MIT"
        assert (draft.url or "").startswith("https://talks.example.edu/talks/")
        assert draft.native_id is None

    def test_entities_and_names_outside_ascii(self) -> None:
        text = page(
            row(title="Q&amp;A on L&eacute;vy flights", speaker="Ang\u00e8le Christin (ENS)")
        )
        [draft] = run(list_source(), serve(text))
        assert draft.title == "Q&A on L\u00e9vy flights"
        assert draft.speakers[0].name == "Ang\u00e8le Christin"

    def test_a_malformed_row_is_skipped_and_the_others_kept(self) -> None:
        text = page(row(title="Good"), row(title="No date", day="sometime soon"))
        drafts = run(list_source(require=["title"]), serve(text))
        assert [draft.title for draft in drafts] == ["Good"]

    def test_a_row_at_the_end_of_the_calendar_does_not_fail_the_page(self) -> None:
        text = page(row(title="Good"), row(title="Far", day="9999-12-31", clock="11pm - 1am"))
        http = serve(text)
        source = list_source(detail_page={"times": "time"})
        get_adapter("html_list").fetch(http, source, window=WINDOW)
        assert http.requests == [LIST_URL, talk_url("Good")]  # "Far" is outside the window
        good, far = run(list_source(), serve(text))
        assert (good.title, far.title) == ("Good", "Far")
        assert (far.start, far.end) == (datetime(9999, 12, 31, 23, 0), None)

    def test_a_draft_the_model_rejects_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def speakers(row: Any) -> list[Any]:
            return [{"name": ""}] if row.get("title") == "Bad" else []

        monkeypatch.setattr(html_list, "row_speakers", speakers)
        with caplog.at_level(logging.WARNING, logger=html_list.__name__):
            drafts = run(list_source(), serve(page(row(title="Bad"), row(title="Good"))))
        assert [draft.title for draft in drafts] == ["Good"]
        assert "a malformed row was skipped (ValidationError)" in caplog.text

    def test_a_row_without_a_time_is_an_all_day_talk(self) -> None:
        [draft] = run(list_source(), serve(page(row(clock=None))))
        assert draft.start == date(2026, 11, 17)
        assert draft.is_all_day
        assert draft.end is None

    def test_tba_as_time_means_no_time(self) -> None:
        [draft] = run(list_source(), serve(page(row(clock="TBA"))))
        assert draft.start == date(2026, 11, 17)

    def test_default_times_fill_rows_without_one(self) -> None:
        source = list_source(default_start_time="12:00", default_end_time="13:00")
        [draft] = run(source, serve(page(row(clock=None))))
        assert (draft.start, draft.end) == (datetime(2026, 11, 17, 12), datetime(2026, 11, 17, 13))

    def test_a_date_range_in_one_string(self) -> None:
        source = list_source(
            date_pattern=r"^(?P<date>\w+ \d+, \d{4}) to (?P<end_date>\w+ \d+, \d{4})$",
            date_format="%B %d, %Y",
        )
        text = page(row(day="November 17, 2026 to November 19, 2026", clock=None))
        [draft] = run(source, serve(text))
        assert (draft.start, draft.end) == (date(2026, 11, 17), date(2026, 11, 19))
        assert draft.is_all_day

    def test_a_multi_day_talk_with_times(self) -> None:
        source = list_source(
            selectors={"item": "ul.talks > li", "title": "h3", "date": ".date"},
            date_pattern=(
                r"^(?P<date>\w+ \d+, \d{4}) - (?P<start>\d+:\d\d[ap]m) - "
                r"(?P<end_date>\w+ \d+, \d{4}) - (?P<end>\d+:\d\d[ap]m)$"
            ),
            date_format="%B %d, %Y",
            time_format="%I:%M%p",
            speaker_pattern=None,
            link_attribute=None,
        )
        text = page(row(day="August 2, 2027 - 10:00am - August 6, 2027 - 02:00pm", clock=None))
        [draft] = run(source, serve(text))
        assert (draft.start, draft.end) == (datetime(2027, 8, 2, 10), datetime(2027, 8, 6, 14))

    def test_a_row_with_one_time_has_no_end(self) -> None:
        [draft] = run(list_source(), serve(page(row(clock="4:30pm PT"))))
        assert (draft.start, draft.end) == (datetime(2026, 11, 17, 16, 30), None)

    def test_a_time_range_that_crosses_midnight(self) -> None:
        [draft] = run(list_source(), serve(page(row(clock="11pm - 1am"))))
        assert draft.end == datetime(2026, 11, 18, 1)

    @pytest.mark.parametrize(
        ("clock", "start", "end"),
        [
            ("Noon - 1:30", datetime(2026, 11, 17, 12), datetime(2026, 11, 17, 13, 30)),
            ("11pm - 5am", datetime(2026, 11, 17, 23), datetime(2026, 11, 18, 5)),
            ("11pm - 5:30am", datetime(2026, 11, 17, 23), None),
            ("4:30 PM - 3:30", datetime(2026, 11, 17, 16, 30), None),
            ("4:30 - 4:30", datetime(2026, 11, 17, 4, 30), None),
        ],
    )
    def test_an_end_before_the_start_ends_at_most_six_hours_later(
        self, clock: str, start: datetime, end: datetime | None
    ) -> None:
        [draft] = run(list_source(), serve(page(row(clock=clock))))
        assert (draft.start, draft.end) == (start, end)

    def test_an_end_before_the_start_of_a_multi_day_row_is_dropped(self) -> None:
        source = list_source(date_pattern=r"^(?P<date>\S+) to (?P<end_date>\S+)$")
        [draft] = run(source, serve(page(row(day="2026-11-18 to 2026-11-17", clock="1pm - 3pm"))))
        assert (draft.start, draft.end) == (datetime(2026, 11, 18, 13), None)

    def test_exclusion_keys(self) -> None:
        source = list_source(
            selectors={
                "item": "ul.talks > li",
                "title": "h3",
                "date": ".date",
                "region": ".region",
                "summary": ".teaser",
            },
            speaker_pattern=None,
            link_attribute=None,
            skip_title_patterns=[r"\(Invite Only\)"],
            skip_summary_patterns=["will be at booth"],
            exclude_regions=["Chicago"],
            require=["title", "date"],
        )
        text = page(
            row(title="Kept", extra="<p class='teaser'>A talk about cells.</p>"),
            row(title="Symposium (Invite Only)"),
            row(title="Booth", extra="<p class='teaser'>We will be at booth 12</p>"),
            row(title="Far away", extra="<span class='region'> Chicago </span>"),
            row(title="", day="2026-11-20"),
        )
        [draft] = run(source, serve(text))
        assert draft.title == "Kept"
        assert draft.abstract == "A talk about cells."

    def test_title_event_types(self) -> None:
        source = list_source(title_event_types={"Webinar": "seminar", "Workshop": "workshop"})
        drafts = run(source, serve(page(row(title="Spring Workshop day"), row(title="Plain"))))
        assert [draft.event_type_hint for draft in drafts] == ["workshop", None]

    def test_links_resolve_against_the_final_url_of_the_list(self) -> None:
        http = FakeHttpClient()
        http.add(LIST_URL, page(row()), final_url="https://talks.example.edu/new/list")
        [draft] = run(list_source(), http)
        assert (draft.url or "").startswith("https://talks.example.edu/talks/")

    def test_an_item_selector_that_matches_nothing_is_a_parse_error(self) -> None:
        with pytest.raises(ParseError, match=r"selectors\.item .* matches nothing"):
            run(list_source(), serve("<html><body><p>Redesigned</p></body></html>"))

    def test_items_without_the_required_fields_are_a_parse_error(self) -> None:
        with pytest.raises(ParseError, match=r"no item .* has all of: title, date"):
            run(list_source(require=["title", "date"]), serve(page(row(title="", day=""))))

    def test_a_payload_without_list_page_is_a_parse_error(self) -> None:
        source = list_source()
        payload = get_adapter("html_list").fetch(serve(page(row())), source, window=WINDOW)
        empty = payload.evolve(documents=[d.evolve(label="other") for d in payload.documents])
        with pytest.raises(ParseError, match="no list page"):
            get_adapter("html_list").parse(empty, source)


class TestDetailPages:
    DETAIL = (
        "<html><head><link rel='shortlink' href='https://talks.example.edu/?p={pid}'></head>"
        "<body><div class='meta'><time datetime='2026-11-18T13:00:00-08:00'>1pm</time>"
        "<time datetime='2026-11-18T14:00:00-08:00'>2pm</time></div>"
        "<p class='place'>Room 1<br>Main Street</p><span class='tag'>{tag}</span></body></html>"
    )

    def source(self, **params: Any) -> SourceConfig:
        detail = {
            "times": ".meta time[datetime]",
            "location": ".place",
            "native_id": "link[rel='shortlink']",
            "native_id_attribute": "href",
            "native_id_pattern": r"[?&]p=(?P<native_id>\d+)",
            "tag": ".tag",
        }
        return list_source(detail_page=detail, **params)

    def test_detail_pages_complete_the_rows(self) -> None:
        text = page(row(title="One"), row(title="Two"))
        http = FakeHttpClient()
        http.add(LIST_URL, text)
        urls = [talk_url(t) for t in ("One", "Two")]
        http.add(urls[0], self.DETAIL.format(pid=7, tag="Seminars"))
        drafts = run(self.source(), http)
        assert http.requests == [LIST_URL, *urls]  # the second page fails: its row is dropped
        [draft] = drafts
        assert draft.start == datetime(2026, 11, 18, 13, tzinfo=PST)
        assert draft.location == "Room 1, Main Street"
        assert draft.native_id == "7"
        assert draft.categories == ["Seminars"]

    def test_keep_tags(self) -> None:
        url = talk_url("One")
        http = serve(page(row(title="One")), **{url: self.DETAIL.format(pid=1, tag="Social")})
        assert run(self.source(keep_tags=["Seminars"]), http) == []

    def test_links_to_other_hosts_are_never_fetched(self, caplog: pytest.LogCaptureFixture) -> None:
        text = page(row(title="Here"), row(title="Elsewhere"), row(title="Old", day="2020-01-01"))
        for title in ("Elsewhere", "Old"):
            text = text.replace(f"/talks/{slug(title)}", f"https://other.org/{slug(title)}")
        here = talk_url("Here")
        http = serve(text, **{here: self.DETAIL.format(pid=1, tag="x")})
        with caplog.at_level(logging.WARNING, logger=html_list.__name__):
            drafts = run(self.source(), http)
        assert http.requests == [LIST_URL, here]
        assert [draft.title for draft in drafts] == ["Here"]
        # Only rows in the window count; the dropped rows are reported, not silent.
        assert [record.getMessage() for record in caplog.records] == [
            "source custom-example: 1 row(s) in the window dropped: their detail page is on "
            "a host the source does not name (params.extra_hosts)"
        ]

    def test_only_foreign_detail_links_fetch_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        text = page(row(title="Elsewhere"))
        text = text.replace(f"/talks/{slug('Elsewhere')}", "https://other.org/x")
        http = serve(text)
        with caplog.at_level(logging.WARNING, logger=html_list.__name__):
            assert run(self.source(), http) == []
        assert http.requests == [LIST_URL]
        assert "1 row(s) in the window dropped" in caplog.text

    def test_a_list_page_redirected_to_another_host_raises(self) -> None:
        http = FakeHttpClient()
        http.add(LIST_URL, page(row()), final_url="https://www.talks.example.edu/seminar/")
        with pytest.raises(ForeignHostError, match=r"www\.talks\.example\.edu"):
            run(self.source(), http)
        assert http.requests == [LIST_URL]

    def test_rows_outside_the_window_and_beyond_the_cap_are_not_fetched(self) -> None:
        detail = {"times": "time", "max_pages": 1}
        source = list_source(detail_page=detail)
        text = page(row(title="Old", day="2020-01-01"), row(title="A"), row(title="B"))
        http = serve(text)
        get_adapter("html_list").fetch(http, source, window=WINDOW)
        assert http.requests == [LIST_URL, talk_url("A")]


BARE: dict[str, Any] = {"speaker_pattern": None, "link_attribute": None}


class TestParams:
    def test_an_unknown_key_is_named(self) -> None:
        with pytest.raises(AdapterConfigError, match=r"params\.colour: unknown key"):
            run(list_source(colour="red"), serve(page(row())))

    def test_an_unknown_selector_key_is_named(self) -> None:
        source = list_source(selectors={"item": "li", "date": ".date", "venue": ".v"})
        with pytest.raises(AdapterConfigError, match=r"params\.selectors\.venue: unknown key"):
            run(source, serve(page(row())))

    def test_extra_hosts_is_left_to_the_host_check(self) -> None:
        assert run(list_source(extra_hosts=["cdn.example.edu"]), serve(page(row())))

    @pytest.mark.parametrize(
        ("params", "message"),
        [
            ({"location_pattern": "x"}, "location_pattern needs selectors.location"),
            ({"require": ["abstract"]}, "require names fields without a selector"),
            ({"date_pattern": "(?P<when>x)"}, "unknown groups: when"),
            ({"date_pattern": "("}, "valid regular expression"),
            ({"selectors": {"item": "li[", "date": ".d"}, **BARE}, "invalid CSS selector"),
            ({"selectors": {"item": "li"}, **BARE}, "selectors.date or detail_page.times"),
            ({"patterns": {}}, "write <field>_pattern"),
            ({"title_event_types": {"Talk": "party"}}, "title_event_types"),
        ],
    )
    def test_bad_params(self, params: dict[str, Any], message: str) -> None:
        with pytest.raises(AdapterConfigError, match=message):
            html_list.load_params(html_list.HtmlListParams, list_source(**params))


# --- helpers -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4:30 - 5:30pm PT", (time(16, 30), time(17, 30))),
        ("4:30\u20135:30 PM", (time(16, 30), time(17, 30))),
        ("11:30 - 1:00 PM", (time(11, 30), time(13, 0))),
        ("noon to 1pm", (time(12, 0), time(13, 0))),
        ("11:00 am - 12:00 noon", (time(11, 0), time(12, 0))),
        ("12:00 noon", (time(12, 0), None)),
        ("12 Noon - 1:15 pm PT", (time(12, 0), time(13, 15))),
        ("Noon - 1:30", (time(12, 0), time(13, 30))),
        ("12 noon - 1:30", (time(12, 0), time(13, 30))),
        ("11am - 1:30", (time(11, 0), time(13, 30))),
        ("4:30pm - 5:30", (time(16, 30), time(17, 30))),
        ("9am - 10:30", (time(9, 0), time(10, 30))),
        ("9:00 AM - 17:00", (time(9, 0), time(17, 0))),
        ("4:30 PM - 3:30", (time(16, 30), time(3, 30))),
        ("11pm - 1", (time(23, 0), time(1, 0))),
        ("11am - 0:30", (time(11, 0), time(0, 30))),
        ("noon PT", (time(12, 0), None)),
        ("11:30 noon", (None, None)),
        ("16:00-17:30", (time(16, 0), time(17, 30))),
        ("4 p.m.", (time(16, 0), None)),
        ("10:00am-11:00am (PT)", (time(10, 0), time(11, 0))),
        ("midnight", (time(0, 0), None)),
        ("TBA", (None, None)),
        ("25:00", (None, None)),
        ("13pm", (None, None)),
        ("", (None, None)),
    ],
)
def test_time_ranges(text: str, expected: tuple[time | None, time | None]) -> None:
    assert time_range(*split_range(text)) == expected


def test_a_shared_meridiem_group() -> None:
    assert time_range("4:30", "5:30", meridiem="PM") == (time(16, 30), time(17, 30))
    assert time_range(None, "5:30", meridiem="pm") == (None, time(17, 30))


def test_parse_clock_with_a_format() -> None:
    assert parse_clock("02:00pm", "%I:%M%p") == (time(14, 0), "pm")
    assert parse_clock("14:00", "%H:%M") == (time(14, 0), None)
    assert parse_clock("later", "%H:%M") is None


@pytest.mark.parametrize(
    ("text", "fmt", "expected"),
    [
        ("2026-11-17", None, date(2026, 11, 17)),
        ("November 18, 2026", None, date(2026, 11, 18)),
        ("10/20/25", "%m/%d/%y", date(2025, 10, 20)),
        ("January 5", "%B %d", date(2027, 1, 5)),
        ("February 29", "%B %d", None),
        ("May 1", "%B %d", date(2027, 5, 1)),
        ("June 1", "%B %d", date(2026, 6, 1)),
        ("January 12", None, date(2027, 1, 12)),
        ("Tuesday, June 2", None, date(2026, 6, 2)),
        ("Tuesday, September 22, 2026", None, date(2026, 9, 22)),
        ("Jan 12, 2026", None, date(2026, 1, 12)),
        ("February 29", None, None),
        ("not a date", None, None),
        ("13/45/25", "%m/%d/%y", None),
        ("TBD", None, None),
    ],
)
def test_parse_day(text: str, fmt: str | None, expected: date | None) -> None:
    assert parse_day(text, fmt, date(2026, 11, 11)) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-17", date(2026, 11, 17)),
        ("20261117", date(2026, 11, 17)),
        (" 2026-11-17 ", date(2026, 11, 17)),
        ("2026-11-17T16:00", datetime(2026, 11, 17, 16, 0)),
        ("2026-11-17 16:00:00-08:00", datetime(2026, 11, 17, 16, 0, tzinfo=PST)),
        ("20261117T160000Z", datetime(2026, 11, 17, 16, 0, tzinfo=UTC)),
        ("1731859200", None),
        ("Nov 17, 2026", None),
        (None, None),
    ],
)
def test_parse_moment(text: str | None, expected: object) -> None:
    moment = parse_moment(text)
    assert moment == expected
    assert type(moment) is type(expected)  # a date is never a midnight datetime


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("Jane Doe", ["Jane Doe"]),
        ("Rahul Saha and Alan Li", ["Rahul Saha", "Alan Li"]),
        ("A One, B Two, and C Three", ["A One", "B Two", "C Three"]),
        ("A One & B Two", ["A One", "B Two"]),
        ("Jane Doe, Jr.", ["Jane Doe, Jr."]),
        ("  ", []),
    ],
)
def test_split_names(text: str, names: list[str]) -> None:
    assert split_names(text) == names
