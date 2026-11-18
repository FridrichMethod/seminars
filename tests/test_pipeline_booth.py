"""A booth at a meeting leaves the stored events in the run that excludes it.

Biohub lists its stands at conferences among its events: "Biohub at NeurIPS
2026" is a booth in the exhibit hall in Sydney ("meet the Biohub team at Booth
72"). A card that the adapter skips never reaches the exclusions of a run, and
a stored event that no run delivers only counts towards its removal while its
source is healthy, which a quiet Biohub page is not. The event filter therefore
excludes a numbered booth (``seminars.classify.filters``): the uid of the
excluded event is among the exclusions of the run, and the stored booth is
removed in that run, also when the booth is the only card the source lists.

Every run here uses the real implementations and the shipped configuration;
only the pages come from memory, made from the recorded Biohub pages.
"""

from __future__ import annotations

import dataclasses
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fakes import FakeClock, FakeEventFilter, FakeHttpClient
from seminars.config import AppConfig, load_config
from seminars.models import Event, RunReport
from seminars.pipeline.run import RunOptions, run_pipeline
from seminars.pipeline.services import Services, build_services
from seminars.pipeline.store import read_events_file

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "bayarea-biohub"
SOURCE_ID = "bayarea-biohub"
LIST_URL = "https://biohub.org/events/"
WEBINAR_URL = "https://biohub.org/events/webinar-esmfold2-structure-prediction-long-context/"
BOOTH_URL = "https://biohub.org/events/neurips-2026/"
BOOTH_UID = "bayarea-biohub.52886"
BOOTH_LOCATION = (
    "International Convention Centre (ICC) Sydney, Exhibition Centre, Level 4, "
    "Halls 5\u20137 Booth 72"
)
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
"""After the ESMFold2 webinar of 18 November: the booth is the only card still to come."""


def recorded(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def replaced(text: str, changes: dict[str, str]) -> str:
    for old, new in changes.items():
        assert old in text, old
        text = text.replace(old, new)
    return text


def booth_list_page() -> str:
    """The recorded list, whose only upcoming card is the booth instead of the webinar."""
    return replaced(
        recorded("events-e78c38af.html"),
        {
            "Long-Context Structure Prediction with ESMFold2 Webinar": "Biohub at NeurIPS 2026",
            "November 18, 2026": "December 6\u201311, 2026",
            (
                "Watch our webinar on long-context protein folding with ESMFold2, from "
                "biological motivation to approaches for extending its reach."
            ): (
                "Try our open models and data, see live demos, and meet the Biohub team at "
                "Booth 72 at NeurIPS 2026 in Sydney, December 6\u201311."
            ),
            WEBINAR_URL: BOOTH_URL,
        },
    )


def booth_detail_page() -> str:
    """The recorded webinar page with the days, the place and the post id of the booth."""
    return replaced(
        recorded("events-webinar-esmfold2-structure-predic-377d9f0a.html"),
        {
            '<time datetime="2026-11-18T13:00:00-08:00">Wednesday, November 18, 2026 1:00 pm'
            "</time>": '<time datetime="2026-12-06">Sunday, December 6, 2026</time>',
            '<time datetime="2026-11-18T14:00:00-08:00">2:00 pm PST</time>': (
                '<time datetime="2026-12-11">Friday, December 11, 2026</time>'
            ),
            'event-meta__value--location">Zoom<': (
                f'event-meta__value--location">{BOOTH_LOCATION}<'
            ),
            "https://biohub.org/?p=52744": "https://biohub.org/?p=52886",
        },
    )


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    """A project with the shipped configuration, of which only Biohub is collected."""
    root = tmp_path / "project"
    shutil.copytree(REPO_ROOT / "config", root / "config")
    shutil.copytree(REPO_ROOT / "locales", root / "locales")
    loaded = load_config(root, env={})
    biohub = tuple(source for source in loaded.sources if source.id == SOURCE_ID)
    assert len(biohub) == 1
    return dataclasses.replace(loaded, sources=biohub)


def real_services(config: AppConfig, pages: dict[str, str]) -> Services:
    services = build_services(config, {}, now=NOW, offline=True, use_llm=False, with_issues=False)
    http = FakeHttpClient(pages, clock=FakeClock(NOW))
    return dataclasses.replace(services, http_for=lambda source: http)


def run(config: AppConfig, services: Services) -> RunReport:
    return run_pipeline(
        config, services, now=NOW, options=RunOptions(offline=True), timer=FakeClock(NOW)
    )


def stored(config: AppConfig) -> dict[str, Event]:
    return {event.uid: event for event in read_events_file(config.paths.events_file)}


def feed(config: AppConfig, feed_id: str) -> str:
    return (config.paths.public_dir / "feeds" / f"{feed_id}.ics").read_text(encoding="utf-8")


BOOTH_PAGES = {LIST_URL: booth_list_page(), BOOTH_URL: booth_detail_page()}


def test_a_stored_booth_is_removed_in_the_run_that_excludes_it(config: AppConfig) -> None:
    # The run before the rule: a filter that keeps everything stores the booth.
    before = dataclasses.replace(real_services(config, BOOTH_PAGES), event_filter=FakeEventFilter())
    run(config, before)
    booth = stored(config)[BOOTH_UID]
    assert (booth.title, booth.location, booth.event_type, booth.all_day) == (
        "Biohub at NeurIPS 2026",
        BOOTH_LOCATION,
        "conference",
        True,
    )
    # bio from the source, ai-ml from the name of the meeting (taxonomy series)
    assert booth.fields == ["ai-ml", "bio"]
    for feed_id in ("all", "ai-ml", "bio", "conferences"):
        assert f"UID:{BOOTH_UID}@" in feed(config, feed_id)

    report = run(config, real_services(config, BOOTH_PAGES))
    assert stored(config) == {}
    assert report.counts.removed == 1
    [excluded] = report.excluded
    assert (excluded.title, excluded.reason, excluded.detail) == (
        "Biohub at NeurIPS 2026",
        "not_a_talk",
        "location names a booth",
    )
    assert excluded.source_ids == ["bayarea-biohub:52886"]
    for feed_id in ("all", "ai-ml", "bio", "conferences"):
        assert BOOTH_UID not in feed(config, feed_id)


def test_the_online_webinar_of_the_same_region_is_kept(config: AppConfig) -> None:
    pages = {
        LIST_URL: recorded("events-e78c38af.html"),
        WEBINAR_URL: recorded("events-webinar-esmfold2-structure-predic-377d9f0a.html"),
    }
    report = run(config, real_services(config, pages))
    assert report.excluded == []
    [webinar] = stored(config).values()
    assert webinar.title == "Long-Context Structure Prediction with ESMFold2 Webinar"
    assert webinar.is_online  # "Zoom" on its page
    assert webinar.fields == ["bio"]
    assert f"UID:{webinar.uid}@" in feed(config, "bio")
