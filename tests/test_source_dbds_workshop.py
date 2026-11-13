"""The Workshop in Biomedical Data Science (html_list) over its recorded page.

The page gives a date, a speaker and a title per talk and states the time of
every talk once, in its heading; the source entry supplies it as default times.
Times are naive wall times of the source until normalization applies its zone.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from factories import make_window
from fakes import FakeHttpClient
from seminars.config import load_sources
from seminars.http import offline_client
from seminars.models import EventDraft
from seminars.sources.base import get_adapter

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCES = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
SOURCE_ID = "custom-dbds-workshop"
PAGE = "https://dbds.stanford.edu/weekly-seminar/"
NOW = datetime(2026, 11, 12, 23, 18, 7, tzinfo=UTC)
"""The latest recording time of the fixtures, which is also the "now" of an offline run."""
RECORDING = REPO_ROOT / "tests" / "fixtures" / SOURCE_ID / "weekly-seminar.html"
ROOM = "LKSC 120 (Li Ka Shing Center)"
ZITNIK = (
    "<strong>Date:</strong> November 19, 2026</span><br />\n"
    '<span style="font-size: 18pt;"><strong>Speaker:</strong> Marinka Zitnik</span><br />\n'
    '<span style="font-size: 18pt;"><strong>Title: </strong>Coming soon</span>'
)
"""The markup of one recorded row, which the tests below write in other ways."""


def run() -> tuple[list[str], list[EventDraft]]:
    source = SOURCES[SOURCE_ID]
    http = offline_client(SOURCE_ID)
    adapter = get_adapter(source.adapter)
    drafts = adapter.parse(adapter.fetch(http, source, window=make_window(NOW)), source)
    return list(http.requests), drafts


def run_with_row(markup: str) -> list[EventDraft]:
    """The drafts of the recorded page with the row of Marinka Zitnik written as ``markup``."""
    text = RECORDING.read_text(encoding="utf-8")
    assert text.count(ZITNIK) == 1
    http = FakeHttpClient()
    http.add(PAGE, text.replace(ZITNIK, markup))
    source = SOURCES[SOURCE_ID]
    adapter = get_adapter(source.adapter)
    return adapter.parse(adapter.fetch(http, source, window=make_window(NOW)), source)


def on(drafts: list[EventDraft], day: date) -> EventDraft:
    found = [d for d in drafts if isinstance(d.start, datetime) and d.start.date() == day]
    assert len(found) == 1, [str(draft.start) for draft in drafts]
    return found[0]


def people(draft: EventDraft) -> list[tuple[str, str | None]]:
    return [(speaker.name, speaker.affiliation) for speaker in draft.speakers]


def test_the_source_entry() -> None:
    source = SOURCES[SOURCE_ID]
    assert source.enabled
    assert (source.group, source.adapter, source.url) == ("stanford-custom", "html_list", PAGE)
    assert set(source.default_fields) == {"ai-ml", "math", "med"}
    assert source.series == "Workshop in Biomedical Data Science"
    assert source.timezone == "America/Los_Angeles"


def test_one_request_and_every_talk_of_the_quarter() -> None:
    requests, drafts = run()
    assert requests == [PAGE]
    assert len(drafts) == 10
    days = [draft.start.date() for draft in drafts if isinstance(draft.start, datetime)]
    assert days[0] == date(2026, 9, 24)
    assert days[-1] == date(2026, 12, 3)
    assert all(day.weekday() == 3 for day in days)  # Thursdays


def test_a_talk_with_speaker_and_title() -> None:
    _, drafts = run()
    talk = on(drafts, date(2026, 11, 12))
    assert (talk.start, talk.end) == (
        datetime(2026, 11, 12, 13, 30),
        datetime(2026, 11, 12, 14, 50),
    )
    assert people(talk) == [("Lucas Janson", None)]
    assert talk.title == (
        "Chiseling: Powerful and Valid Subgroup Selection via Interactive Machine Learning"
    )
    assert talk.location == ROOM  # the heading of the page names it for every talk
    assert talk.url is None  # no page per talk; the title links to a PDF flyer


def test_a_title_that_breaks_across_lines_is_one_line() -> None:
    _, drafts = run()
    talk = on(drafts, date(2026, 10, 29))
    assert people(talk) == [("Zachary Butzin-Dozier", None)]
    assert talk.title == (
        "Machine Learning for Causal Inference via Pediatric Electronic Health Records "
        "in Epic Cosmos"
    )


def test_the_next_talks_have_a_speaker_but_no_title_yet() -> None:
    _, drafts = run()
    assert [
        (people(on(drafts, day)), on(drafts, day).title)
        for day in (
            date(2026, 11, 19),
            date(2026, 12, 3),
        )
    ] == [
        ([("Marinka Zitnik", None)], "Coming soon"),
        ([("Rafael Irizarry", None)], "Coming soon"),
    ]


def test_the_description_above_the_schedule_is_no_talk() -> None:
    # It mentions "a special seminar with Dr. X. Shirley Liu (PhD '02) on October 27th,
    # 2026", which belongs to another series and is not on the schedule.
    _, drafts = run()
    names = {name for draft in drafts for name, _ in people(draft)}
    assert "Robert Tibshirani" not in names
    assert not any(
        isinstance(draft.start, datetime) and draft.start.date() == date(2026, 10, 27)
        for draft in drafts
    )


def test_every_talk_is_in_the_room_the_heading_names() -> None:
    _, drafts = run()
    assert {draft.location for draft in drafts} == {ROOM}


def label(name: str, text: str) -> str:
    return f'<span style="font-size: 18pt;"><strong>{name}</strong> {text}</span>'


@pytest.mark.parametrize(
    ("markup", "speakers", "title"),
    [
        pytest.param(
            "<strong>Date:</strong> November 19, 2026</span><br />\n"
            + label("Speaker:", "Marinka Zitnik"),
            [("Marinka Zitnik", None)],
            None,
            id="no title line",
        ),
        pytest.param(
            "<strong>Date:</strong> November 19, 2026</span><br />\n"
            + label("Speakers:", "Marinka Zitnik and Jure Leskovec")
            + "<br />\n"
            + label("Title:", "Graph learning"),
            [("Marinka Zitnik", None), ("Jure Leskovec", None)],
            "Graph learning",
            id="two speakers",
        ),
        pytest.param(
            "<strong>Date:</strong> Thursday, November 19, 2026</span><br />\n"
            + label("Speaker:", "Marinka Zitnik")
            + "<br />\n"
            + label("Title:", "Coming soon"),
            [("Marinka Zitnik", None)],
            "Coming soon",
            id="a weekday before the date",
        ),
        pytest.param(
            "<strong>Date:</strong> Nov. 19, 2026</span><br />\n"
            + label("Speaker:", "Marinka Zitnik")
            + "<br />\n"
            + label("Title:", "Coming soon"),
            [("Marinka Zitnik", None)],
            "Coming soon",
            id="an abbreviated month",
        ),
        pytest.param(
            "<strong>Date:</strong> Thurs., Nov. 19, 2026</span><br />\n"
            + label("Speaker:", "Marinka Zitnik")
            + "<br />\n"
            + label("Title:", "Coming soon"),
            [("Marinka Zitnik", None)],
            "Coming soon",
            id="an abbreviated weekday",
        ),
        pytest.param(
            "<strong>Date:</strong> November 19, 2026</span><br />\n"
            + label("Speaker:", "Marinka Zitnik")
            + "<br />\n"
            + label("Title:", "Up-to-Date: methods for cohort data"),
            [("Marinka Zitnik", None)],
            "Up-to-Date: methods for cohort data",
            id="a label inside a word of the title",
        ),
        pytest.param(
            "<strong>Date:</strong> November 19, 2026</span><br />\n"
            + label("Title:", "Keeping models up-to-Date: a guide")
            + "<br />\n"
            + label("Speaker:", "Marinka Zitnik"),
            [("Marinka Zitnik", None)],
            "Keeping models up-to-Date: a guide",
            id="a label inside a word of a title before the speaker",
        ),
        pytest.param(
            "<strong>Date:</strong> November 19, 2026</span><br />\n"
            + label("Speaker:", "Marinka Zitnik")
            + "<br />\n"
            + label("Title:", "Save the Date: causal inference"),
            [("Marinka Zitnik", None)],
            "Save the",
            id="a label as a word of its own cuts the title there",
        ),
        pytest.param(
            "<strong>Date:</strong> Nov 19 2026</span><br />\n"
            + label("Title:", "Coming soon")
            + "<br />\n"
            + label("Speaker:", "Marinka Zitnik"),
            [("Marinka Zitnik", None)],
            "Coming soon",
            id="the title before the speaker",
        ),
    ],
)
def test_a_row_written_another_way_is_still_a_talk(
    markup: str, speakers: list[tuple[str, None]], title: str | None
) -> None:
    drafts = run_with_row(markup)
    assert len(drafts) == 10
    talk = on(drafts, date(2026, 11, 19))
    assert (people(talk), talk.title, talk.location) == (speakers, title, ROOM)
    assert (talk.start, talk.end) == (
        datetime(2026, 11, 19, 13, 30),
        datetime(2026, 11, 19, 14, 50),
    )


@pytest.mark.parametrize(
    "markup",
    [
        pytest.param(
            "<strong>Date:</strong> November 26, 2026</span><br />\n"
            + label("Speaker:", "No seminar (Thanksgiving)"),
            id="in the speaker line",
        ),
        pytest.param(
            "<strong>Date:</strong> November 26, 2026</span><br />\n"
            + label("Speaker:", "-")
            + "<br />\n"
            + label("Title:", "No class: Thanksgiving break"),
            id="in the title line",
        ),
        pytest.param(
            "<strong>Date:</strong> November 26, 2026</span><br />\n"
            + label("Speaker:", "No Seminar (Thanksgiving)"),
            id="capitalised in the speaker line",
        ),
        pytest.param(
            "<strong>Date:</strong> November 26, 2026</span><br />\n"
            + label("Speaker:", "NO SEMINAR - Thanksgiving"),
            id="in capitals in the speaker line",
        ),
        pytest.param(
            "<strong>Date:</strong> November 26, 2026</span><br />\n"
            + label("Speaker:", "No talk this week"),
            id="no talk in the speaker line",
        ),
        pytest.param(
            "<strong>Date:</strong> November 26, 2026</span><br />\n"
            + label("Speaker:", "-")
            + "<br />\n"
            + label("Title:", "No Class - Thanksgiving"),
            id="capitalised in the title line",
        ),
        pytest.param(
            "<strong>Date:</strong> November 26, 2026</span><br />\n"
            + label("Speaker:", "-")
            + "<br />\n"
            + label("Title:", "NO CLASS - Thanksgiving"),
            id="in capitals in the title line",
        ),
        pytest.param(
            "<strong>Date:</strong> November 26, 2026</span><br />\n"
            + label("Speaker:", "No Seminars (Thanksgiving week)"),
            id="a plural in the speaker line",
        ),
        pytest.param(
            "<strong>Date:</strong> November 26, 2026</span><br />\n"
            + label("Speaker:", "No talks this week"),
            id="no talks in the speaker line",
        ),
        pytest.param(
            "<strong>Date:</strong> November 26, 2026</span><br />\n"
            + label("Speaker:", "-")
            + "<br />\n"
            + label("Title:", "No classes - Thanksgiving"),
            id="a plural in the title line",
        ),
    ],
)
def test_a_row_that_says_there_is_no_talk_is_none(markup: str) -> None:
    drafts = run_with_row(markup)
    assert len(drafts) == 9
    days = {draft.start.date() for draft in drafts if isinstance(draft.start, datetime)}
    assert date(2026, 11, 26) not in days
    names = {name.lower() for draft in drafts for name, _ in people(draft)}
    assert not any(word in name for name in names for word in ("seminar", "talk"))
    assert not any((draft.title or "").lower().startswith("no ") for draft in drafts)
