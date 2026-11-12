"""The server component payload ("flight" data) of a Next.js page, for ``hai_events``.

The list page of ``hai_events`` carries its events as JSON objects inside the React server
component payload of its inline scripts. This module reads that payload: the params that
describe it (``FlightParams``, the ``flight`` key of the source), the rows it is cut into
(``cut_rows``) and their values (``Flight``). The rules are those of the ``flight`` key in
the docstring of ``seminars.sources.custom.hai_events``.

Every text taken from the payload is cut before anything else: many events can refer to one
long text, and each keeps what it takes. A link longer than ``MAX_TEXT_LENGTH`` as written is
missing, never cut.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import islice
from typing import Annotated, Any, Final

from pydantic import AfterValidator, Field, model_validator
from selectolax.lexbor import LexborHTMLParser

from seminars.models import Model
from seminars.sources.generic.common import (
    PATTERN_INPUT_LIMIT,
    Pattern,
    collapse,
    require_groups,
)
from seminars.sources.generic.jsonld import walk
from seminars.textrules import without_surrogates

MAX_REFERENCE_DEPTH: Final = 8  # references inside one another; a deeper one is missing
MAX_REFERENCE_LENGTH: Final = 256
MAX_LIST_ITEMS: Final = 500  # items of one list in the payload that are looked at
MAX_EVENTS: Final = 1000  # events taken from one payload (the page shows 20)
MAX_TEXT_LENGTH: Final = PATTERN_INPUT_LIMIT  # title, times, label, links
MAX_SUMMARY_LENGTH: Final = 20_000
MAX_TOPIC_LENGTH: Final = 200
MAX_TOPICS: Final = 50  # of one event
_PENDING: Final = object()  # marks a reference that is being resolved


def _check_path(value: str) -> str:
    names = [step.removesuffix("[]") for step in value.split(".")]
    if any(not name or "[" in name for name in names):
        raise ValueError(f"invalid key path {value!r}")
    return value


KeyPath = Annotated[str, AfterValidator(_check_path)]


class FlightKeys(Model):
    title: KeyPath
    start: KeyPath
    end: KeyPath | None = None
    link: KeyPath
    summary_html: KeyPath | None = None
    type_label: KeyPath | None = None
    topics: KeyPath | None = None
    organiser_url: KeyPath | None = None


class FlightParams(Model):
    script_pattern: Pattern
    row_start_pattern: Pattern
    text_row_pattern: Pattern
    special_row_pattern: Pattern
    reference_pattern: Pattern
    escaped_dollar_prefix: Annotated[str, Field(min_length=1)] = "$$"
    undefined_marker: Annotated[str, Field(min_length=1)] = "$undefined"
    event_marker_key: Annotated[str, Field(min_length=1)]
    upcoming_key: KeyPath
    previous_key: KeyPath
    keys: FlightKeys

    @model_validator(mode="after")
    def _check_patterns(self) -> FlightParams:
        require_groups(self.script_pattern, "flight.script_pattern", "json_string")
        require_groups(self.row_start_pattern, "flight.row_start_pattern", "id")
        require_groups(self.text_row_pattern, "flight.text_row_pattern", "id", "length")
        require_groups(self.reference_pattern, "flight.reference_pattern", "id")
        for pattern in (self.row_start_pattern, self.text_row_pattern):
            if not pattern.pattern.isascii():
                raise ValueError("the row patterns must be ASCII: they are matched on bytes")
        return self


# --- the flight payload ------------------------------------------------------------------


class FlightError(ValueError):
    """The payload cannot be cut into rows."""


@dataclass(frozen=True)
class TextRow:
    """The text of a text row: taken as it is, never read as a reference."""

    text: str


def flight_text(params: FlightParams, tree: LexborHTMLParser) -> str | None:
    """The joined JSON strings of the payload scripts; ``None`` when the page has none."""
    parts: list[str] = []
    for script in tree.css("script"):
        match = params.script_pattern.search(script.text(deep=True, strip=False))
        if match is None:
            continue
        try:
            value = json.loads(match["json_string"])
        except (ValueError, RecursionError):
            continue
        if isinstance(value, str):
            parts.append(without_surrogates(value))
    return "".join(parts) if parts else None


def bytes_pattern(pattern: Pattern) -> re.Pattern[bytes]:
    return re.compile(pattern.pattern.encode("ascii"), pattern.flags & ~re.UNICODE)


def cut_rows(params: FlightParams, text: str | None) -> dict[str, Any]:
    """Row id -> JSON value or ``TextRow``; ``FlightError`` when there is nothing to cut."""
    if text is None:
        raise FlightError("the page has no payload")
    data = text.encode("utf-8")
    row_start = bytes_pattern(params.row_start_pattern)
    text_row = bytes_pattern(params.text_row_pattern)
    rows: dict[str, Any] = {}
    position = 0
    while position < len(data):
        match = text_row.match(data, position)
        if match is not None and match.end() > position:
            try:
                end = match.end() + int(match["length"], 16)
            except ValueError as error:
                raise FlightError(f"text row length at byte {position}") from error
            text = data[match.end() : end].decode("utf-8", "replace")
            rows[match["id"].decode("ascii")] = TextRow(text)
            position = end
            continue
        match = row_start.match(data, position)
        if match is None or match.end() == position:
            raise FlightError(f"no row starts at byte {position}")
        newline = data.find(b"\n", match.end())
        end = len(data) if newline < 0 else newline
        identifier, body = match["id"].decode("ascii"), data[match.end() : end]
        head = body[:PATTERN_INPUT_LIMIT].decode("utf-8", "replace")
        if identifier and params.special_row_pattern.search(head) is None:
            with contextlib.suppress(ValueError, RecursionError):  # not JSON: skipped
                rows[identifier] = without_surrogates(json.loads(body))
        position = end + 1
    return rows


@dataclass(frozen=True)
class Flight:
    """The rows of a payload and the rules for its string values.

    Each reference is resolved once, so the work stays linear in the payload, and the
    texts taken from it are cut, so events that share a long text hold short copies. A
    cycle is missing, and so is a reference not yet resolved when met MAX_REFERENCE_DEPTH deep.
    """

    params: FlightParams
    rows: Mapping[str, Any]
    resolved: dict[str, Any] = field(default_factory=dict, init=False, repr=False, compare=False)

    def value(self, value: Any, depth: int = 0) -> Any:
        """``value`` with a reference resolved; ``None`` for markers and tree values."""
        if not isinstance(value, str) or not value.startswith("$"):
            return value
        if value.startswith(self.params.escaped_dollar_prefix):
            return value[1 : MAX_SUMMARY_LENGTH + 1]  # no text is kept longer
        if value == self.params.undefined_marker or len(value) > MAX_REFERENCE_LENGTH:
            return None
        if value not in self.resolved:
            if depth >= MAX_REFERENCE_DEPTH:
                return None
            self.resolved[value] = _PENDING
            self.resolved[value] = self.resolve(value, depth)
        found = self.resolved[value]
        return None if found is _PENDING else found

    def resolve(self, reference: str, depth: int) -> Any:
        match = self.params.reference_pattern.fullmatch(reference)
        if match is None or match["id"] not in self.rows:
            return None
        target = self.rows[match["id"]]
        path = match.groupdict().get("path")
        if isinstance(target, TextRow):
            return None if path else target.text
        for key in path.split(":") if path else []:
            target = step(self.value(target, depth + 1), key)
        return self.value(target, depth + 1)

    def follow(self, node: Any, path: str) -> list[Any]:
        """The values at a dotted ``path`` below ``node``; ``name[]`` fans out over a list.

        Every value is resolved exactly once, so an escaped "$$" text stays text.
        """
        current = [self.value(node)]
        for part in path.split("."):
            fan, name = part.endswith("[]"), part.removesuffix("[]")
            found: list[Any] = []
            for item in current:
                value = self.value(item.get(name)) if isinstance(item, dict) else None
                if fan and isinstance(value, list):
                    found.extend(self.value(entry) for entry in value[:MAX_LIST_ITEMS])
                elif not fan and value is not None:
                    found.append(value)
            current = found
        return [value for value in current if value is not None]

    def text(self, node: Any, path: str | None, length: int = MAX_TEXT_LENGTH) -> str | None:
        return next(iter(self.texts(node, path, length, 1)), None)

    def texts(self, node: Any, path: str | None, length: int, count: int) -> list[str]:
        """The first ``count`` texts at ``path``, each cut to ``length`` characters first."""
        values = self.follow(node, path) if path is not None else []
        texts = (collapse(value[:length]) for value in values if isinstance(value, str))
        return list(islice(filter(None, texts), count))

    def link(self, node: Any, path: str | None) -> str | None:
        """The first text at ``path`` that is not blank, collapsed; ``None`` when it is too long.

        The length is that of the text as the payload writes it: a run of white space
        inside does not make a link short enough that would be cut otherwise.
        """
        values = self.follow(node, path) if path is not None else []
        for value in values:
            if isinstance(value, str) and value and not value.isspace():
                return collapse(value) if len(value) <= MAX_TEXT_LENGTH else None
        return None

    def events(self) -> list[dict[str, Any]] | None:
        """Event objects of the lists at ``upcoming_key`` / ``previous_key``; ``None``: no list.

        A list or event that several objects refer to counts once; at most
        MAX_EVENTS events are taken.
        """
        found: dict[int, dict[str, Any]] = {}
        lists: set[int] = set()
        marker = self.params.event_marker_key
        for root in self.rows.values():
            for node in walk(root):
                for path in (self.params.upcoming_key, self.params.previous_key):
                    if not isinstance(node, dict) or path.split(".", 1)[0] not in node:
                        continue
                    target = next(iter(self.follow(node, path)), None)
                    if not isinstance(target, list) or id(target) in lists:
                        continue
                    lists.add(id(target))
                    for item in map(self.value, target[:MAX_LIST_ITEMS]):
                        if isinstance(item, dict) and marker in item and len(found) < MAX_EVENTS:
                            found.setdefault(id(item), item)
        return list(found.values()) if lists else None


def step(node: Any, key: str) -> Any:
    """One step of a reference path; ``props`` of a React element is its fourth item."""
    if isinstance(node, dict):
        return node.get(key)
    if isinstance(node, list):
        if key == "props" and len(node) >= 4 and node[0] == "$":
            return node[3]
        if key.isdigit() and int(key) < len(node):
            return node[int(key)]
    return None
