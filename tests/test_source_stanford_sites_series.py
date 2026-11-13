"""Three Stanford Sites (template B) sources over their recordings: AIMI, MSE and SystemX.

Each test pins what the fixture README of the source describes: the requests, the
number of drafts and the fields of real talks. The feed and the cards give times
with their offset, so every start here is an aware datetime.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from factories import make_window
from fakes import FakeHttpClient
from seminars.config import load_sources
from seminars.http import offline_client
from seminars.models import TBA_TITLE, EventDraft, FetchedDocument
from seminars.sources.base import get_adapter
from seminars.sources.generic import rss

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
SOURCES = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
NOW = datetime(2026, 11, 12, 23, 18, 7, tzinfo=UTC)
"""The latest recording time of the fixtures, which is also the "now" of an offline run."""
PST = timezone(timedelta(hours=-8))
PDT = timezone(timedelta(hours=-7))
AIMI = "https://aimi.stanford.edu"
MSE = "https://mse.stanford.edu"
SYSTEMX = "https://systemx.stanford.edu"


def run(source_id: str) -> tuple[list[str], list[EventDraft]]:
    source = SOURCES[source_id]
    http = offline_client(source_id)
    adapter = get_adapter(source.adapter)
    drafts = adapter.parse(adapter.fetch(http, source, window=make_window(NOW)), source)
    return list(http.requests), drafts


def offset(moment: object) -> timedelta | None:
    """The offset from UTC of a datetime; ``==`` of aware datetimes compares the instant only."""
    return moment.utcoffset() if isinstance(moment, datetime) else None


def on(drafts: list[EventDraft], start: datetime) -> EventDraft:
    """The one draft that starts at ``start``, with the same offset from UTC."""
    found = [draft for draft in drafts if draft.start == start]
    assert len(found) == 1, [str(draft.start) for draft in drafts]
    assert offset(found[0].start) == start.utcoffset(), str(found[0].start)
    return found[0]


def ends(draft: EventDraft, end: datetime) -> bool:
    """True when ``draft`` ends at ``end``, with the same offset from UTC."""
    return draft.end == end and offset(draft.end) == end.utcoffset()


def people(draft: EventDraft) -> list[tuple[str, str | None]]:
    return [(speaker.name, speaker.affiliation) for speaker in draft.speakers]


def fixture_text(source_id: str, name: str) -> str:
    return (FIXTURES / source_id / name).read_text(encoding="utf-8")


def test_the_three_sources_are_enabled_with_their_default_fields() -> None:
    fields = {source_id: set(SOURCES[source_id].default_fields) for source_id in SOURCES}
    assert fields["sites-aimi"] == {"ai-ml", "med"}
    assert fields["sites-mse"] == {"chem", "physics"}
    assert fields["sites-systemx"] == {"cs"}
    for source_id in ("sites-aimi", "sites-mse", "sites-systemx"):
        source = SOURCES[source_id]
        assert source.enabled
        assert (source.group, source.adapter, source.timezone) == (
            "stanford-sites",
            "rss",
            "America/Los_Angeles",
        )


class TestAimi:
    """AIMI: series, talk title and speaker in one title; two cards point elsewhere."""

    def test_the_feed_the_list_and_the_pages_of_the_native_talks_in_the_window(self) -> None:
        requests, drafts = run("sites-aimi")
        assert requests == [
            f"{AIMI}/events/all/rss",
            f"{AIMI}/events",
            f"{AIMI}/events/ibiis-aimi-seminar-prateek-prasanna-phd",
            f"{AIMI}/events/grand-rounds/aimi-grand-rounds-tba-5",
            f"{AIMI}/events/grand-rounds/aimi-grand-rounds-genetic-diagnosis-and-discovery-"
            "enabled-large-language-models",
        ]
        assert len(drafts) == 7

    def test_the_weekly_colloquium_links_to_the_page_of_its_series(self) -> None:
        _, drafts = run("sites-aimi")
        talk = on(drafts, datetime(2026, 11, 12, 12, 0, tzinfo=PST))
        assert ends(talk, datetime(2026, 11, 12, 13, 0, tzinfo=PST))
        assert talk.title == "Responsible AI in Healthcare: From RWE to Agentic Systems"
        assert talk.series == "Computational Medicine Research Colloquium"
        assert people(talk) == [("Nigam Shah", None)]
        assert (
            talk.url
            == "https://computationalmedicine.stanford.edu/seminars-and-training/colloquium/"
        )
        assert talk.native_id == "c5a9f7f4-1218-4e7d-b7f3-42ffd5022176"
        assert talk.location is None

    def test_grand_rounds_with_speaker_abstract_and_a_webinar(self) -> None:
        _, drafts = run("sites-aimi")
        talk = on(drafts, datetime(2026, 12, 15, 8, 0, tzinfo=PST))
        assert ends(talk, datetime(2026, 12, 15, 9, 0, tzinfo=PST))
        assert talk.title == "Genetic Diagnosis and Discovery Enabled by Large Language Models"
        assert talk.series == "AIMI Grand Rounds"
        assert people(talk) == [("Konstantina Stankovic", None)]
        assert talk.location == "Zoom Webinar"
        assert talk.abstract_html is not None
        assert "Med-PaLM 2" in talk.abstract_html
        assert "Grand Rounds" in talk.categories
        assert talk.url == (
            f"{AIMI}/events/grand-rounds/aimi-grand-rounds-genetic-diagnosis-and-discovery-"
            "enabled-large-language-models"
        )

    def test_a_seminar_announced_by_its_speaker_keeps_its_title(self) -> None:
        _, drafts = run("sites-aimi")
        talk = on(drafts, datetime(2026, 11, 18, 12, 0, tzinfo=PST))
        assert talk.title == "IBIIS-AIMI Seminar: Prateek Prasanna, PhD"
        assert talk.series == "IBIIS-AIMI Seminar"
        assert people(talk) == [("Prateek Prasanna", None)]
        assert talk.location == "Hybrid: In-Person | Virtual"

    def test_a_seminar_on_another_host_names_series_and_speaker(self) -> None:
        _, drafts = run("sites-aimi")
        talk = on(drafts, datetime(2026, 11, 18, 11, 30, tzinfo=PST))
        assert talk.title == "The AI History: What to Ask, What to Build, What to Measure"
        assert talk.series == "AI4MH Seminar"
        assert people(talk) == [("Nina Vasan", None)]
        assert talk.url == "https://med.stanford.edu/psychiatry/special-initiatives/ai4mh.html"

    def test_grand_rounds_without_a_title_yet(self) -> None:
        _, drafts = run("sites-aimi")
        talk = on(drafts, datetime(2026, 11, 24, 8, 0, tzinfo=PST))
        assert talk.title == "TBA"
        assert talk.series == "AIMI Grand Rounds"
        assert talk.speakers == []

    def test_the_two_events_of_may_are_all_day_and_have_no_series(self) -> None:
        _, drafts = run("sites-aimi")
        dated = [draft for draft in drafts if not isinstance(draft.start, datetime)]
        assert [(draft.title, draft.series) for draft in dated] == [
            ("Stanford AI in Healthcare Leadership Strategy Course (HAILS)", None),
            ("AIMI Symposium + Summit Series 2027", None),
        ]

    @pytest.mark.parametrize(
        "title",
        [
            "Foundation Models: A Survey - Jane Doe, PhD",  # no series word before the colon
            "Data Science Seminar: Jane Doe, Google",  # an affiliation is no degree
            "IBIIS-AIMI Seminar: Deep Learning for MRI - Part 2",  # no degree: no speaker
            "AIMI Grand Rounds: AI in radiology - a primer",
        ],
    )
    def test_a_title_only_partly_like_the_pattern_keeps_its_parts(self, title: str) -> None:
        patterns = [re.compile(text) for text in SOURCES["sites-aimi"].params["title_patterns"]]
        first, second, _ = patterns
        assert first.search(title) is None
        assert second.search(title) is None


class TestMse:
    """MSE Colloquium: the speaker is the title; the detail page adds nothing."""

    def test_the_feed_and_the_list_only(self) -> None:
        requests, drafts = run("sites-mse")
        assert requests == [f"{MSE}/events/all/rss", f"{MSE}/events"]
        assert len(drafts) == 4

    def test_a_colloquium(self) -> None:
        _, drafts = run("sites-mse")
        talk = on(drafts, datetime(2026, 11, 18, 11, 30, tzinfo=PST))
        assert ends(talk, datetime(2026, 11, 18, 12, 30, tzinfo=PST))
        assert talk.title == "MSE Colloquium: Robert Hovden"
        assert talk.series == "MSE Colloquium"
        assert people(talk) == [("Robert Hovden", None)]
        assert talk.event_type_hint == "colloquium"
        assert talk.location == (
            "Stanford University, Mechanical Engineering Building, 530-127, "
            "440 Escondido Mall Stanford, Stanford, CA 94305, United States"
        )
        assert talk.native_id == "e6b761bd-76ed-4b32-b2c8-317aac29c65b"
        assert talk.url == f"{MSE}/events/mse-colloquium/mse-colloquium-robert-hovden"

    def test_every_colloquium_of_the_sample(self) -> None:
        _, drafts = run("sites-mse")
        colloquia = [draft for draft in drafts if draft.series == "MSE Colloquium"]
        assert [(draft.start, people(draft)) for draft in colloquia] == [
            (datetime(2026, 11, 18, 11, 30, tzinfo=PST), [("Robert Hovden", None)]),
            (datetime(2026, 12, 2, 11, 30, tzinfo=PST), [("Yanjie Shao", None)]),
            (datetime(2027, 1, 13, 11, 30, tzinfo=PST), [("Max Saccone", None)]),
        ]
        assert {offset(draft.start) for draft in colloquia} == {timedelta(hours=-8)}

    def test_the_distinguished_lecture_in_summer_time(self) -> None:
        _, drafts = run("sites-mse")
        talk = on(drafts, datetime(2027, 4, 2, 8, 30, tzinfo=PDT))
        assert talk.title == "William D. Nix Lecture 2027"
        assert talk.event_type_hint == "lecture"
        assert talk.abstract == "Distinguished Speaker Subra Suresh"
        assert talk.series is None

    def test_a_colloquium_whose_speaker_is_not_known_yet_has_no_title_and_nobody(self) -> None:
        source = SOURCES["sites-mse"]
        title = "MSE Colloquium: Yanjie Shao"
        http = FakeHttpClient()
        for url, name, kind in (
            (f"{MSE}/events/all/rss", "events-all-rss.xml", "application/rss+xml"),
            (f"{MSE}/events", "events.html", "text/html"),
        ):
            text = fixture_text("sites-mse", name)
            assert text.count(title) == 1
            http.add(url, text.replace(title, "MSE Colloquium: Speaker TBA"), content_type=kind)
        adapter = get_adapter(source.adapter)
        drafts = adapter.parse(adapter.fetch(http, source, window=make_window(NOW)), source)
        talk = on(drafts, datetime(2026, 12, 2, 11, 30, tzinfo=PST))
        assert talk.title == TBA_TITLE
        assert talk.series == "MSE Colloquium"
        assert talk.speakers == []

    def test_the_page_of_a_colloquium_has_nothing_the_card_lacks(self) -> None:
        params = rss.RssParams.model_validate(
            {
                **SOURCES["sites-mse"].params,
                "detail_page": SOURCES["sites-chemh"].params["detail_page"],
            }
        )
        url = f"{MSE}/events/mse-colloquium/mse-colloquium-robert-hovden"
        document = FetchedDocument(
            url=url,
            final_url=url,
            status=200,
            content_type="text/html",
            text=fixture_text("sites-mse", "detail-native.html"),
            fetched_at=NOW,
        )
        detail = rss.read_detail(params.detail_page, document, NOW.date())
        assert detail.body is None
        assert detail.texts["title"] == "MSE Colloquium: Robert Hovden"
        assert detail.end == datetime(2026, 11, 18, 20, 30, tzinfo=UTC)
        assert offset(detail.end) == timedelta(0)  # Unix seconds of the page: UTC
        assert SOURCES["sites-mse"].params["detail_page"] == {"fetch": "never"}


class TestSystemx:
    """SystemX: talk titles, the speaker only in the text of the detail page."""

    def test_the_feed_the_list_and_both_detail_pages(self) -> None:
        requests, drafts = run("sites-systemx")
        assert requests == [
            f"{SYSTEMX}/events/all/rss",
            f"{SYSTEMX}/events",
            f"{SYSTEMX}/events/seminars/computing-encrypted-data-concept-reality",
            f"{SYSTEMX}/events/workshop/systemx-physical-intelligence-summit",
        ]
        # The list page also shows 20 past events; only the 2 upcoming ones are drafts.
        assert len(drafts) == 2

    def test_the_seminar(self) -> None:
        _, drafts = run("sites-systemx")
        talk = on(drafts, datetime(2026, 11, 12, 16, 30, tzinfo=PST))
        assert ends(talk, datetime(2026, 11, 12, 17, 30, tzinfo=PST))
        assert talk.title == "Computing on encrypted data: from concept to reality"
        assert talk.event_type_hint == "seminar"
        assert talk.location == "STLC 114, United States"
        assert talk.abstract_html is not None
        assert "fully homomorphic encryption" in talk.abstract_html
        assert "Ingrid Verbauwhede" in talk.abstract_html
        # The template has no speaker field; the first sentence under "Speaker Bio:"
        # ("Dr. Ir. Ingrid Verbauwhede is a Professor ... at KU Leuven.") names her.
        assert people(talk) == [("Ingrid Verbauwhede", "KU Leuven")]

    def test_the_summit_is_a_workshop_of_one_day(self) -> None:
        _, drafts = run("sites-systemx")
        talk = on(drafts, datetime(2026, 12, 3, 9, 0, tzinfo=PST))
        assert ends(talk, datetime(2026, 12, 3, 18, 0, tzinfo=PST))
        assert talk.title == "SystemX Physical Intelligence Summit"
        assert talk.event_type_hint == "workshop"
        assert talk.speakers == []  # its page has a programme, no "Speaker Bio:"
        assert (
            talk.location
            == "Frances C. Arrillaga Alumni Center, Stanford University, United States"
        )
