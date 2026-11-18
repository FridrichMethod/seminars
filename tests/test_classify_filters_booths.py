"""The event filter excludes an exhibitor's booth at a meeting (seminars.classify.filters).

A numbered booth in the title, the series or the location ("Booth 72", "booth
#301") marks a stand in an exhibit hall, which is no talk. The rule holds with
the shipped taxonomy and without any phrase at all, and it never reads the
abstract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from factories import make_candidate, make_event
from seminars.classify.filters import BOOTH_DETAIL, build_event_filter
from seminars.config import Taxonomy, load_settings, load_taxonomy
from seminars.contracts import EventFilter
from seminars.models import Candidate, ExcludedEvent

REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS = load_settings(REPO_ROOT / "config" / "settings.yaml")
SHIPPED = build_event_filter(load_taxonomy(REPO_ROOT / "config" / "taxonomy.yaml"), SETTINGS)
EMPTY = build_event_filter(Taxonomy(), SETTINGS)

NEURIPS_BOOTH: dict[str, Any] = {
    "title": "Biohub at NeurIPS 2026",
    "location": (
        "International Convention Centre (ICC) Sydney, Exhibition Centre, Level 4, "
        "Halls 5\u20137 Booth 72"
    ),
    "abstract": (
        "Try our open models and data, see live demos, and meet the Biohub team at Booth 72 "
        "at NeurIPS 2026 in Sydney, December 6\u201311."
    ),
    "event_type": "conference",
}
"""bayarea-biohub.52886 as the run of 2026-11-17 stored it."""


def candidate(**overrides: Any) -> Candidate:
    values: dict[str, Any] = {
        "uid": "bayarea-biohub.1",
        "title": "Protein structure prediction",
        "series": None,
        "location": "Biohub, 499 Illinois Street, San Francisco",
        "abstract": None,
        "source_ids": ["bayarea-biohub:1"],
        "fields": [],
        "field_tag_method": "source",
    }
    event = make_event(**{**values, **overrides})
    return make_candidate(event, source_ids=["bayarea-biohub"], default_fields=["bio"])


def verdict(event_filter: EventFilter, item: Candidate) -> tuple[str, str | None] | None:
    result = event_filter.filter([item])
    if not result.excluded:
        assert result.kept == [item]
        return None
    [excluded] = result.excluded
    return (excluded.reason, excluded.detail)


@pytest.mark.parametrize("event_filter", [SHIPPED, EMPTY], ids=["shipped", "no phrases"])
def test_the_biohub_booth_at_neurips_is_not_a_talk(event_filter: EventFilter) -> None:
    item = candidate(**NEURIPS_BOOTH)
    result = event_filter.filter([item])
    assert result.kept == []
    assert result.excluded == [
        ExcludedEvent.from_event(item.event, "not_a_talk", "location names a booth")
    ]


@pytest.mark.parametrize(
    ("place", "text"),
    [
        ("title", "Biohub at Booth 72"),
        ("title", "Visit us at booth #301 in the exhibit hall"),
        ("series", "ASCB exhibitors, Booth No. 5"),
        ("location", "Moscone South, Booth 1234"),
        ("location", "Exhibit Hall B, booth no 12"),
        ("location", "Hall C, Booth number 7"),
        ("location", "Expo floor, Booth 12A"),
        ("location", "Exhibit hall, Booth#88"),
    ],
)
def test_a_numbered_booth_excludes_from_title_series_or_location(place: str, text: str) -> None:
    item = candidate(**{place: text})
    assert verdict(SHIPPED, item) == ("not_a_talk", BOOTH_DETAIL.format(place=place))


def test_a_booth_is_excluded_even_when_the_title_names_a_talk() -> None:
    item = candidate(title="Lightning talks at the Biohub booth", location="Booth 72")
    assert verdict(SHIPPED, item) == ("not_a_talk", "location names a booth")


def test_the_title_is_reported_before_the_location() -> None:
    item = candidate(title="Biohub at Booth 72", location="Booth 72")
    assert verdict(SHIPPED, item) == ("not_a_talk", "title names a booth")


@pytest.mark.parametrize(
    "overrides",
    [
        # the online webinar of the same source and region ("Other Locations")
        {"title": "Long-Context Structure Prediction with ESMFold2 Webinar", "location": "Zoom"},
        # a booth without a number, or a word that starts like one
        {"title": "Finance seminar", "location": "Chicago Booth, Harper Center, Room 104"},
        {"title": "Behavioral economics", "location": "Chicago Booth 1st floor"},
        {"title": "Recording booths for linguistics fieldwork"},
        {"title": "A talk", "location": "Boothe Hall 5"},
        # the abstract is never read, whatever it says about booths
        {"title": "Annual holiday party talk", "abstract": "Photo booth, face painter"},
        {"title": "Protein design at scale", "abstract": "Visit us at booth 12 afterwards."},
        # a meeting at which a talk is given
        {"title": "Physics of Life Symposium 2026", "location": "Biohub, San Francisco"},
    ],
)
def test_everything_else_is_left_to_the_other_rules(overrides: dict[str, Any]) -> None:
    item = candidate(**overrides)
    assert verdict(EMPTY, item) is None
