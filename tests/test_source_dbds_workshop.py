"""The Workshop in Biomedical Data Science (html_list) over its recorded page.

The page gives a date, a speaker and a title per talk and states the time of
every talk once, in its heading; the source entry supplies it as default times.
Times are naive wall times of the source until normalization applies its zone.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from factories import make_window
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


def run() -> tuple[list[str], list[EventDraft]]:
    source = SOURCES[SOURCE_ID]
    http = offline_client(SOURCE_ID)
    adapter = get_adapter(source.adapter)
    drafts = adapter.parse(adapter.fetch(http, source, window=make_window(NOW)), source)
    return list(http.requests), drafts


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
    assert talk.location is None  # the room is in the heading of the page only
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
