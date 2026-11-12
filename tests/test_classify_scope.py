"""Categories whose events are always out of scope (``taxonomy.out_of_scope_categories``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from factories import events, make_candidate, make_event
from seminars.classify.filters import (
    OUT_OF_SCOPE_CATEGORY_DETAIL,
    RuleEventFilter,
    build_event_filter,
)
from seminars.config import (
    ConfigError,
    EventSettings,
    Settings,
    Taxonomy,
    load_settings,
    load_taxonomy,
)
from seminars.models import FIELD_IDS, Candidate, EventHints

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
SETTINGS: Final = load_settings(REPO_ROOT / "config" / "settings.yaml")
CME: Final = "Stanford Center for Continuing Medical Education"
TAXONOMY: Final = Taxonomy.model_validate(
    {
        "departments": {"School of Medicine": ["med"]},
        "exclusions": {"course": ["certificate program"], "social": ["reception"]},
        "out_of_scope_categories": [CME, "Book Club"],
    }
)


def with_defenses(include: bool) -> Settings:
    return SETTINGS.evolve(events=EventSettings(include_defenses=include))


def candidate(
    title: str = "Updates in parotid masses",
    *,
    uid: str = "e1",
    event_type: str = "seminar",
    **hints: Any,
) -> Candidate:
    event = make_event(
        uid=uid, title=title, event_type=event_type, fields=[], field_tag_method="source"
    )
    base: dict[str, Any] = {"source_ids": ["localist-stanford"], "default_fields": []}
    return make_candidate(event, **{**base, **hints})


def detail(label: str) -> str:
    return OUT_OF_SCOPE_CATEGORY_DETAIL.format(label=label)


@pytest.fixture
def event_filter() -> RuleEventFilter:
    return RuleEventFilter(TAXONOMY, with_defenses(True))


class TestExclusion:
    def test_a_free_tag_never_puts_an_event_out_of_scope(
        self, event_filter: RuleEventFilter
    ) -> None:
        result = event_filter.filter([candidate(tags=[CME], field_hints=["med"])])
        assert result.excluded == []
        assert len(result.kept) == 1

    def test_an_event_of_such_a_category_is_out_of_scope(
        self, event_filter: RuleEventFilter
    ) -> None:
        item = candidate(categories=["School of Medicine", CME], field_hints=["med"])
        result = event_filter.filter([item])
        assert result.kept == []
        assert result.uncertain == []
        assert [(excluded.reason, excluded.detail) for excluded in result.excluded] == [
            ("out_of_scope", detail(CME))
        ]
        assert result.excluded[0].title == item.event.title

    @pytest.mark.parametrize(
        "spelling",
        [
            CME.upper(),
            CME.lower(),
            f"  {CME}  ",
            "Stanford Center for Continuing-Medical Education",
            "stanford center, for continuing medical education",
        ],
    )
    def test_labels_match_like_departments(
        self, event_filter: RuleEventFilter, spelling: str
    ) -> None:
        result = event_filter.filter([candidate(categories=[spelling])])
        assert [(item.reason, item.detail) for item in result.excluded] == [
            ("out_of_scope", detail(CME))
        ]

    @pytest.mark.parametrize(
        "category",
        [
            "Stanford Center for Medical Education",
            "Center for Continuing Medical Education",
            "Stanford Center for Continuing Medical Education Office",
            "Continuing Studies",
            "Book Clubs",
        ],
    )
    def test_other_labels_do_not_match(self, event_filter: RuleEventFilter, category: str) -> None:
        item = candidate(categories=[category])
        assert event_filter.filter([item]).kept == [item]

    def test_nothing_else_about_the_event_matters(self, event_filter: RuleEventFilter) -> None:
        item = candidate(
            "Medicine Grand Rounds: sepsis in the ICU",
            categories=[CME, "School of Medicine", "Department of Medicine"],
            default_fields=["med", "bio"],
            field_hints=["med"],
        )
        assert [excluded.reason for excluded in event_filter.filter([item]).excluded] == [
            "out_of_scope"
        ]

    def test_the_category_comes_before_the_exclusion_phrases(
        self, event_filter: RuleEventFilter
    ) -> None:
        item = candidate("Physician Leadership Certificate Program", categories=[CME])
        assert [
            (excluded.reason, excluded.detail) for excluded in event_filter.filter([item]).excluded
        ] == [("out_of_scope", detail(CME))]

    def test_a_phrase_in_a_category_does_not_make_it_uncertain(
        self, event_filter: RuleEventFilter
    ) -> None:
        item = candidate(categories=["Social Event/Reception", CME])
        result = event_filter.filter([item])
        assert result.uncertain == []
        assert [excluded.reason for excluded in result.excluded] == ["out_of_scope"]

    def test_the_first_configured_label_is_reported(self, event_filter: RuleEventFilter) -> None:
        item = candidate(categories=["book club", CME])
        assert [excluded.detail for excluded in event_filter.filter([item]).excluded] == [
            detail(CME)
        ]

    def test_the_defense_switch_comes_first(self) -> None:
        event_filter = RuleEventFilter(TAXONOMY, with_defenses(False))
        item = candidate(event_type="defense", categories=[CME])
        assert [excluded.reason for excluded in event_filter.filter([item]).excluded] == ["defense"]
        included = RuleEventFilter(TAXONOMY, with_defenses(True))
        assert [excluded.reason for excluded in included.filter([item]).excluded] == [
            "out_of_scope"
        ]

    def test_only_the_events_of_such_a_category_are_excluded(
        self, event_filter: RuleEventFilter
    ) -> None:
        inside = candidate("A talk", uid="e1", categories=["School of Medicine"])
        outside = candidate("A course", uid="e2", categories=[CME])
        result = event_filter.filter([outside, inside])
        assert result.kept == [inside]
        assert [excluded.title for excluded in result.excluded] == ["A course"]

    def test_without_the_setting_nothing_is_out_of_scope_by_category(self) -> None:
        event_filter = RuleEventFilter(Taxonomy(), with_defenses(True))
        item = candidate(categories=[CME])
        assert event_filter.filter([item]).kept == [item]

    def test_after_classification_the_events_with_a_field_stay(
        self, event_filter: RuleEventFilter
    ) -> None:
        """drop_out_of_scope looks at the fields only; the category was handled before."""
        event = make_event(fields=["med"])
        assert event_filter.drop_out_of_scope([event]).kept == [event]


class TestConfiguration:
    @pytest.mark.parametrize("label", ["---", " & ", "()"])
    def test_a_label_without_letters_or_digits_is_an_error(self, label: str) -> None:
        taxonomy = Taxonomy.model_validate({"out_of_scope_categories": [label]})
        with pytest.raises(ConfigError, match="out_of_scope_categories"):
            build_event_filter(taxonomy, SETTINGS)

    def test_the_shipped_taxonomy_leaves_out_the_continuing_education_courses(self) -> None:
        taxonomy = load_taxonomy(REPO_ROOT / "config" / "taxonomy.yaml")
        assert taxonomy.out_of_scope_categories == [CME]

    def test_the_shipped_label_is_not_also_a_department(self) -> None:
        """A label that is out of scope must not also promise a field."""
        taxonomy = load_taxonomy(REPO_ROOT / "config" / "taxonomy.yaml")
        keys = {label.casefold() for label in taxonomy.departments}
        assert all(label.casefold() not in keys for label in taxonomy.out_of_scope_categories)


# --- properties ------------------------------------------------------------------------------

SHIPPED: Final = load_taxonomy(REPO_ROOT / "config" / "taxonomy.yaml")
FILTERS: Final = {
    include: build_event_filter(SHIPPED, with_defenses(include)) for include in (True, False)
}
_WORDS: Final = st.text(st.characters(codec="ascii", categories=["L", "Nd", "Zs"]), max_size=20)


@st.composite
def labelled_candidates(draw: st.DrawFn) -> Candidate:
    """Any candidate that carries the out-of-scope label in some spelling."""
    event = draw(events()).evolve(title=draw(_WORDS).strip() or "TBA", fields=[])
    spelling = draw(st.sampled_from([CME, CME.upper(), CME.lower(), f" {CME}", CME.title()]))
    hints = EventHints(
        source_ids=draw(st.lists(st.sampled_from(["localist-stanford", "sites-chemistry"]))),
        default_fields=draw(st.lists(st.sampled_from(FIELD_IDS), max_size=2)),
        field_hints=draw(st.lists(st.sampled_from(FIELD_IDS), max_size=2)),
        categories=[spelling, *draw(st.lists(_WORDS, max_size=3))],
    )
    return Candidate(event=event, hints=hints)


@settings(max_examples=60, deadline=None)
@given(labelled_candidates(), st.booleans())
def test_a_labelled_event_never_stays_and_never_goes_to_the_llm(
    item: Candidate, include_defenses: bool
) -> None:
    result = FILTERS[include_defenses].filter([item])
    assert result.kept == []
    assert result.uncertain == []
    (excluded,) = result.excluded
    if not include_defenses and excluded.reason == "defense":
        return
    assert (excluded.reason, excluded.detail) == ("out_of_scope", detail(CME))
