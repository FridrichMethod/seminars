"""The event filter excludes an exhibitor's booth at a meeting (seminars.classify.filters).

A numbered booth in the location ("Booth 72", "booth #301"), or a booth named
as a stand in the title or the series ("at Booth 72", "booth #301"), marks a
stand in an exhibit hall, which is no talk. A surname or a school followed by a
year, a date or an hour is no booth. The rule holds with the shipped taxonomy
and without any phrase at all, and it never reads the abstract.
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
        ("title", "Meet the team at the booth 12"),
        ("title", "Visit us at booth #301 in the exhibit hall"),
        ("series", "ASCB exhibitors, Booth No. 5"),
        ("series", "Exhibitors (booth number 40)"),
        ("location", "Exhibit hall, Booth #2026"),
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


@pytest.mark.parametrize(
    ("place", "text"),
    [
        # a speaker named Booth, followed by a date, an hour or a count
        ("title", "Seminar with Prof. Jane Booth 3/12"),
        ("title", "Talk by Jane Booth 4 pm"),
        ("title", "Jane Booth 3 lectures on protein folding"),
        ("title", "Wayne C. Booth 100th anniversary"),
        # in a title or series only a booth named as a stand counts
        ("title", "Booth 10: the speaker's notes"),
        ("series", "Chicago Booth 2026 Accounting Conference"),
        ("title", "Seminar at Booth 2026"),
        # in the location a year, a date or an hour after the word is no booth number
        ("location", "Chicago Booth 2026 Accounting Conference"),
        ("location", "Booth 10 am, Harper Center"),
        ("location", "Booth 10:30, Harper Center"),
        ("location", "Booth 3/12, Harper Center"),
        ("location", "Chicago Booth, Harper Center, Room 104"),
    ],
)
def test_a_surname_or_a_school_with_a_number_is_no_booth(place: str, text: str) -> None:
    item = candidate(**{place: text})
    assert verdict(EMPTY, item) is None
    assert verdict(SHIPPED, item) is None
