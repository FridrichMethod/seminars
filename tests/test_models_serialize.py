"""Tests for the deterministic serialization of data/events.json and friends."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given, settings

from factories import REFERENCE_NOW, event_lists, make_event
from seminars.models import (
    Event,
    HealthFile,
    LastRun,
    SourceHealthEntry,
    UidMap,
    dump_ascii_json,
    dump_event_lines,
    dump_events,
    dump_json,
    event_from_line,
    event_to_line,
    load_event_lines,
    load_events,
    load_json,
    read_events,
    read_model,
    write_events,
    write_model,
)
from seminars.textrules import data_charset_problems

E_ACUTE = chr(0xE9)
LINE_SEPARATOR = chr(0x2028)
PARAGRAPH_SEPARATOR = chr(0x2029)
NEXT_LINE = chr(0x85)

GOLDEN_EVENT_LINE = (
    '{"uid": "sites-bioengineering-1234", "title": "Protein design with diffusion models", '
    '"speakers": [{"name": "Jane Doe", "affiliation": "Stanford University", "url": null}], '
    '"series": "Bioengineering Seminar", "host_org": "Stanford Bioengineering", '
    '"institution": "stanford", "event_type": "seminar", '
    '"start": "2026-11-17T16:00:00-08:00", "end": "2026-11-17T17:00:00-08:00", '
    '"all_day": false, "location": "Shriram Center, Room 104", "is_online": false, '
    '"stream_url": null, '
    '"abstract": "A talk about generative models for de novo protein design.", '
    '"url": "https://bioengineering.stanford.edu/events/protein-design-seminar", '
    '"source_urls": ["https://bioengineering.stanford.edu/events/protein-design-seminar"], '
    '"source_ids": ["sites-bioengineering:1234"], "fields": ["bio"], '
    '"field_tag_method": "source", "picks_score": 0.0, "picks_reasons": [], '
    '"status": "confirmed", "extraction": "feed", '
    '"first_seen": "2026-11-10T20:00:00Z", "last_changed": "2026-11-10T20:00:00Z", '
    '"missing_runs": 0, "sequence": 0}'
)


def _three_events() -> list[Event]:
    return [make_event(uid=uid) for uid in ("b-2", "a-1", "c-3")]


class TestEventsFileFormat:
    def test_golden_text(self) -> None:
        text = dump_events([make_event()])
        assert text == '{"schema_version": 1, "events": [\n' + GOLDEN_EVENT_LINE + "\n]}\n"

    def test_empty_file(self) -> None:
        assert dump_events([]) == '{"schema_version": 1, "events": [\n]}\n'
        assert load_events(dump_events([])) == []

    def test_is_valid_json_with_one_event_per_line(self) -> None:
        text = dump_events(_three_events())
        lines = text.split("\n")
        assert lines[0] == '{"schema_version": 1, "events": ['
        assert lines[-2:] == ["]}", ""]
        assert [json.loads(line.rstrip(","))["uid"] for line in lines[1:-2]] == [
            "a-1",
            "b-2",
            "c-3",
        ]
        assert json.loads(text)["schema_version"] == 1

    def test_sorted_by_uid_regardless_of_input_order(self) -> None:
        events = _three_events()
        expected = dump_events(events)
        for seed in range(5):
            shuffled = events[:]
            random.Random(seed).shuffle(shuffled)
            assert dump_events(shuffled) == expected

    def test_key_order_is_the_model_field_order(self) -> None:
        line = event_to_line(make_event())
        assert list(json.loads(line)) == list(Event.model_fields)

    def test_non_ascii_is_written_as_is(self) -> None:
        text = dump_events([make_event(title="Poincar" + E_ACUTE + " seminar")])
        assert "Poincar" + E_ACUTE in text
        assert "\\u00e9" not in text

    def test_line_separators_inside_text_are_escaped(self) -> None:
        title = "a" + LINE_SEPARATOR + "b" + PARAGRAPH_SEPARATOR + "c" + NEXT_LINE + "d\ne"
        text = dump_events([make_event(title=title)])
        assert len(text.splitlines()) == 3
        assert "\\u2028" in text
        assert "\\u2029" in text
        assert "\\u0085" in text
        assert load_events(text)[0].title == title

    def test_ends_with_exactly_one_newline(self) -> None:
        text = dump_events(_three_events())
        assert text.endswith("]}\n")
        assert not text.endswith("\n\n")

    def test_duplicate_uids_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate event uid: 'a-1'"):
            dump_events([make_event(uid="a-1"), make_event(uid="a-1")])

    def test_timestamps_keep_their_offset(self) -> None:
        event = make_event(first_seen=datetime(2026, 11, 10, 20, 0, tzinfo=UTC))
        line = event_to_line(event)
        assert '"start": "2026-11-17T16:00:00-08:00"' in line
        assert '"first_seen": "2026-11-10T20:00:00Z"' in line


class TestEventsFileLoading:
    def test_round_trip_is_byte_stable(self) -> None:
        text = dump_events(_three_events())
        assert dump_events(load_events(text)) == text

    def test_loader_returns_events_sorted_by_uid(self) -> None:
        assert [event.uid for event in load_events(dump_events(_three_events()))] == [
            "a-1",
            "b-2",
            "c-3",
        ]

    @pytest.mark.parametrize(
        "text",
        [
            "[]",
            '{"schema_version": 1}',
            '{"schema_version": 2, "events": []}',
            '{"events": []}',
        ],
    )
    def test_rejects_unexpected_documents(self, text: str) -> None:
        with pytest.raises(ValueError, match="events"):
            load_events(text)

    def test_rejects_duplicate_uids(self) -> None:
        line = event_to_line(make_event())
        text = '{"schema_version": 1, "events": [\n' + line + ",\n" + line + "\n]}\n"
        with pytest.raises(ValueError, match="duplicate event uid"):
            load_events(text)

    @settings(max_examples=60, deadline=None)
    @given(event_lists())
    def test_property_round_trip(self, events: list[Event]) -> None:
        text = dump_events(events)
        loaded = load_events(text)
        assert loaded == sorted(events, key=lambda event: event.uid)
        assert dump_events(loaded) == text
        assert dump_events(reversed(events)) == text
        assert len(text.splitlines()) == len(events) + 2
        assert text.endswith("\n")


class TestEventLines:
    def test_single_line_round_trip(self) -> None:
        event = make_event()
        line = event_to_line(event)
        assert line == GOLDEN_EVENT_LINE
        assert event_from_line(line) == event

    def test_json_lines_round_trip(self) -> None:
        text = dump_event_lines(_three_events())
        assert text.count("\n") == 3
        assert [event.uid for event in load_event_lines(text)] == ["a-1", "b-2", "c-3"]
        assert dump_event_lines(load_event_lines(text)) == text
        assert load_event_lines("") == []
        assert dump_event_lines([]) == ""


class TestDocuments:
    def test_dump_json_is_pretty_and_ends_with_newline(self) -> None:
        uid_map = UidMap().with_aliases({"b:2": "evt-2", "a:1": "evt-1"})
        assert dump_json(uid_map) == (
            '{\n  "schema_version": 1,\n  "aliases": {\n'
            '    "a:1": "evt-1",\n    "b:2": "evt-2"\n  }\n}\n'
        )

    def test_document_round_trip_is_byte_stable(self) -> None:
        entry = SourceHealthEntry(at=REFERENCE_NOW, status="ok", events=3)
        health = (
            HealthFile().with_source_entry("sites-b", entry).with_source_entry("sites-a", entry)
        )
        text = dump_json(health)
        assert dump_json(load_json(text, HealthFile)) == text
        assert text.index("sites-a") < text.index("sites-b")

    def test_non_ascii_is_written_as_is(self) -> None:
        uid_map = UidMap().with_aliases({"custom-x:caf" + E_ACUTE: "evt-1"})
        assert "caf" + E_ACUTE in dump_json(uid_map)

    def test_ascii_json_escapes_everything_and_round_trips(self) -> None:
        """For data that cannot be cleaned first, such as LLM cache entries."""
        value = {
            "title": "Of" + chr(0xFB01) + "ce" + chr(0xA0) + "hours " + chr(0x1F680),
            "speaker": "Poincar" + E_ACUTE,
            "note": "a" + LINE_SEPARATOR + "b\tc",
            "b": [1, None, True],
        }
        text = dump_ascii_json(value)
        assert text.isascii()
        assert text.endswith("}\n")
        assert "\r" not in text
        assert "\t" not in text
        assert json.loads(text) == value
        assert list(json.loads(text)) == sorted(value)
        assert dump_ascii_json(json.loads(text)) == text
        assert data_charset_problems(text) == []
        with pytest.raises(ValueError, match="Out of range float"):
            dump_ascii_json({"x": float("nan")})


class TestFiles:
    def test_events_file_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "data" / "events.json"
        assert read_events(path) == []
        write_events(path, _three_events())
        raw = path.read_bytes()
        assert raw.endswith(b"]}\n")
        assert b"\r" not in raw
        assert not raw.startswith(b"\xef\xbb\xbf")
        write_events(path, read_events(path))
        assert path.read_bytes() == raw
        assert [entry.name for entry in path.parent.iterdir()] == ["events.json"]

    def test_model_file_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "last_run.json"
        assert read_model(path, LastRun) is None
        last_run = LastRun(
            started_at=REFERENCE_NOW, finished_at=REFERENCE_NOW, ok=True, mode="live"
        )
        write_model(path, last_run)
        assert read_model(path, LastRun) == last_run
        assert path.read_text(encoding="utf-8").endswith("}\n")
