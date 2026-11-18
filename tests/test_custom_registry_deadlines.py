"""What the registry of meetings gives the feed of the deadlines.

The key ``short_title`` and the name of an edition in a feed (``Meeting.display_name``
and ``display_names``), the public loader of the editions of a registry source
(``registry_editions``), and two rules of curation that the committed registry has to
follow for its milestones: no milestone is a deadline of the main track of a machine
learning conference, which the README leaves to the calendar of ccfddl, and no two
editions with milestones share a name in the feed.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from deadline_samples import SCHOOL, SYMPOSIUM, registry_source
from factories import REFERENCE_NOW, make_window
from fakes import FakeHttpClient
from seminars.config import load_sources
from seminars.models import SourceConfig
from seminars.sources.base import get_adapter
from seminars.sources.custom.registry import (
    ADAPTER,
    MAX_SHORT_TITLE,
    Meeting,
    RegistryAdapter,
    display_names,
    registry_editions,
)
from seminars.sources.generic.common import AdapterConfigError

ROOT = Path(__file__).resolve().parents[1]

ML_MAIN_TRACKS = frozenset(
    {"AAAI", "AISTATS", "COLT", "CVPR", "ECCV", "ICCV", "ICLR", "ICML", "IJCAI", "NeurIPS"}
    | {"UAI"}
)
"""Machine learning conferences whose main-track deadlines the calendar of ccfddl carries."""

MAIN_TRACK_KINDS = frozenset({"abstract", "paper"})
"""Kinds of a milestone that would be a main-track submission; a workshop has its own."""


def edition(**overrides: Any) -> Meeting:
    return Meeting.model_validate({**SYMPOSIUM, **overrides})


def committed_registries() -> list[SourceConfig]:
    sources = load_sources(ROOT / "config" / "sources.d")
    return [source for source in sources if source.enabled and source.adapter == ADAPTER]


class TestShortTitle:
    def test_it_is_optional(self) -> None:
        assert edition().short_title is None

    def test_it_is_one_line_of_ascii_of_at_most_forty_characters(self) -> None:
        assert MAX_SHORT_TITLE == 40
        assert edition(short_title="x" * MAX_SHORT_TITLE).short_title == "x" * 40
        for bad in ("x" * 41, "", "Z\u00fcrich workshop", "two\nlines", " padded"):
            with pytest.raises(ValidationError, match="short_title"):
                edition(short_title=bad)

    def test_it_changes_nothing_of_the_event(self) -> None:
        source = registry_source(SYMPOSIUM)
        named = registry_source({**SYMPOSIUM, "short_title": "Symposium"})
        adapter = RegistryAdapter()
        window = make_window(REFERENCE_NOW, days_ahead=365)
        payload = adapter.fetch(FakeHttpClient(), source, window=window)
        assert adapter.parse(payload, named) == adapter.parse(payload, source)


class TestDisplayName:
    def test_the_short_title_comes_first(self) -> None:
        assert edition(short_title="Symposium on Molecules").display_name == (
            "Symposium on Molecules"
        )

    def test_otherwise_the_series_and_the_year_of_the_start(self) -> None:
        assert edition().display_name == "Example Symposium 2027"
        assert edition(series="PS41").display_name == "PS41 2027"

    @pytest.mark.parametrize("series", ["Example 2027", "Bay Area Day 2027 edition", "1999"])
    def test_a_series_with_a_year_stands_alone(self, series: str) -> None:
        assert edition(series=series).display_name == series

    def test_a_longer_number_is_no_year(self) -> None:
        assert edition(series="Example 12345").display_name == "Example 12345 2027"


class TestDisplayNames:
    def spring(self, **overrides: Any) -> Meeting:
        values = {"id": "example-symposium-spring-2027", "title": "Example Spring Symposium"}
        return edition(**{**values, **overrides})

    def fall(self, **overrides: Any) -> Meeting:
        values = {"id": "example-symposium-fall-2027", "title": "Example Fall Symposium"}
        return edition(**{**values, **overrides})

    def test_editions_of_one_series_and_year_are_named_by_their_titles(self) -> None:
        assert display_names([self.spring(), self.fall(), edition(short_title="Symposium")]) == {
            "example-symposium-spring-2027": "Example Spring Symposium",
            "example-symposium-fall-2027": "Example Fall Symposium",
            "example-symposium-2027": "Symposium",
        }

    def test_a_short_title_frees_the_name_for_the_other_edition(self) -> None:
        assert display_names([self.spring(short_title="Spring"), self.fall()]) == {
            "example-symposium-spring-2027": "Spring",
            "example-symposium-fall-2027": "Example Symposium 2027",
        }

    def test_another_year_or_series_is_another_name(self) -> None:
        later = edition(
            id="example-symposium-2028",
            start=edition().start.replace(year=2028),
            end=edition().end.replace(year=2028),
        )
        other = self.spring(series="Example Meeting")
        assert display_names([edition(), later, other]) == {
            "example-symposium-2027": "Example Symposium 2027",
            "example-symposium-2028": "Example Symposium 2028",
            "example-symposium-spring-2027": "Example Meeting 2027",
        }


class TestLoader:
    def test_every_edition_in_order_whatever_its_region(self) -> None:
        editions = registry_editions(registry_source(SYMPOSIUM, SCHOOL))
        assert [(item.id, item.region) for item in editions] == [
            ("example-symposium-2027", "bay-area"),
            ("example-school-2027", "world"),
        ]

    def test_params_that_do_not_fit_are_an_error_of_the_source(self) -> None:
        broken = registry_source({**SYMPOSIUM, "start": "soon"})
        with pytest.raises(AdapterConfigError, match="source conf-registry"):
            registry_editions(broken)
        assert issubclass(AdapterConfigError, ValueError)

    def test_the_name_of_the_adapter(self) -> None:
        assert ADAPTER == "registry"
        assert isinstance(get_adapter(ADAPTER), RegistryAdapter)


class TestCommittedRegistry:
    def test_there_is_a_registry(self) -> None:
        assert committed_registries()

    def test_no_milestone_is_a_main_track_deadline_of_a_machine_learning_conference(
        self,
    ) -> None:
        offending = [
            f"{meeting.id}: {milestone.kind}"
            for source in committed_registries()
            for meeting in registry_editions(source)
            if meeting.series in ML_MAIN_TRACKS
            for milestone in meeting.milestones
            if milestone.kind in MAIN_TRACK_KINDS
        ]
        assert offending == [], "main-track deadlines of ML conferences are left to ccfddl"

    def test_no_two_editions_with_milestones_share_a_name(self) -> None:
        shared = []
        for source in committed_registries():
            editions = registry_editions(source)
            names = display_names(editions)
            counts = Counter(names[meeting.id] for meeting in editions if meeting.milestones)
            shared += [f"{source.id}: {name}" for name, count in counts.items() if count > 1]
        assert shared == [], "give these editions a short_title of their own"
