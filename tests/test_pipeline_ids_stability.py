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


class TestOneEntryPerTalk:
    def test_a_talk_that_moves_to_another_day_keeps_its_uid(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        moved = MORNING + timedelta(days=7)
        first, second = two_runs(
            config,
            fake_adapter,
            [slot(MORNING, "Quantum codes", "Ann Smith")],
            [slot(moved, "Quantum codes", "Ann Smith")],
        )
        assert [event.uid for event in second] == [first[0].uid]
        assert second[0].start == moved
        assert second[0].sequence == 1

    def test_a_moved_talk_whose_title_says_so_keeps_its_uid(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        moved = MORNING + timedelta(days=7)
        first, second = two_runs(
            config,
            fake_adapter,
            [slot(MORNING, "Quantum codes", "Ann Smith")],
            [slot(moved, "RESCHEDULED: Quantum codes", "Ann Smith")],
        )
        assert [event.uid for event in second] == [first[0].uid]
        assert second[0].title == "RESCHEDULED: Quantum codes"

    def test_a_tba_talk_that_moves_to_another_day_keeps_its_uid(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        moved = MORNING + timedelta(days=14)
        first, second = two_runs(
            config,
            fake_adapter,
            [
                slot(MORNING, "TBA", "Ann Smith"),
                slot(MORNING + timedelta(days=7), "TBA", "Bob Jones"),
            ],
            [
                slot(moved, "TBA", "Ann Smith"),
                slot(MORNING + timedelta(days=7), "TBA", "Bob Jones"),
            ],
        )
        assert [event.uid for event in second] == [first[1].uid, first[0].uid]

    def test_a_talk_without_speakers_that_moves_to_another_day_keeps_its_uid(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        moved = MORNING + timedelta(days=7)
        first, second = two_runs(
            config,
            fake_adapter,
            [slot(MORNING, "Locally testable codes")],
            [slot(moved, "Locally testable codes")],
        )
        assert [event.uid for event in second] == [first[0].uid]
        assert second[0].start == moved

    def test_a_tba_talk_without_speakers_that_moves_by_an_hour_keeps_its_uid(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        moved = MORNING + timedelta(hours=1)
        first, second = two_runs(config, fake_adapter, [slot(MORNING, "TBA")], [slot(moved, "TBA")])
        assert [event.uid for event in second] == [first[0].uid]
        assert second[0].start == moved

    def test_two_speakers_who_swap_their_days_keep_their_uids(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        later = MORNING + timedelta(days=7)
        first, second = two_runs(
            config,
            fake_adapter,
            [slot(MORNING, "TBA", "Ann Smith"), slot(later, "TBA", "Bob Jones")],
            [slot(MORNING, "TBA", "Bob Jones"), slot(later, "TBA", "Ann Smith")],
        )
        uid_of = {event.speakers[0].name: event.uid for event in first}
        assert len(second) == 2
        assert {event.speakers[0].name: event.uid for event in second} == uid_of

    def test_a_generic_title_that_gives_way_to_the_real_one_keeps_the_uid(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        first, second = two_runs(
            config,
            fake_adapter,
            [slot(MORNING, "Theory Lunch")],
            [slot(MORNING, "Locally testable quantum codes")],
        )
        assert [event.uid for event in second] == [first[0].uid]
        assert second[0].title == "Locally testable quantum codes"

    def test_a_tba_page_that_moves_to_a_new_address_keeps_the_uid(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        first, second = two_runs(
            config,
            fake_adapter,
            [slot(MORNING, "TBA", url=f"{A_URL}/coming-soon-3")],
            [slot(MORNING, "Knots and braids", url=f"{A_URL}/knots-and-braids")],
        )
        assert [event.uid for event in second] == [first[0].uid]

    def test_a_speaker_spelled_differently_keeps_the_uid(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        for old, new in [
            ("Emily B. Fox", "Emily Fox"),
            ("Vincent Cohen-Adad", "Vincent Cohen-Addad"),
            (f"{L_STROKE}ukasz D\u0119bowski", "Lukasz Debowski"),
        ]:
            config.paths.events_file.unlink(missing_ok=True)
            config.paths.uid_map_file.unlink(missing_ok=True)
            first, second = two_runs(
                config, fake_adapter, [slot(MORNING, "TBA", old)], [slot(MORNING, "TBA", new)]
            )
            assert [event.uid for event in second] == [first[0].uid], (old, new)
            assert [speaker.name for speaker in second[0].speakers] == [new]

    def test_another_speaker_in_the_same_slot_is_another_talk(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        first, second = two_runs(
            config,
            fake_adapter,
            [slot(MORNING, "TBA", "Ann Smith")],
            [slot(MORNING, "TBA", "Bob Jones")],
        )
        assert first[0].uid in {event.uid for event in second}
        assert len(second) == 2


class TestATalkThatComesBack:
    def test_a_talk_whose_uid_moved_on_gets_it_back_and_the_other_a_uid_of_its_own(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        first = slot(MORNING, "Quantum codes", "Ann Smith")
        later = slot(MORNING + timedelta(days=7), "Quantum codes", "Ann Smith")
        before, moved = two_runs(config, fake_adapter, [first], [later])
        assert [event.uid for event in moved] == [before[0].uid]
        fake_adapter.drafts = [first, later]  # the first listing was only away for a run
        report = run(config, services(FakeEmitters()), days=2)
        back = events(config)
        assert [event.start for event in back] == [first.start, later.start]
        assert back[0].uid == before[0].uid
        assert back[1].uid != before[0].uid
        assert (report.counts.events, report.counts.merged) == (2, 0)

    def test_a_tba_page_that_comes_back_next_to_its_new_address_is_two_events(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        old = slot(MORNING, "TBA", url=f"{A_URL}/coming-soon-3")
        new = slot(MORNING, "Knots and braids", url=f"{A_URL}/knots-and-braids")
        before, _ = two_runs(config, fake_adapter, [old], [new])
        fake_adapter.drafts = [old, new]
        run(config, services(FakeEmitters()), days=2)
        back = events(config)
        assert len(back) == 2
        assert len({event.uid for event in back}) == 2
        by_title = {event.title: event.uid for event in back}
        assert by_title["TBA"] == before[0].uid


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

    def test_the_earlier_talk_keeps_its_uid_when_the_later_one_leaves_the_page(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        early = slot(MORNING + timedelta(hours=2), "Lecture I: Random matrices", "Ann Smith")
        late = slot(MORNING + timedelta(hours=6), "Lecture II: Free probability", "Ann Smith")
        first, second = two_runs(config, fake_adapter, [early, late], [early])
        titles = {event.uid: event.title for event in first}
        listed = [event for event in second if event.missing_runs == 0]
        assert [(event.uid, event.title) for event in listed] == [(first[0].uid, early.title)]
        assert {event.uid: event.title for event in second}.items() <= titles.items()
        fake_adapter.drafts = [early, late]  # the later talk was only away for a run
        run(config, services(FakeEmitters()), days=2)
        assert {event.uid: event.title for event in events(config)} == titles

    def test_each_talk_keeps_its_uid_when_the_later_one_moves_to_another_day(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        early = slot(MORNING + timedelta(hours=2), "Lecture I: Random matrices", "Ann Smith")
        late = slot(MORNING + timedelta(hours=6), "Lecture II: Free probability", "Ann Smith")
        moved = slot(MORNING + timedelta(days=7), "Lecture II: Free probability", "Ann Smith")
        first, second = two_runs(config, fake_adapter, [early, late], [early, moved])
        assert {event.uid: event.title for event in second} == {
            event.uid: event.title for event in first
        }
        assert [event.start for event in second] == [early.start, moved.start]

    def test_the_same_input_gives_the_same_uids_in_every_run(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        talks = [
            slot(MORNING + timedelta(hours=2), "Lecture I: Random matrices", "Ann Smith"),
            slot(MORNING + timedelta(hours=6), "Lecture II: Free probability", "Ann Smith"),
        ]
        first, second = two_runs(config, fake_adapter, talks, list(reversed(talks)))
        assert first == second
