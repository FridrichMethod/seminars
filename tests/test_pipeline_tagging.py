"""Tests for the classification stage: rules first, the LLM tagger for open cases."""

from __future__ import annotations

from collections.abc import Sequence

from factories import make_event
from fakes import FakeFieldTagger
from fakes_llm import FakeLLMFieldTagger
from seminars.contracts import LLMUnavailableError
from seminars.models import Candidate, EventHints, LLMTagResult, TagDecision
from seminars.pipeline.tagging import tag_candidates


def candidate(uid: str, *, fields: Sequence[str] = ()) -> Candidate:
    event = make_event(uid=uid, fields=[], source_ids=[f"sites-x:{uid}"])
    return Candidate(
        event=event, hints=EventHints(source_ids=["sites-x"], default_fields=list(fields))
    )


class RaisingTagger:
    """An LLM tagger that breaks its contract and raises."""

    def tag_batch(self, candidates: Sequence[Candidate]) -> dict[str, LLMTagResult]:
        raise LLMUnavailableError("every provider failed")


class BuggyTagger:
    """An LLM tagger with a bug: it fails with an error of no LLM kind."""

    def tag_batch(self, candidates: Sequence[Candidate]) -> dict[str, LLMTagResult]:
        raise RuntimeError("index out of range")


class TestRules:
    def test_the_decision_of_the_rules_is_used(self) -> None:
        result = tag_candidates([candidate("a", fields=["chem"])], tagger=FakeFieldTagger())
        assert [(e.uid, e.fields, e.field_tag_method) for e in result.events] == [
            ("a", ["chem"], "source")
        ]
        assert result.excluded == ()
        assert result.warnings == ()

    def test_without_an_llm_an_open_case_keeps_the_empty_decision(self) -> None:
        result = tag_candidates([candidate("a")], tagger=FakeFieldTagger())
        assert result.events[0].fields == []

    def test_the_order_of_the_candidates_is_kept(self) -> None:
        names = ["c", "a", "b"]
        result = tag_candidates(
            [candidate(name, fields=["math"]) for name in names], tagger=FakeFieldTagger()
        )
        assert [event.uid for event in result.events] == names


class TestLLM:
    def test_only_open_and_uncertain_cases_are_sent_in_one_batch(self) -> None:
        llm = FakeLLMFieldTagger()
        tag_candidates(
            [candidate("open"), candidate("sure", fields=["cs"]), candidate("odd", fields=["cs"])],
            tagger=FakeFieldTagger(),
            llm_tagger=llm,
            uncertain={"odd"},
        )
        assert llm.batches == [["open", "odd"]]

    def test_nothing_is_sent_when_the_rules_decided_everything(self) -> None:
        llm = FakeLLMFieldTagger()
        tag_candidates([candidate("a", fields=["cs"])], tagger=FakeFieldTagger(), llm_tagger=llm)
        assert llm.batches == []

    def test_the_answer_decides_an_open_case(self) -> None:
        llm = FakeLLMFieldTagger({"a": LLMTagResult(fields=["physics"])})
        result = tag_candidates([candidate("a")], tagger=FakeFieldTagger(), llm_tagger=llm)
        assert (result.events[0].fields, result.events[0].field_tag_method) == (
            ["physics"],
            "llm",
        )

    def test_an_answer_without_fields_leaves_the_rules_in_place(self) -> None:
        llm = FakeLLMFieldTagger({"a": LLMTagResult(fields=[])})
        result = tag_candidates([candidate("a")], tagger=FakeFieldTagger(), llm_tagger=llm)
        assert (result.events[0].fields, result.events[0].field_tag_method) == ([], "source")

    def test_an_uncertain_case_keeps_its_fields_when_it_is_a_talk(self) -> None:
        llm = FakeLLMFieldTagger({"a": LLMTagResult(fields=["med"])})
        result = tag_candidates(
            [candidate("a", fields=["bio"])],
            tagger=FakeFieldTagger(),
            llm_tagger=llm,
            uncertain={"a"},
        )
        assert (result.events[0].fields, result.events[0].field_tag_method) == (["bio"], "source")

    def test_not_a_talk_excludes_the_event(self) -> None:
        llm = FakeLLMFieldTagger({"a": LLMTagResult(is_talk=False)})
        result = tag_candidates(
            [candidate("a", fields=["bio"]), candidate("b", fields=["bio"])],
            tagger=FakeFieldTagger(),
            llm_tagger=llm,
            uncertain={"a"},
        )
        assert [event.uid for event in result.events] == ["b"]
        assert [(item.reason, item.source_ids) for item in result.excluded] == [
            ("not_a_talk", ["sites-x:a"])
        ]

    def test_a_tagger_that_raises_is_treated_as_absent(self) -> None:
        result = tag_candidates(
            [candidate("a")], tagger=FakeFieldTagger(), llm_tagger=RaisingTagger()
        )
        assert result.events[0].fields == []
        assert result.warnings == (
            "the LLM tagger failed (LLMUnavailableError); the rules decided alone",
        )

    def test_a_tagger_with_a_bug_is_treated_as_absent(self) -> None:
        stored = make_event(uid="a", fields=["math"], field_tag_method="llm")
        result = tag_candidates(
            [candidate("a"), candidate("b", fields=["chem"])],
            tagger=FakeFieldTagger(),
            llm_tagger=BuggyTagger(),
            previous={"a": stored},
        )
        assert [(event.uid, event.fields) for event in result.events] == [
            ("a", ["math"]),
            ("b", ["chem"]),
        ]
        assert result.warnings == ("the LLM tagger failed (RuntimeError); the rules decided alone",)


class TestPreviousAnswers:
    def test_fields_the_llm_gave_earlier_are_kept_while_it_is_away(self) -> None:
        stored = make_event(uid="a", fields=["math"], field_tag_method="llm")
        result = tag_candidates(
            [candidate("a")], tagger=FakeFieldTagger(), llm_tagger=None, previous={"a": stored}
        )
        assert (result.events[0].fields, result.events[0].field_tag_method) == (["math"], "llm")

    def test_fields_of_the_rules_are_not_carried_over(self) -> None:
        stored = make_event(uid="a", fields=["math"], field_tag_method="rules")
        result = tag_candidates([candidate("a")], tagger=FakeFieldTagger(), previous={"a": stored})
        assert result.events[0].fields == []

    def test_the_rules_win_once_they_find_something(self) -> None:
        stored = make_event(uid="a", fields=["math"], field_tag_method="llm")
        decision = TagDecision(fields=["cs"], method="rules", needs_llm=True)
        result = tag_candidates(
            [candidate("a")], tagger=FakeFieldTagger({"a": decision}), previous={"a": stored}
        )
        assert (result.events[0].fields, result.events[0].field_tag_method) == (["cs"], "rules")
