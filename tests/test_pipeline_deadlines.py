"""The deadlines of a run: read from the registry, reconciled with data/deadlines.json.

``seminars.pipeline.deadlines`` turns the milestones of every enabled registry source
into deadlines and compares them with the stored ones; ``seminars.pipeline.run``
stores them with the data files of the run and hands them to the feed writer; the
state, the output checks and ``seminars validate`` cover the file like the other data
files.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from deadline_samples import (
    LATER,
    SCHOOL,
    SCHOOL_UID,
    SYMPOSIUM,
    SYMPOSIUM_DATES_URL,
    SYMPOSIUM_UIDS,
    SYMPOSIUM_URL,
    make_deadline,
    registry_source,
)
from factories import REFERENCE_NOW
from fakes import (
    FakeClock,
    FakeEventFilter,
    FakeFieldTagger,
    FakeHttpClient,
    FakePicksScorer,
    FakeSecretScanner,
)
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig, load_sources
from seminars.models import Deadline, LastRun, dump_deadlines, write_model
from seminars.pipeline.deadlines import (
    ConfiguredDeadlines,
    configured_deadlines,
    edition_deadlines,
    reconcile_deadlines,
)
from seminars.pipeline.publish import validate_project
from seminars.pipeline.run import RunOptions, output_target, run_pipeline
from seminars.pipeline.secret_scan import build_secret_scanner
from seminars.pipeline.services import Services
from seminars.pipeline.store import (
    State,
    StoreError,
    data_files,
    load_state,
    read_deadlines_file,
    state_files,
)
from seminars.sources.base import AdapterRegistry
from seminars.sources.custom.registry import ADAPTER, Meeting, RegistryAdapter, registry_editions

ROOT = Path(__file__).resolve().parents[1]
SINCE = date(2026, 10, 27)
"""The first day of the window of a run at REFERENCE_NOW (14 days back)."""


def symposium(**overrides: Any) -> dict[str, Any]:
    return {**SYMPOSIUM, **overrides}


def milestone(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {"kind": "paper", "date": date(2027, 1, 10), "label": "Paper"}
    return {**values, **overrides}


def configured(*meetings: dict[str, Any], now: Any = REFERENCE_NOW) -> ConfiguredDeadlines:
    return configured_deadlines([registry_source(*meetings)], now=now)


def uids(deadlines: Sequence[Deadline]) -> list[str]:
    return [deadline.uid for deadline in deadlines]


class TestEditionDeadlines:
    def test_one_deadline_per_milestone_with_the_facts_of_its_edition(self) -> None:
        meeting = Meeting.model_validate(SYMPOSIUM)
        deadlines = edition_deadlines(meeting, now=REFERENCE_NOW)
        assert len(deadlines) == len(SYMPOSIUM["milestones"])
        scholarship = next(item for item in deadlines if item.kind == "scholarship")
        assert scholarship == make_deadline()

    def test_a_later_milestone_of_a_kind_is_numbered_by_date(self) -> None:
        later = milestone(date=date(2027, 2, 1), label="Second round")
        earlier = milestone(date=date(2027, 1, 10), label="First round")
        same_day = milestone(date=date(2027, 2, 1), time="12:00", label="Noon round")
        meeting = Meeting.model_validate(symposium(milestones=[later, same_day, earlier]))
        numbered = {item.label: item.uid for item in edition_deadlines(meeting, now=LATER)}
        assert numbered == {
            "First round": "dl.example-symposium-2027.paper",
            "Second round": "dl.example-symposium-2027.paper-2",
            "Noon round": "dl.example-symposium-2027.paper-3",
        }

    def test_the_stamps_are_the_time_of_the_run_in_utc(self) -> None:
        meeting = Meeting.model_validate(SCHOOL)
        (deadline,) = edition_deadlines(meeting, now=LATER.astimezone())
        assert (deadline.first_seen, deadline.last_changed, deadline.sequence) == (LATER, LATER, 0)
        assert deadline.first_seen.utcoffset() == timedelta(0)
        assert (deadline.edition_short, deadline.region) == ("Example School", "world")
        assert (deadline.time, deadline.zone) == ("12:00", "Europe/Zurich")


class TestConfiguredDeadlines:
    def test_every_enabled_registry_source_bay_area_and_world_alike(self) -> None:
        result = configured(SYMPOSIUM, SCHOOL)
        assert result.complete
        assert result.warnings == ()
        assert SCHOOL_UID in uids(result.deadlines)
        assert len(result.deadlines) == len(SYMPOSIUM["milestones"]) + 1
        assert list(result.deadlines) == sorted(
            result.deadlines, key=lambda item: (item.date, item.uid)
        )

    def test_editions_of_one_series_and_year_are_named_by_their_titles(self) -> None:
        spring = symposium(id="example-symposium-spring-2027", title="Example Spring Symposium")
        fall = symposium(id="example-symposium-fall-2027", title="Example Fall Symposium")
        result = configured(spring, fall, SCHOOL)
        names = {item.edition_id: item.edition_short for item in result.deadlines}
        assert names == {
            "example-symposium-spring-2027": "Example Spring Symposium",
            "example-symposium-fall-2027": "Example Fall Symposium",
            "example-school-2027": "Example School",
        }

    def test_the_page_of_the_check_is_kept_beside_the_page_of_the_edition(self) -> None:
        pages = {(item.url, item.verified_url) for item in configured(SYMPOSIUM).deadlines}
        assert pages == {(SYMPOSIUM_URL, SYMPOSIUM_DATES_URL)}

    def test_disabled_sources_and_other_adapters_give_nothing(self) -> None:
        off = registry_source(SYMPOSIUM, enabled=False)
        other = registry_source(SYMPOSIUM, id="conf-other", adapter="html_list")
        assert configured_deadlines([off, other], now=REFERENCE_NOW) == ConfiguredDeadlines()

    def test_params_that_cannot_be_read_say_nothing(self) -> None:
        broken = registry_source({**SYMPOSIUM, "start": "soon"}, id="conf-broken")
        result = configured_deadlines([broken, registry_source(SCHOOL)], now=REFERENCE_NOW)
        assert not result.complete
        assert uids(result.deadlines) == [SCHOOL_UID]
        assert result.warnings == (
            "the milestones of source conf-broken could not be read (AdapterConfigError); "
            "its stored deadlines stay",
        )

    def test_two_registries_that_give_one_uid_keep_the_first(self) -> None:
        first = registry_source(SCHOOL)
        second = registry_source({**SCHOOL, "title": "Copy"}, id="conf-copy")
        result = configured_deadlines([first, second], now=REFERENCE_NOW)
        assert [item.edition_title for item in result.deadlines] == [SCHOOL["title"]]
        assert result.warnings == (
            "source conf-copy gives 1 deadline uid(s) that an earlier registry source gives "
            f"already (the first is {SCHOOL_UID}); they are left out",
        )


class TestCommittedRegistry:
    def test_every_milestone_becomes_a_deadline(self) -> None:
        sources = [
            source
            for source in load_sources(ROOT / "config" / "sources.d")
            if source.enabled and source.adapter == ADAPTER
        ]
        result = configured_deadlines(sources, now=REFERENCE_NOW)
        assert sources
        assert result.complete
        assert result.warnings == ()
        milestones = sum(
            len(meeting.milestones) for source in sources for meeting in registry_editions(source)
        )
        assert len(result.deadlines) == milestones


class TestReconcile:
    def run(self, stored: Sequence[Deadline], *meetings: dict[str, Any]) -> tuple[Deadline, ...]:
        return reconcile_deadlines(stored, configured(*meetings, now=LATER), now=LATER, since=SINCE)

    def test_new_deadlines_start_at_sequence_zero(self) -> None:
        result = self.run((), SYMPOSIUM)
        assert uids(result) == list(SYMPOSIUM_UIDS)
        assert all(item.first_seen == item.last_changed == LATER for item in result)
        assert all(item.sequence == 0 for item in result)

    def test_an_unchanged_deadline_stays_as_stored(self) -> None:
        stored = make_deadline(sequence=3)
        result = self.run([stored], SYMPOSIUM)
        assert result[0] is stored

    @pytest.mark.parametrize(
        ("change", "fact"),
        [
            ({"date": date(2026, 11, 22)}, "date"),
            ({"time": "17:00"}, "time"),
            ({"zone": "PT"}, "zone"),
            ({"qualifier": "by"}, "qualifier"),
            ({"label": "Scholarship application deadline"}, "label"),
        ],
    )
    def test_a_changed_fact_of_the_milestone_is_a_new_sequence(
        self, change: dict[str, Any], fact: str
    ) -> None:
        stored = make_deadline(sequence=2)
        moved = [
            {**item, **change} if item["kind"] == "scholarship" else item
            for item in SYMPOSIUM["milestones"]
        ]
        result = {item.uid: item for item in self.run([stored], symposium(milestones=moved))}
        updated = result[stored.uid]
        assert getattr(updated, fact) == change[fact]
        assert (updated.sequence, updated.first_seen, updated.last_changed) == (
            3,
            REFERENCE_NOW,
            LATER,
        )

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param({"title": "Example Symposium 2027: Design of Proteins"}, id="title"),
            pytest.param({"short_title": "Symposium"}, id="short_title"),
            pytest.param({"series": "Example Meeting"}, id="series_of_the_name"),
            pytest.param({"url": "https://symposium.example.org/2027/program"}, id="url"),
            pytest.param({"location": "Example Center, Oakland, CA"}, id="location"),
            pytest.param({"start": date(2027, 3, 28)}, id="start"),
            pytest.param({"end": date(2027, 4, 2)}, id="end"),
            pytest.param(
                {"verified": {"date": date(2026, 11, 16), "url": SYMPOSIUM_DATES_URL}},
                id="verified_on",
            ),
            pytest.param(
                {"verified": {"date": date(2026, 11, 9), "url": SYMPOSIUM_URL}},
                id="verified_url",
            ),
        ],
    )
    def test_a_changed_fact_of_the_edition_is_a_new_sequence(self, change: dict[str, Any]) -> None:
        stored = make_deadline()
        result = {item.uid: item for item in self.run([stored], symposium(**change))}
        assert result[stored.uid].sequence == 1
        assert result[stored.uid].last_changed == LATER

    @pytest.mark.parametrize(
        ("change", "stored_name"),
        [
            pytest.param({"fields": ["bio"]}, "Example Symposium 2027", id="fields"),
            pytest.param({"region": "world"}, "Example Symposium 2027", id="region"),
            pytest.param(
                {"short_title": "Symposium", "series": "Example Meeting"},
                "Symposium",
                id="series_beside_a_short_title",
            ),
        ],
    )
    def test_a_fact_that_nothing_shows_is_stored_without_a_new_sequence(
        self, change: dict[str, Any], stored_name: str
    ) -> None:
        stored = make_deadline(edition_short=stored_name, sequence=2)
        result = {item.uid: item for item in self.run([stored], symposium(**change))}
        kept = result[stored.uid]
        assert (kept.sequence, kept.first_seen, kept.last_changed) == (
            2,
            REFERENCE_NOW,
            REFERENCE_NOW,
        )
        assert kept.shown() == stored.shown()
        assert {name: getattr(kept, name) for name in change if name != "short_title"} == {
            name: value for name, value in change.items() if name != "short_title"
        }

    def test_a_milestone_that_left_the_registry_leaves_the_file(self) -> None:
        stored = make_deadline()
        kept = [item for item in SYMPOSIUM["milestones"] if item["kind"] != "scholarship"]
        result = self.run([stored], symposium(milestones=kept))
        assert stored.uid not in uids(result)

    def test_a_deadline_before_the_window_leaves_the_file(self) -> None:
        stored = make_deadline()
        result = reconcile_deadlines(
            [stored], configured(SYMPOSIUM), now=LATER, since=date(2026, 11, 21)
        )
        assert stored.uid not in uids(result)
        kept = reconcile_deadlines(
            [stored], configured(SYMPOSIUM), now=LATER, since=date(2026, 11, 20)
        )
        assert stored.uid in uids(kept)

    def test_an_unreadable_registry_keeps_what_is_stored(self) -> None:
        stored = make_deadline()
        old = make_deadline(
            uid="dl.example-symposium-2027.other", kind="other", date=date(2026, 10, 1)
        )
        nothing = ConfiguredDeadlines(complete=False)
        result = reconcile_deadlines([stored, old], nothing, now=LATER, since=SINCE)
        assert result == (stored,)


# --- a whole run --------------------------------------------------------------------------


@pytest.fixture
def config(app_config: AppConfig, fake_registry: AdapterRegistry) -> AppConfig:
    fake_registry.register(ADAPTER)(RegistryAdapter)
    return dataclasses.replace(app_config, sources=(registry_source(SYMPOSIUM, SCHOOL),))


def services(emitters: FakeEmitters, **overrides: Any) -> Services:
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


def run(config: AppConfig, emitters: FakeEmitters, *, now: Any = REFERENCE_NOW, **options: Any):
    return run_pipeline(
        config, services(emitters), now=now, options=RunOptions(**options), timer=FakeClock()
    )


def with_sources(config: AppConfig, *meetings: dict[str, Any]) -> AppConfig:
    return dataclasses.replace(config, sources=(registry_source(*meetings),))


class TestRun:
    def test_a_run_stores_the_deadlines_and_hands_them_to_the_feed_writer(
        self, config: AppConfig
    ) -> None:
        emitters = FakeEmitters()
        report = run(config, emitters)
        assert report.ok
        stored = read_deadlines_file(config.paths.deadlines_file)
        assert uids(stored) == [
            SYMPOSIUM_UIDS[0],
            SYMPOSIUM_UIDS[1],
            SCHOOL_UID,
            *SYMPOSIUM_UIDS[2:],
        ]
        feeds = [call for name, call in emitters.calls if name == "write_ics_feeds"]
        assert feeds[-1]["deadlines"] == len(stored)
        feed = config.paths.public_feeds_dir / "deadlines.ics"
        assert f"X-FAKE-DEADLINES:{len(stored)}" in feed.read_text(encoding="utf-8")
        assert config.paths.deadlines_file.read_bytes().isascii()

    def test_the_next_run_keeps_the_stamps_and_counts_a_change(self, config: AppConfig) -> None:
        run(config, FakeEmitters())
        moved = [
            {**item, "time": "17:00"} if item["kind"] == "scholarship" else item
            for item in SYMPOSIUM["milestones"]
        ]
        run(with_sources(config, symposium(milestones=moved), SCHOOL), FakeEmitters(), now=LATER)
        stored = {item.uid: item for item in read_deadlines_file(config.paths.deadlines_file)}
        assert stored[SCHOOL_UID].last_changed == REFERENCE_NOW
        assert stored[SCHOOL_UID].sequence == 0
        changed = stored["dl.example-symposium-2027.scholarship"]
        assert (changed.first_seen, changed.last_changed, changed.sequence) == (
            REFERENCE_NOW,
            LATER,
            1,
        )

    def test_the_window_starts_on_the_local_day_of_the_run(self, config: AppConfig) -> None:
        """06:00 UTC on 2026-11-10 is 22:00 on 2026-11-09 in the zone of the settings."""
        assert (config.settings.timezone, config.settings.window.days_back) == (
            "America/Los_Angeles",
            14,
        )
        late_evening = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
        edge = {
            **SCHOOL,
            "milestones": [
                {"kind": "other", "date": date(2026, 10, 25), "label": "Fifteen days before"},
                {"kind": "application", "date": date(2026, 10, 26), "label": "Fourteen days"},
                {"kind": "notification", "date": date(2026, 10, 27), "label": "Thirteen days"},
            ],
        }
        run(with_sources(config, edge), FakeEmitters(), now=late_evening)
        stored = read_deadlines_file(config.paths.deadlines_file)
        assert [item.label for item in stored] == ["Fourteen days", "Thirteen days"]

    def test_deadlines_need_no_source_of_the_run(self, config: AppConfig) -> None:
        events_only = dataclasses.replace(
            config,
            sources=(
                *config.sources,
                registry_source(id="conf-other", params={"allow_empty": True, "meetings": []}),
            ),
        )
        run(events_only, FakeEmitters(), source_ids=("conf-other",))
        stored = read_deadlines_file(config.paths.deadlines_file)
        assert SCHOOL_UID in uids(stored)

    def test_an_unreadable_registry_is_a_warning_and_keeps_the_stored_deadlines(
        self, config: AppConfig
    ) -> None:
        run(config, FakeEmitters())
        before = read_deadlines_file(config.paths.deadlines_file)
        broken = with_sources(config, symposium(start="soon"))
        report = run(broken, FakeEmitters(), now=LATER)
        assert (
            "the milestones of source conf-registry could not be read (AdapterConfigError); "
            "its stored deadlines stay"
        ) in report.warnings
        assert read_deadlines_file(config.paths.deadlines_file) == before

    def test_a_dry_run_writes_its_deadlines_into_the_scratch_tree_only(
        self, config: AppConfig
    ) -> None:
        with output_target(config.paths, dry_run=True) as target:
            run_pipeline(
                config,
                services(FakeEmitters()),
                now=REFERENCE_NOW,
                options=RunOptions(dry_run=True),
                target=target,
                timer=FakeClock(),
            )
            assert SCHOOL_UID in uids(read_deadlines_file(target.deadlines_file))
        assert not config.paths.deadlines_file.exists()

    def test_an_offline_run_stores_its_deadlines_and_a_live_run_starts_again(
        self, config: AppConfig
    ) -> None:
        run(config, FakeEmitters(), offline=True)
        assert config.paths.deadlines_file in state_files(config.paths)
        offline = read_deadlines_file(config.paths.deadlines_file)
        assert all(item.first_seen == REFERENCE_NOW for item in offline)
        run(config, FakeEmitters(), now=LATER)
        live = read_deadlines_file(config.paths.deadlines_file)
        assert uids(live) == uids(offline)
        assert all(item.first_seen == LATER for item in live)

    def test_a_secret_in_a_deadline_stops_the_data_files(self, config: AppConfig) -> None:
        scanner = FakeSecretScanner(["Example Hall"])
        report = run_pipeline(
            config,
            services(FakeEmitters(), secret_scanner=scanner),
            now=REFERENCE_NOW,
            timer=FakeClock(),
        )
        assert not report.ok
        assert any(f.origin == "data/deadlines.json" for f in report.secret_findings)
        assert not config.paths.deadlines_file.exists()


class TestState:
    def test_a_missing_file_is_no_deadline(self, app_config: AppConfig) -> None:
        assert load_state(app_config.paths).deadlines == ()
        assert State().deadlines == ()

    def test_the_file_is_read_and_written_with_the_data_files(self, app_config: AppConfig) -> None:
        paths = app_config.paths
        files = data_files(
            paths,
            events=[],
            uid_map=load_state(paths).uid_map,
            health=load_state(paths).health,
            deadlines=[make_deadline()],
        )
        assert files[paths.deadlines_file] == dump_deadlines([make_deadline()])
        paths.data_dir.mkdir()
        paths.deadlines_file.write_text(files[paths.deadlines_file], encoding="utf-8")
        assert load_state(paths).deadlines == (make_deadline(),)

    def test_a_damaged_file_is_an_error_that_names_it(self, app_config: AppConfig) -> None:
        paths = app_config.paths
        paths.data_dir.mkdir()
        paths.deadlines_file.write_text('{"schema_version": 1, "deadlines": [{}]}\n', "utf-8")
        with pytest.raises(StoreError, match=r"deadlines\.json: cannot be read"):
            load_state(paths)

    def test_validate_reads_the_file(self, app_config: AppConfig) -> None:
        paths = app_config.paths
        paths.data_dir.mkdir()
        write_model(paths.last_run_file, LastRun.model_validate(_last_run()))
        paths.deadlines_file.write_text("{broken\n", encoding="utf-8")
        result = validate_project(
            paths, validator=FakeEmitters().validate_ics, scanner=FakeSecretScanner()
        )
        damaged = [problem for problem in result.problems if "deadlines.json" in problem]
        assert len(damaged) == 1
        assert damaged[0].endswith("data/deadlines.json: cannot be read (JSONDecodeError)")


def _last_run() -> dict[str, Any]:
    return {
        "started_at": REFERENCE_NOW,
        "finished_at": REFERENCE_NOW,
        "mode": "live",
        "ok": True,
    }
