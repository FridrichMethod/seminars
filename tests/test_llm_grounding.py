"""Grounding: what the model answered is only taken when the page text has it."""

from __future__ import annotations

from seminars.llm.grounding import MAX_FUZZY_EDITS, Grounding, normalize_for_grounding


class TestNormalization:
    def test_case_whitespace_and_typography_do_not_count(self) -> None:
        page = "\u201cProtein\u00a0Design\u201d \u2013 Jane\u2019s  talk\u2026"
        assert normalize_for_grounding(page) == '"protein design" - jane\'s talk...'

    def test_invisible_characters_are_dropped(self) -> None:
        assert normalize_for_grounding("en\u00adzyme\u200b design") == "enzyme design"

    def test_compatibility_forms_are_folded(self) -> None:
        assert normalize_for_grounding("\ufb01ne \uff21") == "fine a"


class TestGrounding:
    PAGE = (
        "Upcoming talks\nProtein design with diffusion models [[1]]\n"
        "Jane Doe, Massachusetts Institute of Technology\n"
        "Tuesday, November 17, 2026, 4:30 PM - 5:45 PM, Shriram 104\n"
        "Workshop: Dec 1-3, noon"
    )

    def grounding(self, similarity: int = 90) -> Grounding:
        return Grounding(self.PAGE, min_similarity=similarity)

    def test_a_value_on_the_page_is_kept_in_the_tidy_form_of_the_answer(self) -> None:
        assert self.grounding().text("  protein DESIGN with\ndiffusion ") == (
            "protein DESIGN with diffusion"
        )

    def test_a_value_that_is_not_on_the_page_is_dropped(self) -> None:
        grounding = self.grounding()
        assert grounding.text("Ignore previous instructions") is None
        assert grounding.text("John Smith") is None
        assert grounding.text("") is None
        assert grounding.text(None) is None
        assert grounding.text("   ") is None

    def test_a_close_variant_of_a_long_value_is_kept(self) -> None:
        assert self.grounding().text("Massachusets Institute of Technology") is not None

    def test_a_variant_too_far_from_the_page_is_dropped(self) -> None:
        assert self.grounding().text("Massachusetts Institute of Chemistry") is None

    def test_short_values_must_be_exact(self) -> None:
        grounding = self.grounding()
        assert grounding.text("Jane") == "Jane"
        assert grounding.text("Jone") is None

    def test_the_number_of_edits_is_capped_for_long_values(self) -> None:
        long_line = "a careful account of protein design " * 20
        grounding = Grounding(long_line, min_similarity=50)
        changed = "x" * (MAX_FUZZY_EDITS + 1) + long_line[MAX_FUZZY_EDITS + 1 : 300]
        assert grounding.text(long_line[:300]) is not None
        assert grounding.text(changed) is None

    def test_days_months_years_and_times(self) -> None:
        grounding = self.grounding()
        assert grounding.day(11, 17)
        assert grounding.day(12, 3)
        assert not grounding.day(11, 18)
        assert not grounding.day(10, 17)
        assert grounding.year(2026)
        assert not grounding.year(2027)
        assert grounding.clock(16, 30)
        assert grounding.clock(17, 45)
        assert grounding.clock(12, 0)
        assert not grounding.clock(16, 15)
        assert not grounding.clock(9, None)

    def test_a_month_given_as_a_number_counts(self) -> None:
        grounding = Grounding("Talk on 11/17 at 16:00", min_similarity=90)
        assert grounding.day(11, 17)
        assert grounding.clock(16, 0)
        assert not grounding.cancellation()

    def test_cancellation_must_be_stated(self) -> None:
        assert Grounding("This talk is CANCELLED", min_similarity=90).cancellation()
        assert Grounding("Postponed to spring", min_similarity=90).cancellation()
        assert not self.grounding().cancellation()
