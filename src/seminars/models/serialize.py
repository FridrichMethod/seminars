"""Deterministic (de)serialization of the files under data/ and public/.

data/events.json is the database of this project and is committed every day,
so its text must only change where the data changed::

    {"schema_version": 1, "events": [
    {"uid": "a", ...},
    {"uid": "b", ...}
    ]}

Events are sorted by uid, each event is one line, keys follow the field order
of the Event model, non-ASCII text is written as is and the file ends with a
newline. Loading such a file and dumping it again yields identical bytes.

Every other JSON document (health, last run, uid map, run report, the public
payloads) goes through ``dump_json``: two-space indentation, model field
order, trailing newline.

All of these writers keep non-ASCII text readable, so the text has to be
clean before it gets here (``seminars.textrules.clean_text``). Data that
cannot be cleaned because it must come back unchanged, such as the entries
of the LLM cache, is written with ``dump_ascii_json`` instead.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel

from seminars.models.event import Event
from seminars.textio import read_text, write_text_atomic

EVENTS_SCHEMA_VERSION: Final = 1

# json.dumps(ensure_ascii=False) leaves these line separators raw; escaping them keeps
# "one event per line" true for every tool that splits text into lines.
_LINE_SEPARATORS: Final = {chr(code): f"\\u{code:04x}" for code in (0x85, 0x2028, 0x2029)}


def _encode(value: Any, *, indent: int | None = None) -> str:
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=indent)
    for char, escaped in _LINE_SEPARATORS.items():
        text = text.replace(char, escaped)
    return text


def event_to_line(event: Event) -> str:
    """One event as a single line of JSON (no trailing newline)."""
    return _encode(event.model_dump(mode="json"))


def event_from_line(line: str) -> Event:
    """Inverse of ``event_to_line``."""
    return Event.model_validate_json(line)


def _sorted_by_uid(events: Iterable[Event]) -> list[Event]:
    ordered = sorted(events, key=lambda event: event.uid)
    for previous, current in pairwise(ordered):
        if previous.uid == current.uid:
            raise ValueError(f"duplicate event uid: {current.uid!r}")
    return ordered


def dump_events(events: Iterable[Event]) -> str:
    """Serialize events as the text of data/events.json (see module docstring)."""
    lines = [event_to_line(event) for event in _sorted_by_uid(events)]
    body = ",\n".join(lines)
    header = f'{{"schema_version": {EVENTS_SCHEMA_VERSION}, "events": ['
    return f"{header}\n{body}\n]}}\n" if lines else f"{header}\n]}}\n"


def load_events(text: str) -> list[Event]:
    """Parse the text of data/events.json; the result is sorted by uid."""
    document = json.loads(text)
    if not isinstance(document, dict) or not isinstance(document.get("events"), list):
        raise ValueError("events file must be an object with an 'events' list")
    version = document.get("schema_version")
    if version != EVENTS_SCHEMA_VERSION:
        raise ValueError(f"unsupported events schema_version: {version!r}")
    return _sorted_by_uid(Event.model_validate(item) for item in document["events"])


def dump_event_lines(events: Iterable[Event]) -> str:
    """Serialize events as JSON Lines (data/archive/<year>.jsonl), sorted by uid."""
    return "".join(event_to_line(event) + "\n" for event in _sorted_by_uid(events))


def load_event_lines(text: str) -> list[Event]:
    """Parse JSON Lines written by ``dump_event_lines``; blank lines are ignored."""
    return _sorted_by_uid(event_from_line(line) for line in text.split("\n") if line.strip())


def dump_json(model: BaseModel) -> str:
    """Serialize any model as a pretty, deterministic JSON document."""
    return _encode(model.model_dump(mode="json"), indent=2) + "\n"


def dump_ascii_json(value: Any) -> str:
    """Serialize plain JSON data as an ASCII-only document.

    Every character outside ASCII is written as a ``\\uXXXX`` escape, so the
    file passes the charset rule of data/ whatever the strings contain, and
    ``json.loads`` gives back exactly the value that went in. Keys are
    sorted, the indentation is two spaces and the text ends with a newline.
    Use it for files below data/ that hold text which was not normalized
    (data/llm_cache/).
    """
    return json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True) + "\n"


def load_json[T: BaseModel](text: str, model: type[T]) -> T:
    """Inverse of ``dump_json``."""
    return model.model_validate_json(text)


def write_events(path: Path, events: Iterable[Event]) -> None:
    """Write data/events.json atomically."""
    write_text_atomic(path, dump_events(events))


def read_events(path: Path) -> list[Event]:
    """Read data/events.json; a missing file means no events yet."""
    return load_events(read_text(path)) if path.exists() else []


def write_model(path: Path, model: BaseModel) -> None:
    """Write ``model`` to ``path`` with ``dump_json``, atomically."""
    write_text_atomic(path, dump_json(model))


def read_model[T: BaseModel](path: Path, model: type[T]) -> T | None:
    """Read a document written by ``write_model``; ``None`` when the file is missing."""
    return load_json(read_text(path), model) if path.exists() else None
