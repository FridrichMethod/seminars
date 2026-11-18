"""Tests for the registry of conferences and workshops kept by hand (adapter ``registry``).

The registry is the source ``conf-registry`` of config/sources.d/conferences.yaml. Its
adapter requests nothing: every meeting is an entry of its params, checked against the
organizer's page. The tests cover the schema of an entry, the drafts a run gets, what
the pipeline makes of them (one event per edition, joined with a listing of the same
page that gives its hours, removed after the usual missing runs unless one change
deletes most of the entries, healthy when every meeting is over), and the state of the
committed registry on the day the tests run.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from factories import PACIFIC, REFERENCE_NOW, make_draft, make_source
from fakes import (
    FakeAdapter,
    FakeClock,
    FakeEventFilter,
    FakeFieldTagger,
    FakeHttpClient,
    FakeIssueClient,
    FakePicksScorer,
)
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig, load_settings, load_sources
from seminars.models import FIELD_IDS, EventDraft, RunReport, SourceConfig, TimeWindow
from seminars.models.health import HEALTH_HISTORY_LIMIT
from seminars.outputs.health import allows_empty, source_issue_title, update_health
from seminars.pipeline.collect import SHRINK_MIN_RUNS
from seminars.pipeline.normalize import NormalizeContext, normalize_drafts
from seminars.pipeline.run import RunOptions, run_pipeline
from seminars.pipeline.secret_scan import build_secret_scanner
from seminars.pipeline.services import Services
from seminars.pipeline.store import read_events_file
from seminars.sources.base import AdapterRegistry, get_adapter
from seminars.sources.custom.registry import (
    MAX_MEETING_DAYS,
    MILESTONE_KINDS,
    RECHECK_AFTER_DAYS,
    RECHECK_WITHIN_DAYS,
    Meeting,
    RegistryAdapter,
    RegistryParams,
    curation_problems,
)
from seminars.sources.generic.common import AdapterConfigError, load_params

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_ID = "conf-registry"
REGISTRY_URL = (
    "https://github.com/FridrichMethod/seminars/blob/main/config/sources.d/conferences.yaml"
)
SETTINGS = load_settings(ROOT / "config" / "settings.yaml")
MEETING_WINDOW = SETTINGS.meeting_window_at(REFERENCE_NOW)
"""The window a run on 2026-11-10 fetches the registry with: 2026-10-27 to 2027-11-11."""
PAGE = "https://qbi.example.org/events/symposium-2027"


def production() -> SourceConfig:
    sources = load_sources(ROOT / "config" / "sources.d")
    return next(source for source in sources if source.id == REGISTRY_ID)


def meeting(**overrides: Any) -> dict[str, Any]:
    """One registry entry as YAML gives it: a two-day symposium in January 2027."""
    values: dict[str, Any] = {
        "id": "qbi-symposium-2027",
        "title": "Computational Biology, Drug Discovery and AI",
        "series": "QBI symposia",
        "host_org": "UCSF Quantitative Biosciences Institute (QBI)",
        "start": date(2027, 1, 7),
        "end": date(2027, 1, 8),
        "region": "bay-area",
        "location": "UCSF Mission Bay, San Francisco, CA",
        "url": PAGE,
        "fields": ["bio", "med"],
        "event_type": "conference",
        "status": "confirmed",
        "verified": {"date": date(2026, 11, 1), "url": PAGE},
    }
    return {**values, **overrides}


def registry(*meetings: dict[str, Any], allow_empty: bool = True) -> SourceConfig:
    return make_source(
        id=REGISTRY_ID,
        name="Conference registry",
        group="conferences",
        adapter="registry",
        url=REGISTRY_URL,
        institution="other",
        host_org="Conference registry",
        default_fields=[],
        default_event_type="conference",
        meeting_horizon=True,
        params={"allow_empty": allow_empty, "meetings": list(meetings)},
    )


def drafts(source: SourceConfig, window: TimeWindow = MEETING_WINDOW) -> list[EventDraft]:
    adapter = RegistryAdapter()
    return adapter.parse(adapter.fetch(FakeHttpClient(), source, window=window), source)


def problem(*entries: dict[str, Any]) -> str:
    with pytest.raises(AdapterConfigError) as caught:
        load_params(RegistryParams, registry(*entries))
    return str(caught.value)


# --- the committed registry -----------------------------------------------------------------


class TestTheCommittedRegistry:
    def test_the_entry_describes_a_source_that_fetches_nothing(self) -> None:
        source = production()
        assert (source.group, source.adapter, source.institution) == (
            "conferences",
            "registry",
            "other",
        )
        assert source.url == REGISTRY_URL
        assert source.meeting_horizon
        assert allows_empty(source)
        assert isinstance(get_adapter(source.adapter), RegistryAdapter)

    def test_every_meeting_fits_the_schema(self) -> None:
        params = load_params(RegistryParams, production())
        assert params.meetings
        for entry in params.meetings:
            assert set(entry.fields) <= set(FIELD_IDS), entry.id
            assert entry.url.startswith("https://"), entry.id
            assert entry.verified.url.startswith("https://"), entry.id
            assert entry.id.endswith(str(entry.start.year)), entry.id

    def test_no_meeting_needs_the_curator_today(self) -> None:
        # This test depends on the day it runs on, by design: it fails when an edition
        # that starts within RECHECK_WITHIN_DAYS was verified more than RECHECK_AFTER_DAYS
        # ago, or when no Bay Area edition is ahead. Check the organizer's pages of the
        # meetings it names and update "verified" (docs/ADDING_A_SOURCE.md).
        today = datetime.now(UTC).astimezone(ZoneInfo(production().timezone)).date()
        meetings = load_params(RegistryParams, production()).meetings
        assert curation_problems(meetings, today) == []

    def test_a_run_gets_every_bay_area_meeting_that_is_not_over(self) -> None:
        source = production()
        window = SETTINGS.meeting_window_at(datetime.now(UTC))
        meetings = load_params(RegistryParams, source).meetings
        first = window.start.astimezone(ZoneInfo(source.timezone)).date()
        expected = [m.id for m in meetings if m.region == "bay-area" and m.end >= first]
        assert [draft.native_id for draft in drafts(source, window)] == expected


# --- the schema of an entry -----------------------------------------------------------------


class TestSchema:
    def test_a_complete_entry_is_read(self) -> None:
        entry = meeting(
            milestones=[
                {
                    "kind": "poster",
                    "date": date(2026, 12, 1),
                    "time": "23:59",
                    "zone": "PT",
                    "label": "Poster deadline",
                },
                {
                    "kind": "registration_open",
                    "date": date(2026, 12, 2),
                    "qualifier": "by",
                    "label": "Registration opens",
                },
            ],
            notes=["Dates from the detail page; the list card shows one day."],
        )
        (parsed,) = load_params(RegistryParams, registry(entry)).meetings
        assert parsed.id == "qbi-symposium-2027"
        assert [milestone.line for milestone in parsed.milestones] == [
            "Poster deadline: 2026-12-01 23:59 PT",
            "Registration opens: by 2026-12-02",
        ]

    def test_the_milestone_kinds_are_a_closed_vocabulary(self) -> None:
        assert "workshop_paper" in MILESTONE_KINDS
        message = problem(
            meeting(milestones=[{"kind": "keynote", "date": date(2026, 12, 1), "label": "x"}])
        )
        assert "params.meetings.0.milestones.0.kind: Input should be 'abstract'" in message

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"id": "qbi-symposium-2028"}, "the id ends in 2028, the edition starts in 2027"),
            ({"id": "qbi-symposium"}, "params.meetings.0.id"),
            ({"id": "QBI-symposium-2027"}, "params.meetings.0.id"),
            ({"id": "q" * 59 + "-2027"}, "params.meetings.0.id"),
            ({"end": date(2027, 1, 6)}, "end is before start"),
            ({"end": date(2027, 2, 8)}, f"more than {MAX_MEETING_DAYS} days after start"),
            ({"region": "online"}, "params.meetings.0.region"),
            ({"event_type": "seminar"}, "params.meetings.0.event_type"),
            ({"status": "cancelled"}, "params.meetings.0.status"),
            ({"fields": []}, "needs at least one field"),
            ({"fields": ["biology"]}, "unknown field id"),
            ({"url": "http://qbi.example.org/events/symposium-2027"}, "https address"),
            ({"url": "https://qbi.example.org/e?token=abc"}, "credential"),
            ({"title": "Symposium on Prote\u00efns"}, "printable ASCII"),
            ({"title": "Two\nlines"}, "printable ASCII"),
            ({"location": " San Francisco"}, "white space"),
            ({"description": "Copied text"}, "params.meetings.0.description: unknown key"),
            ({"verified": {"date": date(2026, 11, 1)}}, "params.meetings.0.verified.url"),
        ],
    )
    def test_an_entry_that_breaks_a_rule_is_a_config_error(
        self, overrides: dict[str, Any], message: str
    ) -> None:
        assert message in problem(meeting(**overrides))

    @pytest.mark.parametrize(
        ("milestone", "message"),
        [
            ({"date": date(2027, 1, 9)}, "after the end of the edition: Deadline"),
            ({"time": "23:59", "zone": "PST"}, "zone"),
            ({"zone": "PST"}, "zone"),
            ({"time": "24:00"}, "params.meetings.0.milestones.0.time"),
            ({"time": 1439}, "write the time in quotes"),
            ({"qualifier": True}, "write exact or by (YAML reads an unquoted on"),
            ({"qualifier": "on"}, "params.meetings.0.milestones.0.qualifier"),
            ({"label": ""}, "params.meetings.0.milestones.0.label"),
        ],
    )
    def test_a_milestone_that_breaks_a_rule_is_a_config_error(
        self, milestone: dict[str, Any], message: str
    ) -> None:
        values = {"kind": "paper", "date": date(2026, 12, 1), "label": "Deadline", **milestone}
        assert message in problem(meeting(milestones=[values]))

    @pytest.mark.parametrize("zone", ["AoE", "PT", "MT", "ET", "UTC", "Europe/Berlin"])
    def test_a_milestone_names_a_known_zone(self, zone: str) -> None:
        values = {"kind": "paper", "date": date(2026, 12, 1), "time": "12:00", "zone": zone}
        entry = meeting(milestones=[{**values, "label": "Deadline"}])
        (parsed,) = load_params(RegistryParams, registry(entry)).meetings
        assert parsed.milestones[0].zone == zone

    def test_a_zone_without_a_time_is_the_zone_of_the_day(self) -> None:
        values = {"kind": "paper", "date": date(2026, 12, 1), "zone": "AoE", "label": "Deadline"}
        (parsed,) = load_params(RegistryParams, registry(meeting(milestones=[values]))).meetings
        assert [milestone.line for milestone in parsed.milestones] == ["Deadline: 2026-12-01 AoE"]

    def test_the_yaml_traps_are_errors_that_say_what_to_write(self) -> None:
        # YAML 1.1 reads "on" as true and 23:59 as the number 1439.
        written = yaml.safe_load(
            "verified: {on: 2026-11-01, url: 'https://qbi.example.org/'}\n"
            "milestones: [{kind: paper, date: 2026-12-01, time: 23:59, label: Deadline}]\n"
        )
        message = problem(meeting(**written))
        assert "params.meetings.0.verified: Value error, a key is true or false" in message
        assert "write the time in quotes ('23:59')" in message

    def test_an_id_is_used_once(self) -> None:
        second = meeting(title="Another symposium")
        assert "used more than once: qbi-symposium-2027" in problem(meeting(), second)

    def test_the_registry_may_be_empty(self) -> None:
        assert load_params(RegistryParams, registry()).meetings == []

    def test_unknown_params_are_errors(self) -> None:
        source = registry(meeting()).evolve(params={"meetings": [], "entries": []})
        with pytest.raises(AdapterConfigError, match=r"params\.entries: unknown key"):
            load_params(RegistryParams, source)


# --- what a run gets ------------------------------------------------------------------------


class TestFetchAndParse:
    def test_fetch_requests_nothing(self) -> None:
        http = FakeHttpClient()
        source = registry(meeting())
        payload = RegistryAdapter().fetch(http, source, window=MEETING_WINDOW)
        assert http.requests == []
        assert payload.documents == []
        assert (payload.source_id, payload.window) == (REGISTRY_ID, MEETING_WINDOW)
        assert payload.fetched_at == MEETING_WINDOW.start

    def test_fetch_checks_the_params(self) -> None:
        source = registry(meeting(region="online"))
        with pytest.raises(AdapterConfigError):
            RegistryAdapter().fetch(FakeHttpClient(), source, window=MEETING_WINDOW)

    def test_a_meeting_is_an_all_day_draft_with_its_facts(self) -> None:
        (draft,) = drafts(registry(meeting(status="tentative")))
        assert draft.model_dump(exclude_defaults=True) == {
            "source_id": REGISTRY_ID,
            "extraction": "api",
            "native_id": "qbi-symposium-2027",
            "url": PAGE,
            "title": "Computational Biology, Drug Discovery and AI",
            "start": date(2027, 1, 7),
            "end": date(2027, 1, 8),
            "all_day": True,
            "series": "QBI symposia",
            "host_org": "UCSF Quantitative Biosciences Institute (QBI)",
            "location": "UCSF Mission Bay, San Francisco, CA",
            "field_hints": ["bio", "med"],
            "event_type_hint": "conference",
            "status_hint": "tentative",
        }

    def test_the_abstract_lists_the_milestones_from_the_first_day_of_the_window(self) -> None:
        first = MEETING_WINDOW.start.date()
        assert first == date(2026, 10, 27)
        milestones = [
            {"kind": "registration", "date": date(2027, 1, 8), "label": "Registration deadline"},
            {"kind": "paper", "date": first - timedelta(days=1), "label": "Passed"},
            {"kind": "poster", "date": first, "time": "17:00", "zone": "PT", "label": "Posters"},
            {"kind": "abstract", "date": first, "time": "09:00", "label": "Abstracts"},
            {
                "kind": "registration_open",
                "date": date(2026, 12, 1),
                "qualifier": "by",
                "label": "Registration opens",
            },
        ]
        (draft,) = drafts(registry(meeting(milestones=milestones)))
        assert draft.abstract == (
            "Abstracts: 2026-10-27 09:00\n"
            "Posters: 2026-10-27 17:00 PT\n"
            "Registration opens: by 2026-12-01\n"
            "Registration deadline: 2027-01-08"
        )

    def test_notes_are_never_published(self) -> None:
        (draft,) = drafts(registry(meeting(notes=["Ask the organizers about the venue."])))
        assert draft.abstract is None
        assert "venue" not in draft.model_dump_json()

    def test_only_bay_area_meetings_that_are_not_over_are_listed(self) -> None:
        first = MEETING_WINDOW.start.date()
        entries = [
            meeting(id="over-2026", start=date(2026, 10, 20), end=first - timedelta(days=1)),
            meeting(id="last-day-2026", start=date(2026, 10, 20), end=first),
            meeting(id="world-2027", region="world"),
            meeting(id="far-ahead-2028", start=date(2028, 1, 10), end=date(2028, 1, 12)),
            meeting(),
        ]
        assert [draft.native_id for draft in drafts(registry(*entries))] == [
            "last-day-2026",
            "far-ahead-2028",
            "qbi-symposium-2027",
        ]

    def test_a_registry_of_past_meetings_gives_no_draft(self) -> None:
        over = meeting(id="over-2026", start=date(2026, 6, 1), end=date(2026, 6, 3))
        assert drafts(registry(over)) == []


class TestNormalized:
    def context(self) -> NormalizeContext:
        return NormalizeContext(
            settings=SETTINGS, now=REFERENCE_NOW, window=SETTINGS.window_at(REFERENCE_NOW)
        )

    def test_every_listed_meeting_is_an_event_of_the_meeting_window(self) -> None:
        entries = [
            meeting(),
            meeting(id="biotech-day-2027", start=date(2027, 5, 17), end=date(2027, 5, 17)),
            meeting(id="far-ahead-2028", start=date(2028, 1, 10), end=date(2028, 1, 12)),
        ]
        source = registry(*entries)
        result = normalize_drafts(drafts(source), source, self.context())
        assert result.outside_window == 1  # beyond the meeting window, until it reaches it
        symposium, day = sorted((c.event for c in result.candidates), key=lambda e: e.start)
        assert symposium.uid == "conf-registry.qbi-symposium-2027"
        assert symposium.source_ids == ["conf-registry:qbi-symposium-2027"]
        assert (symposium.start, symposium.end) == (
            datetime(2027, 1, 7, tzinfo=PACIFIC),
            datetime(2027, 1, 9, tzinfo=PACIFIC),
        )
        assert symposium.all_day
        assert symposium.is_meeting
        assert symposium.host_org == "UCSF Quantitative Biosciences Institute (QBI)"
        assert symposium.speakers == []
        assert (day.uid, day.end, day.all_day, day.is_meeting) == (
            "conf-registry.biotech-day-2027",
            None,
            True,
            True,
        )

    def test_a_venue_to_be_announced_is_no_location(self) -> None:
        source = registry(meeting(location="To be announced", status="tentative"))
        (candidate,) = normalize_drafts(drafts(source), source, self.context()).candidates
        assert candidate.event.location is None
        assert candidate.event.status == "tentative"


# --- whole runs -----------------------------------------------------------------------------

CALENDAR_URL = "https://events.example.edu/api/2/events"


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


def run(config: AppConfig, day: int, **overrides: Any) -> RunReport:
    return run_pipeline(
        config,
        services(**overrides),
        now=REFERENCE_NOW + timedelta(days=day),
        options=RunOptions(),
        timer=FakeClock(),
    )


def with_sources(config: AppConfig, *sources: SourceConfig) -> AppConfig:
    return dataclasses.replace(config, sources=sources)


def calendar_listing(**overrides: Any) -> EventDraft:
    """The symposium as another source lists it: with its hours, under a title of its own.

    The listing spans the meeting, from the first day to the last, and links the
    organizer's page of the edition, as the host's own listing or a feed that gives
    that address does. A Localist calendar links its own page of the event
    (``localist_url``) instead, so its listing never shares the page of an entry.
    """
    values: dict[str, Any] = {
        "source_id": "bayarea-qbi",
        "extraction": "api",
        "native_id": "777",
        "url": PAGE,
        "title": "QBI-TAU Symposium: Computational Biology, Drug Discovery and AI",
        "start": datetime(2027, 1, 7, 9, 0, tzinfo=PACIFIC),
        "end": datetime(2027, 1, 8, 17, 0, tzinfo=PACIFIC),
        "speaker_text": None,
        "location": "Byers Hall",
        "abstract": "Two days of talks on computational biology.",
    }
    return make_draft(**{**values, **overrides})


@pytest.fixture
def calendar(fake_registry: AdapterRegistry) -> FakeAdapter:
    fake_registry.register("registry")(RegistryAdapter)
    return FakeAdapter().register(fake_registry, "calendar_like")


def calendar_source(source_id: str = "bayarea-qbi", *, aggregator: bool = False) -> SourceConfig:
    return make_source(
        id=source_id,
        group="bayarea",
        adapter="calendar_like",
        url=CALENDAR_URL,
        aggregator=aggregator,
        default_fields=["bio"],
    )


class TestWithAListingOfTheSameMeeting:
    @pytest.mark.parametrize(
        ("source_id", "aggregator", "all_day", "start", "end"),
        [
            # The host's own listing leads the merge and gives its hours.
            (
                "bayarea-qbi",
                False,
                False,
                datetime(2027, 1, 7, 9, 0, tzinfo=PACIFIC),
                datetime(2027, 1, 8, 17, 0, tzinfo=PACIFIC),
            ),
            # The entry leads an aggregator's listing and keeps the whole days.
            (
                "bayarea-calendar",
                True,
                True,
                datetime(2027, 1, 7, tzinfo=PACIFIC),
                datetime(2027, 1, 9, tzinfo=PACIFIC),
            ),
        ],
    )
    def test_a_listing_of_the_same_page_that_gives_the_hours_joins_the_entry(
        self,
        app_config: AppConfig,
        calendar: FakeAdapter,
        source_id: str,
        aggregator: bool,
        all_day: bool,
        start: datetime,
        end: datetime,
    ) -> None:
        other = calendar_source(source_id, aggregator=aggregator)
        config = with_sources(app_config, registry(meeting()), other)
        run(config, 0)
        (before,) = read_events_file(config.paths.events_file)
        calendar.drafts = [calendar_listing(source_id=source_id)]
        report = run(config, 1)
        (event,) = read_events_file(config.paths.events_file)
        assert report.counts.merged == 1
        assert event.uid == before.uid == "conf-registry.qbi-symposium-2027"
        assert sorted(event.source_ids) == [
            f"{source_id}:777",
            "conf-registry:qbi-symposium-2027",
        ]
        assert (event.all_day, event.start, event.end) == (all_day, start, end)

    def test_a_listing_of_another_page_stays_an_event_of_its_own(
        self, app_config: AppConfig, calendar: FakeAdapter
    ) -> None:
        config = with_sources(app_config, registry(meeting()), calendar_source())
        calendar.drafts = [calendar_listing(url="https://events.example.edu/event/777")]
        run(config, 0)
        events = read_events_file(config.paths.events_file)
        assert sorted(event.uid for event in events) == [
            "bayarea-qbi.777",
            "conf-registry.qbi-symposium-2027",
        ]

    def test_an_entry_whose_url_is_a_front_page_joins_no_listing(
        self, app_config: AppConfig, calendar: FakeAdapter
    ) -> None:
        # A front page is no detail page: the edition sites of NAACL and KDD are such.
        site = "https://2027.symposium.example.org/"
        config = with_sources(app_config, registry(meeting(url=site)), calendar_source())
        calendar.drafts = [calendar_listing(url=site)]
        run(config, 0)
        events = read_events_file(config.paths.events_file)
        assert sorted(event.uid for event in events) == [
            "bayarea-qbi.777",
            "conf-registry.qbi-symposium-2027",
        ]


class TestRemovalAndHealth:
    def test_a_deleted_entry_leaves_after_three_runs_without_it(
        self, app_config: AppConfig, calendar: FakeAdapter
    ) -> None:
        other = meeting(id="biotech-day-2027", start=date(2027, 1, 20), end=date(2027, 1, 20))
        run(with_sources(app_config, registry(meeting(), other)), 0)
        config = with_sources(app_config, registry(other))
        stored = []
        for day in (1, 2, 3):
            report = run(config, day)
            stored.append(sorted(event.uid for event in read_events_file(config.paths.events_file)))
        assert stored[1] == ["conf-registry.biotech-day-2027", "conf-registry.qbi-symposium-2027"]
        assert stored[2] == ["conf-registry.biotech-day-2027"]
        assert report.counts.removed == 1

    def test_a_deleted_entry_stays_while_the_registry_lists_no_meeting(
        self, app_config: AppConfig, calendar: FakeAdapter
    ) -> None:
        run(with_sources(app_config, registry(meeting())), 0)
        over = meeting(id="over-2026", start=date(2026, 6, 1), end=date(2026, 6, 3))
        config = with_sources(app_config, registry(over))
        reports = [run(config, day) for day in (1, 2, 3, 4)]
        assert [report.sources[0].status for report in reports] == ["empty"] * 4
        (event,) = read_events_file(config.paths.events_file)
        assert (event.uid, event.missing_runs) == ("conf-registry.qbi-symposium-2027", 0)

    def runs(
        self, app_config: AppConfig, kept: int, days: int
    ) -> tuple[list[RunReport], list[int], list[list[str]]]:
        """Runs on ``days`` days, ten editions listed for the first three, ``kept`` after."""
        entries = [
            meeting(
                id=f"symposium-{n}-2027", start=date(2027, 1, 10 + n), end=date(2027, 1, 10 + n)
            )
            for n in range(10)
        ]
        client = FakeIssueClient()
        reports, stored, opened = [], [], []
        for day in range(days):
            listed = entries if day < SHRINK_MIN_RUNS else entries[:kept]
            config = with_sources(app_config, registry(*listed))
            reports.append(run(config, day, update_health=update_health, issues=client))
            stored.append(len(read_events_file(config.paths.events_file)))
            opened.append(sorted(issue.title for issue in client.issues if issue.state == "open"))
        return reports, stored, opened

    def test_deleting_at_most_half_of_the_entries_removes_them_after_three_runs(
        self, app_config: AppConfig, calendar: FakeAdapter
    ) -> None:
        reports, stored, opened = self.runs(app_config, kept=5, days=6)
        assert all(report.sources[0].read_in_full for report in reports)
        assert stored == [10, 10, 10, 10, 10, 5]
        assert opened == [[]] * 6

    def test_deleting_most_entries_at_once_is_a_shrunk_listing_until_it_is_resized(
        self, app_config: AppConfig, calendar: FakeAdapter
    ) -> None:
        # Every run of a source is compared with its usual size (seminars.pipeline.collect):
        # four editions where there were ten is a listing that lost part of itself. Its
        # missing events are kept, and an issue opens, until the history holds too few
        # runs read in full for a measure and the run takes four as the new size.
        shrunk = HEALTH_HISTORY_LIMIT - SHRINK_MIN_RUNS + 1
        resized = SHRINK_MIN_RUNS + shrunk
        reports, stored, opened = self.runs(app_config, kept=4, days=resized + 3)
        assert [report.sources[0].shrank_from for report in reports] == (
            [None] * SHRINK_MIN_RUNS + [10] * shrunk + [None] * 3
        )
        assert [report.sources[0].resized_from for report in reports[resized:]] == [10, None, None]
        assert stored == [10] * (resized + 2) + [4]
        broken = [source_issue_title(REGISTRY_ID)]
        assert opened == [[]] * (SHRINK_MIN_RUNS + 2) + [broken] * (shrunk - 2) + [[]] * 3

    @pytest.mark.parametrize(("allow_empty", "issues"), [(True, []), (False, [REGISTRY_ID])])
    def test_a_registry_whose_meetings_are_over_is_healthy_with_allow_empty(
        self, app_config: AppConfig, calendar: FakeAdapter, allow_empty: bool, issues: list[str]
    ) -> None:
        soon = meeting(id="soon-2026", start=date(2026, 11, 12), end=date(2026, 11, 13))
        config = with_sources(app_config, registry(soon, allow_empty=allow_empty))
        client = FakeIssueClient()
        reports = [
            run(config, day, update_health=update_health, issues=client) for day in (0, 30, 31, 32)
        ]
        assert [report.sources[0].status for report in reports] == ["ok", *["empty"] * 3]
        assert [report.sources[0].drafts for report in reports] == [1, 0, 0, 0]
        opened = sorted(issue.title for issue in client.issues if issue.state == "open")
        assert opened == [source_issue_title(source) for source in issues]


# --- curation -------------------------------------------------------------------------------


class TestCurationProblems:
    TODAY = date(2027, 1, 10)

    def entries(self, **overrides: Any) -> list[Meeting]:
        return load_params(RegistryParams, registry(meeting(**overrides))).meetings

    def checked(self, days_ago: int, **overrides: Any) -> list[str]:
        verified = {"date": self.TODAY - timedelta(days=days_ago), "url": PAGE}
        return curation_problems(self.entries(verified=verified, **overrides), self.TODAY)

    def test_a_recent_check_is_enough(self) -> None:
        assert self.checked(RECHECK_AFTER_DAYS, start=date(2027, 2, 1), end=date(2027, 2, 2)) == []

    def test_an_old_check_of_a_meeting_that_comes_soon_is_stale(self) -> None:
        start = self.TODAY + timedelta(days=RECHECK_WITHIN_DAYS)
        found = self.checked(RECHECK_AFTER_DAYS + 1, id="soon-2027", start=start, end=start)
        assert found == [
            "soon-2027: starts on 2027-05-10 and was last verified on 2026-10-11; "
            "check the organizer's page again"
        ]

    def test_an_old_check_of_a_meeting_further_ahead_can_wait(self) -> None:
        start = self.TODAY + timedelta(days=RECHECK_WITHIN_DAYS + 1)
        assert self.checked(365, id="later-2027", start=start, end=start) == []

    def test_a_check_in_the_future_is_a_mistake(self) -> None:
        start, end = date(2027, 2, 1), date(2027, 2, 2)
        assert self.checked(-1, start=start, end=end) == [
            "qbi-symposium-2027: verified on 2027-01-11, after 2027-01-10"
        ]

    def test_a_registry_without_a_bay_area_meeting_ahead_needs_curation(self) -> None:
        over = self.checked(0, id="over-2026", start=date(2026, 12, 20), end=date(2027, 1, 9))
        world = self.checked(0, region="world", start=date(2027, 2, 1), end=date(2027, 2, 2))
        for found in (over, world):
            assert found == ["no bay-area meeting ends on or after today; curate the registry"]
