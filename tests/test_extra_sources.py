"""The three html_list sources of stanford-extra.yaml over their recorded pages.

Each test pins what the fixture README of the source lists under "What the
source entry reads from it": the requests, the number of drafts and the
fields of the talks named there. Times are naive wall times of the source.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from factories import make_window
from seminars.config import load_sources
from seminars.http import offline_client
from seminars.models import EventDraft
from seminars.sources.base import get_adapter

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCES = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
NOW = datetime(2026, 11, 12, 6, 37, 13, tzinfo=UTC)
"""The recording time of these fixtures, which is also the "now" of an offline run."""


def run(source_id: str) -> tuple[list[str], list[EventDraft]]:
    source = SOURCES[source_id]
    http = offline_client(source_id)
    adapter = get_adapter(source.adapter)
    drafts = adapter.parse(adapter.fetch(http, source, window=make_window(NOW)), source)
    return list(http.requests), drafts


def on(drafts: list[EventDraft], day: date) -> EventDraft:
    found = [draft for draft in drafts if _day(draft) == day]
    assert len(found) == 1, [str(draft.start) for draft in drafts]
    return found[0]


def _day(draft: EventDraft) -> date | None:
    return draft.start.date() if isinstance(draft.start, datetime) else None


def people(draft: EventDraft) -> list[tuple[str, str | None]]:
    return [(speaker.name, speaker.affiliation) for speaker in draft.speakers]


def text_of(draft: EventDraft) -> Any:
    return draft.abstract or draft.abstract_html


class TestAi4Science:
    """Marlowe AI + Data for Science: one schedule table, open dates skipped."""

    def test_one_request_and_the_talks_of_the_schedule(self) -> None:
        requests, drafts = run("custom-ai4science")
        assert requests == ["https://aida4sci.stanford.edu/schedule.html"]
        assert len(drafts) == 19

    def test_a_talk_with_everything(self) -> None:
        _, drafts = run("custom-ai4science")
        talk = on(drafts, date(2026, 11, 11))
        assert (talk.start, talk.end) == (
            datetime(2026, 11, 11, 16, 30),
            datetime(2026, 11, 11, 17, 30),
        )
        assert talk.location == "CoDA E160"
        assert people(talk) == [("Alex Pentland", "HAI, Stanford University")]
        assert (
            talk.title == "Detecting coordinated behavior across multiple fully encrypted domains"
        )
        assert text_of(talk)
        assert talk.url is None

    def test_the_room_notice_moves_two_talks(self) -> None:
        _, drafts = run("custom-ai4science")
        for day in (date(2026, 9, 23), date(2026, 10, 7)):
            assert on(drafts, day).location == "Packard 101"

    def test_talks_without_a_title_keep_their_speaker_and_the_default_times(self) -> None:
        _, drafts = run("custom-ai4science")
        untitled = [draft for draft in drafts if draft.title is None]
        days = sorted(_day(draft) or date.min for draft in untitled)
        winter = [day for day in days if date(2027, 1, 6) <= day <= date(2027, 3, 3)]
        assert len(untitled) == 9
        assert len(winter) == 8
        assert days[-1] == date(2027, 5, 12)
        assert people(on(drafts, date(2027, 5, 12)))[0][0] == "Mohsen Bayati"
        for draft in untitled:
            assert draft.speakers
            assert isinstance(draft.start, datetime)
            assert (draft.start.hour, draft.start.minute) == (16, 30)
            assert draft.end is not None


class TestCsbEvents:
    """Chemical and Systems Biology: seminar and defense posts, nothing else."""

    def test_one_request_and_four_talks(self) -> None:
        requests, drafts = run("custom-csb-events")
        assert requests[0] == "https://chemsysbio.stanford.edu/events/"
        assert len(requests) == 3  # and the posts of the two talks in the window
        assert len(drafts) == 4

    def test_the_special_seminar_with_its_post(self) -> None:
        _, drafts = run("custom-csb-events")
        talk = on(drafts, date(2026, 11, 16))
        assert (talk.start, talk.end) == (
            datetime(2026, 11, 16, 11, 0),
            datetime(2026, 11, 16, 12, 45),
        )
        assert talk.location == "Munzer"
        assert [name for name, _ in people(talk)] == ["David Garfield"]
        assert talk.title == "CSB Special Seminar & Open Discussion, David Garfield, PhD"
        assert talk.event_type_hint == "seminar"
        assert talk.url is not None
        assert talk.url.startswith(
            "https://chemsysbio.stanford.edu/events/join-us-csb-special-seminar-open-discussion"
        )

    def test_a_seminar_without_an_end(self) -> None:
        _, drafts = run("custom-csb-events")
        talk = on(drafts, date(2026, 12, 3))
        assert (talk.start, talk.end) == (datetime(2026, 12, 3, 12, 30), None)
        assert talk.location == "Clark Auditorium"
        assert [name for name, _ in people(talk)] == ["Stephan Sieber"]

    def test_the_two_defenses_get_the_year_closest_to_the_fetch(self) -> None:
        _, drafts = run("custom-csb-events")
        defenses = sorted(
            (draft for draft in drafts if draft.event_type_hint == "defense"),
            key=lambda draft: str(draft.start),
        )
        assert [(d.start, d.location, people(d)[0][0]) for d in defenses] == [
            (datetime(2026, 9, 15, 12, 0), "Clark S360 or via Zoom", "Alex Van Elgort"),
            (datetime(2026, 9, 23, 13, 0), "ChEM-H E153", "Rebecca Rodell"),
        ]


class TestIslColloquium:
    """ISL Colloquium: every talk of the archive, from the list alone."""

    def test_one_request_and_every_talk_of_the_list(self) -> None:
        requests, drafts = run("custom-isl-colloquium")
        assert requests == ["https://isl.stanford.edu/talks/"]
        assert len(drafts) == 152

    def test_the_talk_of_the_recording_day(self) -> None:
        _, drafts = run("custom-isl-colloquium")
        talk = on(drafts, date(2026, 11, 12))
        assert (talk.start, talk.end) == (datetime(2026, 11, 12, 16, 0), None)
        assert talk.location == "Packard 202"
        assert people(talk) == [("Thomas Courtade", "Associate Professor, UC Berkeley")]
        assert talk.title == "A galaxy of information inequalities, with applications"
        assert talk.url == "https://isl.stanford.edu/talks/talk-schedule/fall-2026-thomas-courtade"

    def test_the_newest_talk(self) -> None:
        _, drafts = run("custom-isl-colloquium")
        newest = max(drafts, key=lambda draft: str(draft.start))
        assert newest.start == datetime(2026, 12, 3, 16, 0)
        assert people(newest)[0][0] == "Guillaume Derval"
