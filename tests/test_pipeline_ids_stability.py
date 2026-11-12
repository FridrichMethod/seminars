"""Tests for uids over several runs of sources that know a talk only by its day.

Such a source gives a talk no native id and no page of its own, so its key
is the day of the talk with the first speaker, the title or the time
(``seminars.pipeline.ids``). Ordinary edits change that key: the talk moves
to another day, a speaker's name is spelled differently, a generic title
gives way to the real one, a page of a TBA talk gets a new address. A
subscriber must still see one entry per talk after every run, and an
import of an older and then a newer file must not leave a second one.

The helpers and the ``config`` fixture are those of tests/test_pipeline_run.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from factories import make_draft
from fakes import FakeAdapter
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig
from seminars.models import Event, EventDraft
from seminars.outputs.ics import write_ics_feeds
from test_pipeline_run import A_URL, MORNING, config, run, services, stored

__all__ = ["config"]  # the fixture of test_pipeline_run

L_STROKE = "\u0141"


def slot(
    start: datetime, title: str, speaker: str | None = None, **overrides: object
) -> EventDraft:
    """A listing without a native id or a page of its own: its key is a day key."""
    values: dict[str, object] = {
        "source_id": "sites-a",
        "native_id": None,
        "url": A_URL,
        "title": title,
        "speaker_text": speaker,
        "start": start,
        "end": None,
    }
    return make_draft(**{**values, **overrides})


def events(cfg: AppConfig) -> list[Event]:
    return sorted(stored(cfg).values(), key=lambda event: (event.start, event.uid))


def two_runs(
    cfg: AppConfig, adapter: FakeAdapter, before: list[EventDraft], after: list[EventDraft]
) -> tuple[list[Event], list[Event]]:
    adapter.drafts = before
    run(cfg, services(FakeEmitters()))
    first = events(cfg)
    adapter.drafts = after
    run(cfg, services(FakeEmitters()), days=1)
    return first, events(cfg)


class TestTwoTalksOfOneDay:
    def test_two_talks_of_one_speaker_on_one_day_stay_two_events(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [
            slot(MORNING + timedelta(hours=2), "Lecture I: Random matrices", "Ann Smith"),
            slot(MORNING + timedelta(hours=6), "Lecture II: Free probability", "Ann Smith"),
        ]
        report = run(config, services(FakeEmitters(), write_ics_feeds=write_ics_feeds))
        assert report.ok
        assert [event.title for event in events(config)] == [
            "Lecture I: Random matrices",
            "Lecture II: Free probability",
        ]
        feed = (config.paths.public_feeds_dir / "all.ics").read_text(encoding="utf-8")
        assert feed.count("BEGIN:VEVENT") == 2

    def test_two_talks_of_one_title_without_speakers_on_one_day_stay_two_events(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [
            slot(MORNING + timedelta(hours=4), "Student Seminar"),
            slot(MORNING + timedelta(hours=8), "Student Seminar"),
        ]
        run(config, services(FakeEmitters()))
        assert len(events(config)) == 2

    def test_the_later_talk_keeps_its_uid_when_the_earlier_one_leaves_the_page(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        early = slot(MORNING + timedelta(hours=2), "Lecture I: Random matrices", "Ann Smith")
        late = slot(MORNING + timedelta(hours=6), "Lecture II: Free probability", "Ann Smith")
        first, second = two_runs(config, fake_adapter, [early, late], [late])
        assert {event.uid for event in second} == {event.uid for event in first}
        by_uid = {event.uid: event for event in second}
        assert by_uid[first[1].uid].title == "Lecture II: Free probability"
        assert by_uid[first[0].uid].title == "Lecture I: Random matrices"

    def test_the_same_input_gives_the_same_uids_in_every_run(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        talks = [
            slot(MORNING + timedelta(hours=2), "Lecture I: Random matrices", "Ann Smith"),
            slot(MORNING + timedelta(hours=6), "Lecture II: Free probability", "Ann Smith"),
        ]
        first, second = two_runs(config, fake_adapter, talks, list(reversed(talks)))
        assert first == second
