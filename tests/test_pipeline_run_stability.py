"""Tests for what several runs of the pipeline keep: uids, keys and events that go missing.

The helpers and the ``config`` fixture are those of tests/test_pipeline_run.py.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from factories import REFERENCE_NOW, make_draft
from fakes import FakeAdapter
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig
from seminars.http import HttpStatusError
from seminars.models import EventDraft, UidMap, read_model
from test_pipeline_run import A_URL, MORNING, by_key, config, draft, run, services, stored

__all__ = ["config"]  # the fixture of test_pipeline_run


class TestStability:
    def test_the_same_input_gives_the_same_files(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1"), draft("sites-b", "2")]
        run(config, services(FakeEmitters()))
        first = config.paths.events_file.read_bytes()
        uid_map = config.paths.uid_map_file.read_bytes()
        report = run(config, services(FakeEmitters()), days=1)
        assert config.paths.events_file.read_bytes() == first
        assert config.paths.uid_map_file.read_bytes() == uid_map
        assert (report.counts.added, report.counts.changed) == (0, 0)

    def test_a_talk_two_hosts_list_keeps_the_values_of_the_same_host(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        # Once the uid map binds both keys to one uid, the host whose values the merged
        # event takes must not change: here the other host's values sort first.
        fake_adapter.drafts = [
            draft("sites-a", "1", host_org="Zeta Department", location="Room Z"),
            draft("sites-b", "1", host_org="Alpha Department", location="Room A"),
        ]
        run(config, services(FakeEmitters()))
        (first,) = stored(config).values()
        assert (first.host_org, first.location) == ("Zeta Department", "Room Z")
        report = run(config, services(FakeEmitters()), days=1)
        assert stored(config) == {first.uid: first}
        assert (report.counts.added, report.counts.changed) == (0, 0)

    def test_a_new_title_keeps_the_uid_and_raises_the_sequence(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1", title="TBA")]
        run(config, services(FakeEmitters()))
        before = by_key(config)["sites-a:1"]
        fake_adapter.drafts = [draft("sites-a", "1", title="Folding proteins in the cell")]
        report = run(config, services(FakeEmitters()), days=1)
        after = by_key(config)["sites-a:1"]
        assert after.uid == before.uid
        assert (after.sequence, after.title) == (1, "Folding proteins in the cell")
        assert after.last_changed == REFERENCE_NOW + timedelta(days=1)
        assert after.first_seen == REFERENCE_NOW
        assert report.counts.changed == 1

    def test_a_new_key_of_the_same_talk_keeps_the_uid(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1")]
        run(config, services(FakeEmitters()))
        uid = by_key(config)["sites-a:1"].uid
        renamed = {"title": "Talk number 1 on protein folding", "speaker_text": "Speaker 1 Person"}
        fake_adapter.drafts = [draft("sites-a", "1b", **renamed)]
        run(config, services(FakeEmitters()), days=1)
        assert list(stored(config)) == [uid]
        assert by_key(config)["sites-a:1b"].uid == uid

    def test_a_tba_slot_keeps_its_uid_through_title_and_speaker(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        def slot(title: str, speaker: str | None = None) -> EventDraft:
            return make_draft(
                source_id="sites-a",
                native_id=None,
                url=None,
                title=title,
                speaker_text=speaker,
                start=MORNING,
                end=None,
            )

        fake_adapter.drafts = [slot("TBA")]
        run(config, services(FakeEmitters()))
        (uid,) = stored(config)
        fake_adapter.drafts = [slot("Folding proteins in the cell")]
        first = run(config, services(FakeEmitters()), days=1)
        assert list(stored(config)) == [uid]
        assert stored(config)[uid].title == "Folding proteins in the cell"
        fake_adapter.drafts = [slot("Folding proteins in the cell", "Ada Lovelace")]
        second = run(config, services(FakeEmitters()), days=2)
        (event,) = stored(config).values()
        assert event.uid == uid
        assert [speaker.name for speaker in event.speakers] == ["Ada Lovelace"]
        assert event.sequence == 2
        uid_map = read_model(config.paths.uid_map_file, UidMap)
        assert uid_map is not None
        keys = ["t0800", "folding-proteins-in-the-cell", "ada-lovelace"]
        assert {uid_map.resolve(f"sites-a:day:2026-11-17:{key}") for key in keys} == {uid}
        assert (first.counts.added, first.counts.changed) == (0, 1)
        assert (second.counts.added, second.counts.changed) == (0, 1)

    def test_a_tba_talk_with_a_native_id_is_not_taken_over_while_it_is_away(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        tba = draft("sites-a", "1001", title="TBA", speaker_text=None, start=MORNING)
        other = draft("sites-a", "2002", title="Enzymes in motion", start=MORNING)
        fake_adapter.drafts = [tba]
        run(config, services(FakeEmitters()))
        fake_adapter.drafts = [other]  # 1001 is away for one run, 2002 appears at its start
        run(config, services(FakeEmitters()), days=1)
        assert by_key(config)["sites-a:2002"].uid != by_key(config)["sites-a:1001"].uid
        fake_adapter.drafts = [tba.evolve(title="Knots and braids"), other]
        report = run(config, services(FakeEmitters()), days=2)
        events = by_key(config)
        assert (report.counts.events, report.counts.merged) == (2, 0)
        assert events["sites-a:1001"].uid == "sites-a.1001"
        assert events["sites-a:2002"].uid == "sites-a.2002"
        assert events["sites-a:1001"].title == "Knots and braids"
        assert events["sites-a:2002"].title == "Enzymes in motion"

    def test_a_tba_talk_on_a_series_page_is_not_taken_over_while_it_is_away(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        series = "https://a.example.edu/colloquium-series"
        tba = draft("sites-a", "1001", title="TBA", speaker_text=None, start=MORNING, url=series)
        other = draft("sites-a", "2002", title="Enzymes in motion", start=MORNING, url=series)
        fake_adapter.drafts = [tba]
        run(config, services(FakeEmitters()))
        fake_adapter.drafts = [other]  # 1001 is away, 2002 appears at its start on its page
        run(config, services(FakeEmitters()), days=1)
        assert by_key(config)["sites-a:2002"].uid == "sites-a.2002"
        back = tba.evolve(title="Knots and braids", speaker_text="Ann Smith")
        fake_adapter.drafts = [back, other]
        report = run(config, services(FakeEmitters()), days=2)
        events = by_key(config)
        assert (report.counts.events, report.counts.merged) == (2, 0)
        assert (events["sites-a:1001"].uid, events["sites-a:2002"].uid) == (
            "sites-a.1001",
            "sites-a.2002",
        )
        assert [s.name for s in events["sites-a:1001"].speakers] == ["Ann Smith"]
        uid_map = read_model(config.paths.uid_map_file, UidMap)
        assert uid_map is not None
        assert uid_map.resolve("sites-a:2002") in (None, "sites-a.2002")

    @pytest.mark.parametrize("minutes", [0, 10])
    def test_a_talk_that_names_its_speaker_late_leaves_the_talk_it_joined(
        self, config: AppConfig, fake_adapter: FakeAdapter, minutes: int
    ) -> None:
        title = "Astrophysics Colloquium"
        first = draft("sites-a", "1001", title=title, speaker_text=None, start=MORNING)
        later = MORNING + timedelta(minutes=minutes)
        second = draft("sites-a", "2002", title=title, speaker_text="Bea Jones", start=later)
        fake_adapter.drafts = [first, second]
        joined = run(config, services(FakeEmitters()))
        assert (joined.counts.events, joined.counts.merged) == (1, 1)
        fake_adapter.drafts = [first.evolve(speaker_text="Ann Smith"), second]
        report = run(config, services(FakeEmitters()), days=1)
        assert (report.counts.events, report.counts.merged) == (2, 0)
        events = by_key(config)
        assert (events["sites-a:1001"].uid, events["sites-a:2002"].uid) == (
            "sites-a.1001",
            "sites-a.2002",
        )
        assert [s.name for s in events["sites-a:1001"].speakers] == ["Ann Smith"]
        assert [s.name for s in events["sites-a:2002"].speakers] == ["Bea Jones"]
        assert events["sites-a:1001"].source_ids == ["sites-a:1001"]
        uid_map = read_model(config.paths.uid_map_file, UidMap)
        assert uid_map is not None
        assert uid_map.resolve("sites-a:2002") == "sites-a.2002"
        again = run(config, services(FakeEmitters()), days=2)
        assert (again.counts.events, again.counts.added, again.counts.changed) == (2, 0, 0)

    @pytest.mark.parametrize(
        ("old_title", "new_title"),
        [
            ("Astrophysics Colloquium", "Astrophysics Colloquium"),
            ("Applied Physics Colloquium", "Applied Physics Colloquium: Knots and braids"),
        ],
    )
    def test_a_talk_without_a_speaker_is_not_taken_over_by_its_series_title(
        self, config: AppConfig, fake_adapter: FakeAdapter, old_title: str, new_title: str
    ) -> None:
        first = draft("sites-a", "1001", title=old_title, speaker_text=None, start=MORNING)
        second = draft("sites-a", "2002", title=new_title, speaker_text="Bea Jones", start=MORNING)
        fake_adapter.drafts = [first]
        run(config, services(FakeEmitters()))
        fake_adapter.drafts = [second]  # 1001 is away for one run, 2002 appears at its start
        run(config, services(FakeEmitters()), days=1)
        assert by_key(config)["sites-a:2002"].uid == "sites-a.2002"
        fake_adapter.drafts = [first.evolve(speaker_text="Ann Smith"), second]
        report = run(config, services(FakeEmitters()), days=2)
        assert (report.counts.events, report.counts.merged) == (2, 0)
        events = by_key(config)
        assert [s.name for s in events["sites-a:1001"].speakers] == ["Ann Smith"]
        assert [s.name for s in events["sites-a:2002"].speakers] == ["Bea Jones"]


class TestAbsence:
    def test_a_missing_event_is_removed_after_three_healthy_runs(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1"), draft("sites-a", "2")]
        run(config, services(FakeEmitters()))
        fake_adapter.drafts = [draft("sites-a", "1")]
        seen = []
        for day in (1, 2):
            report = run(config, services(FakeEmitters()), days=day)
            seen.append((by_key(config)["sites-a:2"].missing_runs, report.counts.missing))
        assert seen == [(1, 1), (2, 1)]
        report = run(config, services(FakeEmitters()), days=3)
        assert "sites-a:2" not in by_key(config)
        assert report.counts.removed == 1

    def test_an_event_that_returns_starts_counting_again(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1"), draft("sites-a", "2")]
        run(config, services(FakeEmitters()))
        fake_adapter.drafts = [draft("sites-a", "1")]
        run(config, services(FakeEmitters()), days=1)
        fake_adapter.drafts = [draft("sites-a", "1"), draft("sites-a", "2")]
        run(config, services(FakeEmitters()), days=2)
        assert by_key(config)["sites-a:2"].missing_runs == 0

    def test_a_failed_or_empty_source_changes_nothing(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1"), draft("sites-b", "2")]
        run(config, services(FakeEmitters()))
        fake_adapter.drafts = [draft("sites-b", "2")]  # sites-a is empty now
        run(config, services(FakeEmitters()), days=1)
        assert by_key(config)["sites-a:1"].missing_runs == 0
        fake_adapter.fetch_error = HttpStatusError(A_URL, 503)
        for day in (2, 3, 4):
            run(config, services(FakeEmitters()), days=day)
        assert by_key(config)["sites-a:1"].missing_runs == 0

    def test_a_source_left_out_of_the_run_changes_nothing(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-a", "1"), draft("sites-b", "2")]
        run(config, services(FakeEmitters()))
        for day in (1, 2, 3):
            run(config, services(FakeEmitters()), days=day, source_ids=("sites-b",))
        assert by_key(config)["sites-a:1"].missing_runs == 0

    def test_a_cancelled_talk_stays_until_its_time_has_passed(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [
            draft("sites-a", "1", title="Cancelled: Talk number 1 on protein folding"),
            draft("sites-a", "2"),
        ]
        report = run(config, services(FakeEmitters()))
        event = by_key(config)["sites-a:1"]
        assert event.status == "cancelled"
        assert report.counts.cancelled == 1
        fake_adapter.drafts = [draft("sites-a", "2")]
        for day in (1, 2, 3, 4):
            run(config, services(FakeEmitters()), days=day)
        assert by_key(config)["sites-a:1"] == event
        run(config, services(FakeEmitters()), days=30)
        assert "sites-a:1" not in by_key(config)
        archive = config.paths.archive_file(event.start.year).read_text(encoding="utf-8")
        assert event.uid in archive
