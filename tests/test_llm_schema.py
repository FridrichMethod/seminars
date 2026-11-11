"""The output schema sent to a backend and the validation of what comes back."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from seminars.contracts import LLMInvalidOutputError
from seminars.llm.schema import is_closed_vocabulary, parse_answer, strict_json_schema
from seminars.models import Model

FAKE_TOKEN = "-".join(["sk", "ant", "oat01", "do", "not", "repeat", "me" * 8])
"""Shaped like a credential, and put together here so that no file holds one."""


class Speaker(Model):
    name: str
    affiliation: str | None = None


class Talk(Model):
    title: Annotated[str, Field(min_length=1, max_length=300, description="Title as printed")]
    day: Annotated[int, Field(ge=1, le=31)]
    kind: Literal["seminar", "colloquium"] = "seminar"
    speakers: list[Speaker] = []
    link: int | None = None


class Listing(Model):
    talks: list[Talk]


class Verdict(Model):
    index: int
    is_talk: bool
    fields: list[Literal["math", "bio"]]


class Verdicts(Model):
    results: list[Verdict]


def walk(schema: Any) -> list[dict[str, Any]]:
    """Every sub-schema of ``schema``, itself included."""
    found: list[dict[str, Any]] = []
    todo = [schema]
    while todo:
        node = todo.pop()
        if isinstance(node, dict):
            found.append(node)
            todo.extend(node.get("properties", {}).values())
            todo.extend(node.get("anyOf", []))
            if "items" in node:
                todo.append(node["items"])
    return found


class TestStrictSchema:
    def test_every_object_is_closed_and_requires_all_its_properties(self) -> None:
        schema = strict_json_schema(Listing)
        objects = [node for node in walk(schema) if node.get("type") == "object"]
        assert len(objects) == 3
        for node in objects:
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])

    def test_references_are_inlined(self) -> None:
        text = json.dumps(strict_json_schema(Listing))
        assert "$ref" not in text
        assert "$defs" not in text

    def test_only_structural_keywords_remain(self) -> None:
        allowed = {
            "type",
            "enum",
            "const",
            "description",
            "properties",
            "required",
            "additionalProperties",
            "items",
            "anyOf",
        }
        for node in walk(strict_json_schema(Listing)):
            assert set(node) <= allowed, node

    def test_the_shape_of_a_small_schema(self) -> None:
        assert strict_json_schema(Verdicts) == {
            "type": "object",
            "additionalProperties": False,
            "required": ["results"],
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["index", "is_talk", "fields"],
                        "properties": {
                            "index": {"type": "integer"},
                            "is_talk": {"type": "boolean"},
                            "fields": {
                                "type": "array",
                                "items": {"type": "string", "enum": ["math", "bio"]},
                            },
                        },
                    },
                }
            },
        }

    def test_descriptions_are_kept_and_optional_fields_stay_nullable(self) -> None:
        talk = strict_json_schema(Listing)["properties"]["talks"]["items"]
        assert talk["properties"]["title"] == {"type": "string", "description": "Title as printed"}
        assert talk["properties"]["link"] == {"anyOf": [{"type": "integer"}, {"type": "null"}]}
        assert talk["properties"]["day"] == {"type": "integer"}

    def test_the_result_is_plain_json_and_a_fresh_object_each_time(self) -> None:
        first = strict_json_schema(Listing)
        first["properties"].clear()
        assert strict_json_schema(Listing)["properties"]
        json.dumps(strict_json_schema(Listing), allow_nan=False)

    def test_a_schema_that_refers_to_itself_is_refused(self) -> None:
        class Node(BaseModel):
            name: str
            children: list[Node] = []

        with pytest.raises(ValueError, match="refers to itself"):
            strict_json_schema(Node)

    def test_a_free_form_mapping_is_refused(self) -> None:
        class Loose(BaseModel):
            extra: dict[str, int]

        with pytest.raises(ValueError, match="additionalProperties"):
            strict_json_schema(Loose)

    def test_a_tuple_is_refused(self) -> None:
        class Pair(BaseModel):
            point: tuple[int, int]

        with pytest.raises(ValueError, match="prefixItems"):
            strict_json_schema(Pair)

    def test_a_plain_pydantic_model_gets_closed_objects_too(self) -> None:
        class Open(BaseModel):
            model_config = ConfigDict(extra="allow")
            ok: bool

        assert strict_json_schema(Open) == {
            "type": "object",
            "additionalProperties": False,
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        }


class TestClosedVocabulary:
    def test_enumerations_numbers_and_booleans_are_closed(self) -> None:
        assert is_closed_vocabulary(strict_json_schema(Verdicts))

    def test_free_text_is_open(self) -> None:
        assert not is_closed_vocabulary(strict_json_schema(Listing))

    @pytest.mark.parametrize(
        ("schema", "closed"),
        [
            ({"type": "string"}, False),
            ({"type": "string", "enum": ["a"]}, True),
            ({"const": "a"}, True),
            ({"type": "null"}, True),
            ({"type": "number"}, True),
            ({"type": "array", "items": {"type": "string"}}, False),
            ({"type": "array"}, False),
            ({"anyOf": [{"type": "integer"}, {"type": "string"}]}, False),
            ({"anyOf": [{"type": "integer"}, {"type": "null"}]}, True),
            ({"type": "object", "properties": {"a": {"type": "string"}}}, False),
            ({"type": "object", "properties": {}}, True),
            ({}, False),
            ({"type": "mystery"}, False),
        ],
    )
    def test_cases(self, schema: dict[str, Any], closed: bool) -> None:
        assert is_closed_vocabulary(schema) is closed


class TestParseAnswer:
    def test_json_text_is_validated(self) -> None:
        answer = parse_answer(
            '{"results": [{"index": 0, "is_talk": true, "fields": ["bio"]}]}',
            Verdicts,
            provider="p",
        )
        assert answer == Verdicts(results=[Verdict(index=0, is_talk=True, fields=["bio"])])

    def test_decoded_data_is_validated(self) -> None:
        answer = parse_answer({"results": []}, Verdicts, provider="p")
        assert answer == Verdicts(results=[])

    @pytest.mark.parametrize(
        "text",
        [
            '```json\n{"results": []}\n```',
            '```\n{"results": []}\n```\n',
            'Here is the answer:\n{"results": []}\nDone.',
            '  \n{"results": []}  ',
        ],
    )
    def test_a_wrapped_object_is_found(self, text: str) -> None:
        assert parse_answer(text, Verdicts, provider="p") == Verdicts(results=[])

    @pytest.mark.parametrize(
        "answer",
        [
            "",
            "I cannot help with that.",
            '{"results": [',
            "[1, 2]",
            "null",
            '{"results": [{"index": 0, "is_talk": true, "fields": ["astrology"]}]}',
            json.dumps({"results": [], "secret": FAKE_TOKEN}),
            '{"results": [{"index": 0, "is_talk": true, "fields": [], "note": "extra"}]}',
            '{"results": "all of them"}',
            None,
            17,
            ["results"],
            "[" * 100_000,
        ],
    )
    def test_anything_else_is_invalid_output(self, answer: object) -> None:
        with pytest.raises(LLMInvalidOutputError) as caught:
            parse_answer(answer, Verdicts, provider="claude-cli")
        assert caught.value.provider == "claude-cli"
        assert caught.value.failure_kind == "invalid_output"

    def test_the_error_never_quotes_the_answer(self) -> None:
        secret = FAKE_TOKEN
        with pytest.raises(LLMInvalidOutputError) as caught:
            parse_answer({"results": [], "leak": secret}, Verdicts, provider="p")
        assert secret not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__
        with pytest.raises(LLMInvalidOutputError) as caught:
            parse_answer(f"{secret} is not json", Verdicts, provider="p")
        assert secret not in str(caught.value)
        assert caught.value.__suppress_context__

    def test_constraints_that_were_left_out_of_the_schema_are_still_enforced(self) -> None:
        good = {"talks": [{"title": "A talk", "day": 17}]}
        assert parse_answer(good, Listing, provider="p").talks[0].day == 17
        for bad in ({"title": "A talk", "day": 32}, {"title": "", "day": 17}):
            with pytest.raises(LLMInvalidOutputError):
                parse_answer({"talks": [bad]}, Listing, provider="p")

    def test_a_lone_surrogate_in_the_answer_is_dropped(self) -> None:
        class Note(BaseModel):
            text: str

        answer = parse_answer('{"text": "half \\ud83d emoji"}', Note, provider="p")
        assert answer.text == "half  emoji"
        answer.text.encode("utf-8")
