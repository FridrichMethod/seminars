"""The output schema a backend is given, and the validation of what it answers.

Every backend gets the same JSON schema, built by ``strict_json_schema`` from
the pydantic model of the request, and every answer goes through
``parse_answer``, whatever the backend claims to have enforced. An answer
that does not validate is discarded (``LLMInvalidOutputError``).

The schema is the common subset that the strict modes of all backends
accept: objects are closed (``additionalProperties: false``) and require all
their properties, optional values are nullable instead of absent, references
are inlined, and only structural keywords are left. Length limits, numeric
ranges and patterns are not sent; the pydantic model enforces them when the
answer is validated. Schemas that this subset cannot express (free-form
mappings, tuples, schemas that refer to themselves) are refused with
``ValueError`` when the request is built.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from pydantic import BaseModel, ValidationError

from seminars.contracts import LLMInvalidOutputError
from seminars.textrules import without_surrogates

_STRUCTURAL: Final = frozenset({"type", "enum", "const", "description"})
"""Keywords that are copied as they are."""

_DROPPED: Final = frozenset(
    {
        "title",
        "default",
        "examples",
        "format",
        "pattern",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)
"""Annotations and constraints that are left out; validation applies the constraints."""

_REFERENCE_PREFIX: Final = "#/$defs/"
_CLOSED_TYPES: Final = frozenset({"boolean", "integer", "number", "null"})
_FENCE: Final = "```"


def strict_json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """The JSON schema of ``schema`` in the form every backend accepts as a strict schema.

    The result is plain JSON data and belongs to the caller. ``ValueError``
    when the schema uses something the common subset cannot express.
    """
    document = schema.model_json_schema()
    definitions = document.get("$defs", {})
    return _strict(document, definitions, (), schema.__name__)


def _strict(
    node: Mapping[str, Any], definitions: Mapping[str, Any], open_refs: tuple[str, ...], name: str
) -> dict[str, Any]:
    node, open_refs = _resolved(node, definitions, open_refs, name)
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in _STRUCTURAL:
            result[key] = value
        elif key == "properties":
            result["properties"] = {
                prop: _strict(sub, definitions, open_refs, name) for prop, sub in value.items()
            }
        elif key == "items":
            result["items"] = _strict(value, definitions, open_refs, name)
        elif key in ("anyOf", "oneOf"):
            result["anyOf"] = [_strict(sub, definitions, open_refs, name) for sub in value]
        elif key == "additionalProperties":
            if value is not False and "properties" not in node:
                raise ValueError(
                    f"schema of {name}: a mapping with free keys (additionalProperties) "
                    "cannot be part of a strict output schema; use a list of objects"
                )
        elif key not in _DROPPED and key not in ("required", "$defs"):
            raise ValueError(f"schema of {name}: keyword {key!r} is not supported")
    if result.get("type") == "object":
        result["properties"] = result.get("properties", {})
        result["required"] = list(result["properties"])
        result["additionalProperties"] = False
    return result


def _resolved(
    node: Mapping[str, Any], definitions: Mapping[str, Any], open_refs: tuple[str, ...], name: str
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """``node`` with its reference replaced by the definition it points to.

    Keywords next to the reference win over those of the definition (a
    description of the field, for example). Returned with it: the
    references that are open at this point, to notice one that comes back.
    """
    merged = dict(node)
    while "$ref" in merged:
        reference = str(merged.pop("$ref"))
        target = reference.removeprefix(_REFERENCE_PREFIX)
        if not reference.startswith(_REFERENCE_PREFIX) or target not in definitions:
            raise ValueError(f"schema of {name}: cannot resolve the reference {reference!r}")
        if target in open_refs:
            raise ValueError(
                f"schema of {name}: {target} refers to itself, which a strict output "
                "schema cannot express"
            )
        open_refs = (*open_refs, target)
        merged = {**definitions[target], **merged}
    return merged, open_refs


def is_closed_vocabulary(schema: Mapping[str, Any]) -> bool:
    """Whether answers to ``schema`` (a strict schema) can only hold values from fixed sets.

    True when every string is one of an enumeration; numbers, booleans and
    null are closed by nature. Such an answer has no room for text that a
    hostile input might want to pass on. A backend that cannot be kept from
    reading local files is only ever given requests of this kind.
    """
    if "enum" in schema or "const" in schema:
        return True
    if "anyOf" in schema:
        return all(is_closed_vocabulary(variant) for variant in schema["anyOf"])
    kind = schema.get("type")
    if kind in _CLOSED_TYPES:
        return True
    if kind == "array":
        return "items" in schema and is_closed_vocabulary(schema["items"])
    if kind == "object":
        return all(is_closed_vocabulary(sub) for sub in schema.get("properties", {}).values())
    return False


def _object_text(text: str) -> str:
    """The JSON object inside ``text``: without a code fence or words around it."""
    text = text.strip()
    if text.startswith(_FENCE):
        lines = text.split("\n")
        body = lines[1:-1] if lines[-1].strip() == _FENCE else lines[1:]
        text = "\n".join(body).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            text = text[start : end + 1]
    return text


def parse_answer[T: BaseModel](answer: object, schema: type[T], *, provider: str) -> T:
    """``answer`` as a validated ``schema``; ``LLMInvalidOutputError`` for anything else.

    ``answer`` is the JSON text a backend returned or the data it decoded
    itself. The error says what kind of problem it was and never quotes the
    answer, which can hold text of a page or whatever a hostile page made
    the model write; for the same reason it does not carry the validation
    error as its cause.
    """
    data = answer
    if isinstance(answer, str):
        try:
            data = json.loads(_object_text(answer))
        except (ValueError, RecursionError):
            raise LLMInvalidOutputError("the answer is not valid JSON", provider=provider) from None
    try:
        return schema.model_validate(without_surrogates(data))
    except ValidationError as error:
        problems = error.error_count()
        raise LLMInvalidOutputError(
            f"the answer does not match the schema {schema.__name__} ({problems} problem(s))",
            provider=provider,
        ) from None
    except (ValueError, RecursionError):
        raise LLMInvalidOutputError(
            f"the answer does not match the schema {schema.__name__}", provider=provider
        ) from None
