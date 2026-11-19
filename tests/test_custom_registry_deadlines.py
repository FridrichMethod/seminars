"""What the registry of meetings gives the feed of the deadlines.

The key ``short_title`` and the name of an edition in a feed (``Meeting.display_name``
and ``display_names``), the public loader of the editions of a registry source
(``registry_editions``), and two rules of curation that the committed registry has to
follow for its milestones: no milestone is a submission deadline of the main track of a
machine-learning or computer-science conference, which the README leaves to the calendar
of ccfddl, and no two editions with milestones share a name in the feed.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Final

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
from test_source_denylist import denied

ROOT = Path(__file__).resolve().parents[1]

MAIN_TRACK_SERIES: Final = frozenset(
    {
        *("aaai", "aistats", "colt", "corl", "cvpr", "eccv", "iccv", "iclr", "icml"),
        *("ijcai", "neurips", "nips", "uai", "wacv"),
        *("acl", "coling", "eacl", "emnlp", "naacl"),
        *("cikm", "icde", "kdd", "sigir", "sigkdd", "sigmod", "vldb", "wsdm", "www"),
        *("chi", "focs", "osdi", "siggraph", "soda", "sosp", "stoc"),
    }
)
"""Machine-learning and computer-science conferences whose main tracks the README leaves
to ccfddl, as a word of the series or the id of an edition names them (in lower case)."""

MAIN_TRACK_KINDS: Final = frozenset({"abstract", "paper"})
"""Kinds of a milestone that would be a main-track submission; a workshop has its own."""

AWAITING_THE_OWNER: Final[dict[str, str]] = {}
"""Editions of the committed registry that break the rule of the main tracks until the
owner decides; the test of such an edition is expected to fail (strictly, so the entry
here goes once the edition follows the rule)."""

_WORD: Final = re.compile(r"[a-z0-9]+")

PAPER: Final = {"kind": "paper", "date": "2027-01-10", "label": "Paper deadline"}


def edition(**overrides: Any) -> Meeting:
    return Meeting.model_validate({**SYMPOSIUM, **overrides})


def committed_registries() -> list[SourceConfig]:
    sources = load_sources(ROOT / "config" / "sources.d")
    return [source for source in sources if source.enabled and source.adapter == ADAPTER]


def committed_editions() -> list[Meeting]:
    return [meeting for source in committed_registries() for meeting in registry_editions(source)]


def names_a_main_track(meeting: Meeting) -> bool:
    """True for an edition, not a workshop, of a conference of ``MAIN_TRACK_SERIES``."""
    words = set(_WORD.findall(f"{meeting.series} {meeting.id}".lower()))
    return meeting.event_type != "workshop" and bool(words & MAIN_TRACK_SERIES)


def main_track_milestones(meeting: Meeting) -> list[str]:
    """The milestones of ``meeting`` that would be submissions to a main track."""
    if not names_a_main_track(meeting):
        return []
    return [
        f"{meeting.id}: {milestone.kind} {milestone.label}"
        for milestone in meeting.milestones
        if milestone.kind in MAIN_TRACK_KINDS
    ]


def main_track_cases() -> list[Any]:
    """One case per committed edition of a main-track conference, by id."""
    return [
        pytest.param(
            meeting,
            id=meeting.id,
            marks=[pytest.mark.xfail(strict=True, reason=AWAITING_THE_OWNER[meeting.id])]
            if meeting.id in AWAITING_THE_OWNER
            else [],
        )
        for meeting in committed_editions()
        if names_a_main_track(meeting)
    ]


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

    @pytest.mark.parametrize("meeting", main_track_cases())
    def test_no_milestone_is_a_main_track_submission_of_a_machine_learning_or_cs_conference(
        self, meeting: Meeting
    ) -> None:
        assert main_track_milestones(meeting) == [], "main-track deadlines are left to ccfddl"

    def test_every_edition_awaiting_the_owner_is_in_the_registry(self) -> None:
        assert set(AWAITING_THE_OWNER) <= {meeting.id for meeting in committed_editions()}

    def test_no_page_of_a_milestone_is_on_a_denied_aggregator(self) -> None:
        # The page that states a milestone is an organizer's page, as the pages of the edition
        # and of its check are (tests/test_source_denylist.py).
        pages = [
            (meeting.id, milestone.label, milestone.url)
            for meeting in committed_editions()
            for milestone in meeting.milestones
            if milestone.url is not None
        ]
        assert pages
        assert [page for page in pages if denied(page[2])] == []

    def test_no_two_editions_with_milestones_share_a_name(self) -> None:
        shared = []
        for source in committed_registries():
            editions = registry_editions(source)
            names = display_names(editions)
            counts = Counter(names[meeting.id] for meeting in editions if meeting.milestones)
            shared += [f"{source.id}: {name}" for name, count in counts.items() if count > 1]
        assert shared == [], "give these editions a short_title of their own"


class TestMainTrackRule:
    """The rule that the committed registry is held to, on made-up editions."""

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"series": "NeurIPS"}, id="series"),
            pytest.param({"series": "neurips 2027"}, id="case_and_year"),
            pytest.param(
                {"series": "International Conference on Machine Learning (ICML)"},
                id="acronym_in_the_name",
            ),
            pytest.param({"id": "naacl-2027", "series": "Nations of the Americas"}, id="id"),
            pytest.param({"series": "ACM SIGKDD"}, id="computer_science"),
        ],
    )
    def test_a_main_track_submission_is_found_by_a_word(self, overrides: dict[str, Any]) -> None:
        meeting = edition(**overrides, milestones=[PAPER])
        assert main_track_milestones(meeting) == [f"{meeting.id}: paper Paper deadline"]

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"series": "ICLR", "event_type": "workshop"}, id="a_workshop"),
            pytest.param(
                {"series": "ICLR", "milestones": [{**PAPER, "kind": "workshop_paper"}]},
                id="a_workshop_paper",
            ),
            pytest.param({"series": "Oracle Symposium"}, id="no_word_of_its_own"),
            pytest.param({"series": "Example Symposium"}, id="another_meeting"),
        ],
    )
    def test_other_milestones_pass(self, overrides: dict[str, Any]) -> None:
        meeting = edition(**{"milestones": [PAPER], **overrides})
        assert main_track_milestones(meeting) == []
