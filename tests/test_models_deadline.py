"""The model of a deadline and the format of data/deadlines.json."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from deadline_samples import make_deadline
from factories import REFERENCE_NOW
from seminars.models import (
    DEADLINE_BOOKKEEPING,
    DEADLINES_FEED,
    FEED_IDS,
    UID_PATTERN,
    Deadline,
    deadline_uid,
    dump_deadlines,
    load_deadlines,
    ordered_deadlines,
)


class TestUid:
    def test_the_first_of_a_kind_has_no_number_and_the_later_ones_count_from_two(self) -> None:
        assert deadline_uid("gem-2027", "workshop_paper") == "dl.gem-2027.workshop_paper"
        assert deadline_uid("gem-2027", "workshop_paper", 2) == "dl.gem-2027.workshop_paper-2"
        assert deadline_uid("gem-2027", "paper", 12) == "dl.gem-2027.paper-12"

    def test_a_position_starts_at_one(self) -> None:
        with pytest.raises(ValueError, match="starts at 1"):
            deadline_uid("gem-2027", "paper", 0)

    def test_the_longest_uid_fits_the_uid_of_an_event(self) -> None:
        edition = "a" * 58 + "-2027"
        uid = deadline_uid(edition, "early_registration", 99)
        assert re.fullmatch(UID_PATTERN, uid)

    @pytest.mark.parametrize(
        "uid",
        [
            "dl.example-symposium-2027.abstract-2",
            "dl.example-symposium-2027.abstract-10",
        ],
    )
    def test_a_later_round_is_valid(self, uid: str) -> None:
        assert make_deadline(uid=uid, kind="abstract").uid == uid

    @pytest.mark.parametrize(
        "uid",
        [
            "dl.example-symposium-2027.abstract-1",
            "dl.example-symposium-2027.abstract-02",
            "dl.example-symposium-2027.abstract-x",
            "dl.other-2027.scholarship",
            "conf-registry.example-symposium-2027",
        ],
    )
    def test_a_uid_that_is_not_the_one_of_its_milestone_is_refused(self, uid: str) -> None:
        with pytest.raises(ValidationError, match="uid must be"):
            make_deadline(uid=uid, kind="abstract")


class TestModel:
    def test_the_feed_is_no_feed_id(self) -> None:
        assert DEADLINES_FEED == "deadlines"
        assert DEADLINES_FEED not in FEED_IDS

    def test_a_deadline_is_frozen_and_refuses_unknown_keys(self) -> None:
        deadline = make_deadline()
        with pytest.raises(ValidationError):
            deadline.label = "changed"
        with pytest.raises(ValidationError, match="extra"):
            make_deadline(speakers=[])

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("label", "Deadline \u2013 extended"),
            ("label", "two\nlines"),
            ("label", " padded"),
            ("edition_short", ""),
            ("location", "Z\u00fcrich"),
            ("url", "https://example.org/caf\u00e9"),
            ("url", "ftp://example.org/"),
            ("time", "24:00"),
            ("time", "9:00"),
            ("zone", "Pacific Time"),
            ("qualifier", "about"),
            ("region", "europe"),
            ("fields", ["astrology"]),
            ("sequence", -1),
            ("kind", "Paper"),
        ],
    )
    def test_values_outside_the_rules_of_the_registry_are_refused(
        self, field: str, value: Any
    ) -> None:
        with pytest.raises(ValidationError):
            make_deadline(**{field: value})

    def test_a_deadline_lies_within_its_edition(self) -> None:
        with pytest.raises(ValidationError, match="after the end of the edition"):
            make_deadline(date=date(2027, 4, 2))
        with pytest.raises(ValidationError, match="before edition_start"):
            make_deadline(edition_end=date(2027, 3, 1), date=date(2027, 2, 1))

    def test_the_facts_are_every_value_but_the_bookkeeping(self) -> None:
        deadline = make_deadline()
        facts = deadline.facts()
        assert set(facts) == set(Deadline.model_fields) - DEADLINE_BOOKKEEPING
        assert {"first_seen", "last_changed", "sequence"} == DEADLINE_BOOKKEEPING
        later = deadline.evolve(sequence=4, last_changed=REFERENCE_NOW + timedelta(days=3))
        assert later.facts() == facts
        assert deadline.evolve(time="23:59").facts() != facts


class TestFile:
    def deadlines(self) -> list[Deadline]:
        return [
            make_deadline(
                uid="dl.example-symposium-2027.abstract-2",
                kind="abstract",
                label="Poster abstract deadline",
                date=date(2027, 3, 8),
                zone=None,
            ),
            make_deadline(),
            make_deadline(
                uid="dl.example-symposium-2027.abstract",
                kind="abstract",
                label="Short talk abstract deadline",
                date=date(2026, 11, 20),
                time="23:59",
                zone="MT",
            ),
        ]

    def test_one_deadline_per_line_by_date_and_uid(self) -> None:
        text = dump_deadlines(self.deadlines())
        lines = text.split("\n")
        assert lines[0] == '{"schema_version": 1, "deadlines": ['
        assert lines[-2:] == ["]}", ""]
        uids = [json.loads(line.rstrip(","))["uid"] for line in lines[1:-2]]
        assert uids == [
            "dl.example-symposium-2027.abstract",
            "dl.example-symposium-2027.scholarship",
            "dl.example-symposium-2027.abstract-2",
        ]
        assert text.isascii()
        first = json.loads(lines[1].rstrip(","))
        assert list(first) == list(Deadline.model_fields)

    def test_loading_and_dumping_again_gives_the_same_bytes(self) -> None:
        text = dump_deadlines(self.deadlines())
        loaded = load_deadlines(text)
        assert loaded == ordered_deadlines(self.deadlines())
        assert dump_deadlines(loaded) == text
        assert dump_deadlines(reversed(loaded)) == text

    def test_no_deadline_is_an_empty_list(self) -> None:
        text = dump_deadlines([])
        assert text == '{"schema_version": 1, "deadlines": [\n]}\n'
        assert load_deadlines(text) == []

    def test_a_uid_twice_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="duplicate deadline uid"):
            dump_deadlines([make_deadline(), make_deadline(label="Other")])

    @pytest.mark.parametrize(
        "text",
        [
            '{"schema_version": 2, "deadlines": []}',
            '{"schema_version": 1}',
            "[]",
            '{"schema_version": 1, "deadlines": [{"uid": "x"}]}',
        ],
    )
    def test_a_file_of_another_form_is_refused(self, text: str) -> None:
        with pytest.raises(ValueError, match=r"deadlines|schema|validation"):
            load_deadlines(text)
