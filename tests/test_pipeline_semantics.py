"""Whole runs for the meaning of a few things sources do, each with the rule behind it.

* A postponed talk is a cancelled one: the announced date does not happen.
* A talk that leaves its source after it started is over, not missing.
* A source is empty for health only when it parsed nothing, not when
  nothing it parsed falls into the window.
* Two talks of one source at one start with different speakers stay two.
* A department's copy of a campus calendar event merges with the calendar's
  listing by their shared address, and the department's values win.
* A page whose address changes when its title is announced keeps the uid
  of its talk when source, start and speaker say which talk it is.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from factories import PACIFIC, REFERENCE_NOW, make_draft, make_source
from fakes import (
    FakeAdapter,
    FakeClock,
    FakeEventFilter,
    FakeFieldTagger,
    FakeHttpClient,
    FakePicksScorer,
)
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig
from seminars.models import Event, EventDraft, RunReport
from seminars.outputs.health import update_health
from seminars.pipeline.run import RunOptions, run_pipeline
from seminars.pipeline.secret_scan import build_secret_scanner
from seminars.pipeline.services import Services
from seminars.pipeline.store import read_events_file

DEPT_URL = "https://physics.example.edu/events"
KIPAC_URL = "https://kipac.example.edu/colloquium"
LOCALIST_URL = "https://events.example.edu/api/2/events"
IMPORTED = "https://events.example.edu/event/applied-physics-colloquium-dmitry-abanin"
START = datetime(2026, 11, 17, 15, 30, tzinfo=PACIFIC)

SOURCES = (
    make_source(id="sites-physics", adapter="fake", url=DEPT_URL, host_org="Stanford Physics"),
    make_source(
        id="bayarea-kipac", group="bayarea", adapter="fake", url=KIPAC_URL, host_org="KIPAC"
    ),
    make_source(
        id="localist-stanford",
        group="localist",
        adapter="fake",
        url=LOCALIST_URL,
        homepage="https://events.example.edu/",
        host_org="Stanford University",
        aggregator=True,
    ),
)


@pytest.fixture
def config(app_config: AppConfig) -> AppConfig:
    return dataclasses.replace(app_config, sources=SOURCES)


def draft(source_id: str, native_id: str | None, **overrides: Any) -> EventDraft:
    values: dict[str, Any] = {
        "source_id": source_id,
        "native_id": native_id,
        "url": None,
        "title": "Quantum advantage in many-body systems",
        "speaker_text": None,
        "start": START,
        "end": None,
        "location": None,
        "abstract": None,
    }
    return make_draft(**{**values, **overrides})


def services(**overrides: Any) -> Services:
    emitters = FakeEmitters()
    values: dict[str, Any] = {
        "http_for": lambda chosen: FakeHttpClient(),
        "event_filter": FakeEventFilter(),
        "field_tagger": FakeFieldTagger(),
        "picks_scorer": FakePicksScorer(),
        "write_ics_feeds": emitters.write_ics_feeds,
        "write_site_data": emitters.write_site_data,
        "update_health": emitters.update_health,
        "secret_scanner": build_secret_scanner(),
    }
    return Services(**{**values, **overrides})


def run(config: AppConfig, *, at: datetime = REFERENCE_NOW, **overrides: Any) -> RunReport:
    return run_pipeline(
        config, services(**overrides), now=at, options=RunOptions(), timer=FakeClock()
    )


def by_key(config: AppConfig) -> dict[str, Event]:
    events = read_events_file(config.paths.events_file)
    return {key: event for event in events for key in event.source_ids}


def by_title(config: AppConfig) -> dict[str, Event]:
    return {event.title: event for event in read_events_file(config.paths.events_file)}


class TestPostponed:
    def test_a_postponed_talk_is_cancelled_and_stays_until_its_time(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        keep = draft("sites-physics", "keep", title="Knots and braids", start=START)
        fake_adapter.drafts = [draft("sites-physics", "1", title="POSTPONED - Knots"), keep]
        report = run(config)
        event = by_key(config)["sites-physics:1"]
        assert (event.title, event.status) == ("Knots", "cancelled")
        assert report.counts.cancelled == 1
        fake_adapter.drafts = [keep]
        for day in (1, 2, 3, 4):
            report = run(config, at=REFERENCE_NOW + timedelta(days=day))
            assert by_key(config)["sites-physics:1"] == event
            assert report.counts.missing == 0

    def test_a_postponement_on_one_listing_cancels_the_merged_talk(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [
            draft("sites-physics", "1", url=IMPORTED),
            draft("localist-stanford", "54426626599231", url=IMPORTED, title="(Postponed) Talk"),
        ]
        run(config)
        (event,) = by_title(config).values()
        assert event.status == "cancelled"
        assert event.title == "Quantum advantage in many-body systems"


class TestOverOnceStarted:
    def test_a_talk_that_leaves_its_source_after_it_started_is_not_missing(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        later = draft(
            "sites-physics", "2", title="Dark matter underground", start=START.replace(day=25)
        )
        fake_adapter.drafts = [draft("sites-physics", "1"), later]
        run(config)
        before = by_key(config)["sites-physics:1"]
        fake_adapter.drafts = [later]  # the page dropped the talk once it began
        after_start = START.astimezone(REFERENCE_NOW.tzinfo) + timedelta(hours=20)
        for hours in (0, 24, 48, 72):
            report = run(config, at=after_start + timedelta(hours=hours))
            assert report.counts.missing == 0
            assert by_key(config)["sites-physics:1"] == before
        assert before.status == "confirmed"

    def test_a_future_talk_that_leaves_its_source_is_missing(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        other = draft("sites-physics", "2", title="Dark matter underground")
        fake_adapter.drafts = [draft("sites-physics", "1"), other]
        run(config)
        fake_adapter.drafts = [other]
        report = run(config, at=REFERENCE_NOW + timedelta(days=1))
        assert report.counts.missing == 1
        assert by_key(config)["sites-physics:1"].missing_runs == 1


class TestEmptySource:
    def history(self, config: AppConfig, source_id: str) -> list[dict[str, Any]]:
        health = json.loads(config.paths.health_file.read_text(encoding="utf-8"))
        return health["sources"][source_id]["history"]

    def test_a_source_without_events_in_the_window_is_not_empty_for_health(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        far = START + timedelta(days=400)
        fake_adapter.drafts = [draft("bayarea-kipac", "1", start=far), draft("sites-physics", "2")]
        report = run(config, update_health=update_health)
        (kipac,) = [result for result in report.sources if result.source_id == "bayarea-kipac"]
        assert (kipac.status, kipac.drafts, kipac.outside_window) == ("empty", 1, 1)
        assert [entry["status"] for entry in self.history(config, "bayarea-kipac")] == ["ok"]
        assert [entry["status"] for entry in self.history(config, "localist-stanford")] == ["empty"]


class TestOneSourceOneStart:
    def colloquium(self, native_id: str, speaker: str) -> EventDraft:
        return draft(
            "bayarea-kipac",
            native_id,
            url=KIPAC_URL,
            title="Astrophysics Colloquium",
            speaker_text=speaker,
            start=START,
        )

    def test_two_colloquia_with_different_speakers_stay_two(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [
            self.colloquium("a", "Ann Smith (MIT)"),
            self.colloquium("b", "Bea Jones (Caltech)"),
            draft(
                "localist-stanford",
                "9",
                url="https://events.example.edu/event/astrophysics-colloquium",
                title="Astrophysics Colloquium",
                speaker_text="Bea Jones",
            ),
        ]
        report = run(config)
        events = by_key(config)
        assert (report.counts.events, report.counts.merged) == (2, 1)
        assert events["bayarea-kipac:a"].uid != events["bayarea-kipac:b"].uid
        assert events["localist-stanford:9"].uid == events["bayarea-kipac:b"].uid
        assert [s.name for s in events["bayarea-kipac:a"].speakers] == ["Ann Smith"]


class TestImportedCampusEvents:
    def test_a_department_copy_merges_with_the_calendar_by_its_address(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [
            draft(
                "sites-physics",
                "0b6c1d2e-uuid",
                url=IMPORTED,
                title="Applied Physics/Physics Colloquium: Dmitry Abanin",
                location="Hewlett 201",
            ),
            draft(
                "localist-stanford",
                "54426626599231",
                url=IMPORTED,
                title='Dmitry Abanin - "Quantum advantage and computational complexity"',
                location="Hewlett Teaching Center, Room 201",
                abstract="A longer abstract from the campus calendar.",
                start=START + timedelta(minutes=5),
            ),
        ]
        report = run(config)
        (event,) = by_title(config).values()
        assert report.counts.merged == 1
        assert sorted(event.source_ids) == [
            "localist-stanford:54426626599231",
            "sites-physics:0b6c1d2e-uuid",
        ]
        assert event.title == "Applied Physics/Physics Colloquium: Dmitry Abanin"
        assert (event.host_org, event.location, event.start) == (
            "Stanford Physics",
            "Hewlett 201",
            START,
        )
        assert str(event.url) == IMPORTED
        assert event.abstract == "A longer abstract from the campus calendar."


class TestRenamedPage:
    def page(self, slug: str, title: str, speaker: str) -> EventDraft:
        return draft(
            "sites-physics", None, url=f"{DEPT_URL}/{slug}", title=title, speaker_text=speaker
        )

    def test_a_new_address_after_the_title_was_announced_keeps_the_uid(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [
            self.page("tba-jane-doe", "TBA", "Jane Doe"),
            self.page("tba-john-roe", "TBA", "John Roe"),
        ]
        run(config)
        before = by_key(config)
        jane = before[f"sites-physics:url:{DEPT_URL}/tba-jane-doe"]
        john = before[f"sites-physics:url:{DEPT_URL}/tba-john-roe"]
        fake_adapter.drafts = [
            self.page("tba-john-roe", "TBA", "John Roe"),
            self.page("folding-proteins", "Folding proteins in the cell", "Jane Doe"),
        ]
        report = run(config, at=REFERENCE_NOW + timedelta(days=1))
        after = by_key(config)
        renamed = after[f"sites-physics:url:{DEPT_URL}/folding-proteins"]
        assert renamed.uid == jane.uid
        assert (renamed.title, renamed.sequence) == ("Folding proteins in the cell", 1)
        assert after[f"sites-physics:url:{DEPT_URL}/tba-john-roe"].uid == john.uid
        assert (report.counts.events, report.counts.added, report.counts.missing) == (2, 0, 0)

    def test_two_speakers_keep_the_uid_through_a_new_address(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [self.page("tba-panel", "TBA", "Jane Doe and John Roe")]
        run(config)
        (before,) = by_key(config).values()
        fake_adapter.drafts = [
            self.page("protein-folding-panel", "Protein folding panel", "John Roe, Jane Doe")
        ]
        report = run(config, at=REFERENCE_NOW + timedelta(days=1))
        (after,) = by_key(config).values()
        assert after.uid == before.uid
        assert (report.counts.added, report.counts.missing) == (0, 0)

    def test_without_a_speaker_the_new_address_is_a_new_talk(
        self, config: AppConfig, fake_adapter: FakeAdapter
    ) -> None:
        fake_adapter.drafts = [draft("sites-physics", None, url=f"{DEPT_URL}/tba", title="TBA")]
        run(config)
        fake_adapter.drafts = [
            draft("sites-physics", None, url=f"{DEPT_URL}/knots", title="Knots and braids")
        ]
        report = run(config, at=REFERENCE_NOW + timedelta(days=1))
        assert (report.counts.added, report.counts.missing) == (1, 1)
