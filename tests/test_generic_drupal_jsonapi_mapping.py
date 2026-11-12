"""The drupal_jsonapi adapter: the mapping of single H&S event nodes into drafts."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from seminars.models import EventDraft
from seminars.sources.generic.drupal_jsonapi import (
    MAX_PATTERN_INPUT,
    split_speaker_label,
)
from test_generic_drupal_jsonapi import (
    PACIFIC,
    PST_4PM_NOV_2,
    SITE,
    link,
    node,
    parse_nodes,
    span,
    term,
    utc,
)

# --- mapping -------------------------------------------------------------------------------


SPEAKER = "hs_entity--event_collections__speaker"
SERIES = "node--hs_event_series"
TYPE = "taxonomy_term--hs_event_type"
STATUS = "taxonomy_term--hs_event_status"
CATEGORY = "taxonomy_term--hs_event_category"
AUDIENCE = "taxonomy_term--hs_event_audience"


class TestMapping:
    def test_a_plain_node(self) -> None:
        (draft,) = parse_nodes(node())
        assert draft.native_id == "n1"
        assert draft.title == "A talk"
        assert draft.start == datetime(2026, 11, 18, 0, 0, tzinfo=UTC)
        assert draft.end == datetime(2026, 11, 18, 1, 0, tzinfo=UTC)
        assert draft.location == "Room 101"
        assert draft.abstract_html == "<p>Abstract</p>"
        assert draft.speakers == []
        assert draft.series is None
        assert draft.is_online is None

    def test_relationships_are_resolved_by_type_and_id(self) -> None:
        resource = node(
            relationships={
                "field_hs_event_speaker": [
                    link(SPEAKER, "s1"),
                    link(SPEAKER, "s2"),
                    link(SPEAKER, "gone"),
                ],
                "field_hs_event_series": [link(SERIES, "x1"), link(SERIES, "x2")],
                "field_hs_event_type": [link(TYPE, "t2"), link(TYPE, "t1")],
                "field_hs_event_category": [link(CATEGORY, "c1")],
                "field_hs_event_audience": [link(AUDIENCE, "a1")],
                "field_hs_event_status": link(STATUS, "st"),
            }
        )
        included = [
            term(SPEAKER, "s1", label=" Ada  Lovelace (Analytical Engines, London) "),
            term(SPEAKER, "s2", label="Jane Doe, PhD"),
            term(SERIES, "x1", title="Number  Theory "),
            term(SERIES, "x2", title="Other"),
            term(SERIES, "s1", title="same id, other type"),
            term(TYPE, "t1", name=" Lecture"),
            term(TYPE, "t2", name="  "),
            term(CATEGORY, "c1", name="Class/Seminar"),
            term(AUDIENCE, "a1", name="Everyone"),
            term(STATUS, "st", name="canceled"),
            "junk",
            {"type": TYPE},
        ]
        (draft,) = parse_nodes(
            resource,
            included=included,
            event_type_names={"Lecture": "lecture"},
            status_names={"canceled": "cancelled"},
        )
        assert [(s.name, s.affiliation) for s in draft.speakers] == [
            ("Ada Lovelace", "Analytical Engines, London"),
            ("Jane Doe, PhD", None),
        ]
        assert draft.series == "Number Theory"
        assert draft.categories == ["Lecture", "Class/Seminar", "Everyone"]
        assert draft.event_type_hint == "lecture"
        assert draft.status_hint == "cancelled"

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            (
                "Matthew Novack (Notre Dame University) ",
                ("Matthew Novack", "Notre Dame University"),
            ),
            (
                "Alec Shelley (Stanford, Applied Physics)",
                ("Alec Shelley", "Stanford, Applied Physics"),
            ),
            ("A (B) Smith (Univ (Main) Campus)", ("A (B) Smith", "Univ (Main) Campus")),
            ("Hang Du, MIT", ("Hang Du", "MIT")),
            ("Peter Walter, PhD", ("Peter Walter, PhD", None)),
            ("saehoon", ("saehoon", None)),
            ("(Stanford)", ("(Stanford)", None)),
            ("Name ()", ("Name", None)),
            ("Broken)", ("Broken)", None)),
            ("Fran\u00e7ois Charles (ENS)", ("Fran\u00e7ois Charles", "ENS")),
            (", MIT", (", MIT", None)),
        ],
    )
    def test_speaker_labels(self, label: str, expected: tuple[str, str | None]) -> None:
        assert split_speaker_label(label) == expected

    def test_speaker_affiliation_attribute(self) -> None:
        resource = node(
            custm_speaker_affiliation=" ETH  Zurich ",
            relationships={"field_hs_event_speaker": [link(SPEAKER, "s1")]},
        )
        (draft,) = parse_nodes(
            resource,
            included=[term(SPEAKER, "s1", label="Ji Hoon Lee (not split)")],
            speaker_affiliation_attribute="custm_speaker_affiliation",
        )
        assert [(s.name, s.affiliation) for s in draft.speakers] == [
            ("Ji Hoon Lee (not split)", "ETH Zurich")
        ]

    def test_title_patterns(self) -> None:
        params = {
            "title_patterns": [
                r"^Never: (?P<title>.+)$",
                r'^(?P<series>[^:]+):\s*(?P<speaker>[^-]+?)\s*\((?P<affiliation>[^)]+)\)\s*-\s*"(?P<title>.+)"$',
            ],
            "event_type_names": {"Algebra Seminar": "seminar"},
        }
        (draft,) = parse_nodes(
            node(title='Algebra  Seminar: Emmy Noether (Erlangen) - "Rings"'), **params
        )
        assert draft.title == "Rings"
        assert draft.series == "Algebra Seminar"
        assert [(s.name, s.affiliation) for s in draft.speakers] == [("Emmy Noether", "Erlangen")]
        assert draft.event_type_hint == "seminar"

    def test_relationships_win_over_title_patterns(self) -> None:
        resource = node(
            title="Talks: Someone Else",
            relationships={
                "field_hs_event_speaker": [link(SPEAKER, "s1")],
                "field_hs_event_series": [link(SERIES, "x1")],
            },
        )
        (draft,) = parse_nodes(
            resource,
            included=[term(SPEAKER, "s1", label="Ada"), term(SERIES, "x1", title="Real Series")],
            title_patterns=[r"^(?P<series>[^:]+): (?P<speaker>.+)$"],
        )
        assert [s.name for s in draft.speakers] == ["Ada"]
        assert draft.series == "Real Series"
        assert draft.title == "Talks: Someone Else"

    def test_patterns_are_not_applied_to_very_long_titles(self) -> None:
        title = "Series: " + "x" * MAX_PATTERN_INPUT
        (draft,) = parse_nodes(
            node(title=title),
            title_patterns=[r"^(?P<series>[^:]+): (?P<title>.+)$"],
            title_status_pattern=r"(x)",
            title_status_names={"x": "cancelled"},
        )
        assert draft.title == title
        assert draft.series is None
        assert draft.status_hint is None

    @pytest.mark.parametrize(
        ("title", "term_name", "status"),
        [
            ("(CANCELED) Gluing small black holes", None, "cancelled"),
            ("Gluing small black holes (POSTPONED)", None, "cancelled"),
            ("Topology (Cancelled)", "live", "cancelled"),
            ("Better than square-root cancellation", None, None),
            ("A talk", "live", "confirmed"),
            ("A talk", "Published", "confirmed"),
            ("A talk", "unknown term", None),
            ("A talk", None, None),
            ("Canceled: Colloquium", "canceled", "cancelled"),
            ("Maybe (tentative)", None, "tentative"),
        ],
    )
    def test_status_from_the_term_and_the_title(
        self, title: str, term_name: str | None, status: str | None
    ) -> None:
        relationships = {"field_hs_event_status": link(STATUS, "st") if term_name else None}
        included = [term(STATUS, "st", name=term_name)] if term_name else []
        (draft,) = parse_nodes(
            node(title=title, relationships=relationships),
            included=included,
            title_status_names={
                "canceled": "cancelled",
                "cancelled": "cancelled",
                "postponed": "cancelled",
                "tentative": "tentative",
            },
            title_status_pattern=r"(?i)\b(cancell?ed|postponed|tentative)\b",
        )
        assert draft.status_hint == status

    def test_a_status_pattern_without_a_group_uses_the_whole_match(self) -> None:
        (draft,) = parse_nodes(
            node(title="Seminar CANCELLED"),
            title_status_pattern=r"(?i)cancelled",
            title_status_names={"cancelled": "cancelled"},
        )
        assert draft.status_hint == "cancelled"

    @pytest.mark.parametrize(
        ("date_field", "start", "end", "all_day"),
        [
            # 2026-11-05 00:00 to 23:59 PST, duration 1439
            (span("2026-11-05T08:00", "2026-11-06T07:59"), date(2026, 11, 5), None, True),
            # three days, 17 to 19 November
            (
                span("2026-11-17T08:00", "2026-11-20T07:59"),
                date(2026, 11, 17),
                date(2026, 11, 19),
                True,
            ),
            # across the change to standard time: 1 November 00:00 PDT to 23:59 PST
            (span("2026-11-01T07:00", "2026-11-02T07:59"), date(2026, 11, 1), None, True),
            # no end time
            (span("2026-11-03T00:00", "2026-11-03T00:00", duration=0), PST_4PM_NOV_2, None, False),
            (span("2026-11-03T00:00"), datetime(2026, 11, 3, tzinfo=UTC), None, False),
            # midnight to midnight is not all day by this rule, so the times stay
            (span("2026-11-05T08:00", "2026-11-06T08:00"), utc(5, 8), utc(6, 8), False),
            # an end before the start is passed on as the source says
            (span("2026-11-05T08:00", "2026-11-05T07:00"), utc(5, 8), utc(5, 7), False),
            # a naive time stays naive
            ({"value": "2026-11-05T16:00:00"}, datetime(2026, 11, 5, 16, 0), None, False),
        ],
    )
    def test_dates(self, date_field: dict[str, Any], start: Any, end: Any, all_day: bool) -> None:
        (draft,) = parse_nodes(node(field_hs_event_date=date_field))
        assert (draft.start, draft.end, draft.all_day) == (start, end, all_day)

    def test_several_dates_give_several_drafts(self) -> None:
        dates = [
            span("2026-11-18T00:00", "2026-11-18T01:00", rrule_index=0),
            span("2026-11-25T00:00", "2026-11-25T01:00", rrule_index=1),
            span("2026-11-25T00:00", "2026-11-25T01:00", rrule_index=1),
            {"value": "bad", "end_value": None},
            "junk",
        ]
        drafts = parse_nodes(node(field_hs_event_date=dates))
        assert [draft.native_id for draft in drafts] == ["n1", "n1/1"]
        assert drafts[1].start == datetime(2026, 11, 24, 16, 0, tzinfo=PACIFIC)

    def test_instances_of_an_import_come_first(self) -> None:
        instances = [span("2026-11-18T00:00", "2026-11-18T01:00"), span("2026-11-19T00:00")]
        drafts = parse_nodes(node(field_hs_event_instances=instances))
        assert [draft.native_id for draft in drafts] == ["n1", "n1/1"]
        same = parse_nodes(node(field_hs_event_instances=[instances[0]]))
        assert [draft.native_id for draft in same] == ["n1"]

    @pytest.mark.parametrize(
        ("dates", "native_ids"),
        [
            # an event that gains a second date keeps the native id of its first one
            ([span("2026-11-18T00:00")], ["n1"]),
            ([span("2026-11-18T00:00"), span("2026-11-25T00:00")], ["n1", "n1/1"]),
            # with rrule_index the ids follow it, whatever dates are left in the list
            ([span("2026-11-25T00:00", rrule_index=1)], ["n1/1"]),
            (
                [span("2026-12-02T00:00", rrule_index=2), span("2026-11-18T00:00", rrule_index=0)],
                ["n1/2", "n1"],
            ),
        ],
    )
    def test_native_ids_of_dates(self, dates: list[dict[str, Any]], native_ids: list[str]) -> None:
        drafts = parse_nodes(node(field_hs_event_date=dates))
        assert [draft.native_id for draft in drafts] == native_ids

    @pytest.mark.parametrize(
        ("value", "location"),
        [
            (
                "Hewlett Teaching Center\r\n370 Jane Stanford Way, Stanford, CA 94305\r\n201",
                "Hewlett Teaching Center, 370 Jane Stanford Way, Stanford, CA 94305, 201",
            ),
            ("Bass 122 or on Zoom \r\n", "Bass 122 or on Zoom"),
            ("  ", None),
            (None, None),
            (["Room"], None),
        ],
    )
    def test_location(self, value: Any, location: str | None) -> None:
        (draft,) = parse_nodes(node(field_hs_event_location=value))
        assert draft.location == location

    @pytest.mark.parametrize(
        ("experience", "online"),
        [("inperson", False), ("hybrid", True), ("virtual", True), (None, None)],
    )
    def test_experience(self, experience: str | None, online: bool | None) -> None:
        (draft,) = parse_nodes(node(field_hs_event_experience=experience))
        assert draft.is_online is online

    def test_links(self) -> None:
        (draft,) = parse_nodes(
            node(
                path={"alias": "/events/x"},
                field_hs_event_stream={
                    "uri": "https://stanford.zoom.us/j/1?pwd=REDACTED",
                    "title": "",
                },
            )
        )
        assert draft.url == f"{SITE}/events/x"
        assert draft.stream_url == "https://stanford.zoom.us/j/1?pwd=REDACTED"
        (plain,) = parse_nodes(node(path=None, field_hs_event_stream="javascript:alert(1)"))
        assert plain.url is None
        assert plain.stream_url is None

    def test_localist_imports_can_be_excluded(self) -> None:
        imported = node("imp", field_hs_event_link={"uri": "https://events.stanford.edu/event/x"})
        own = node("own", field_hs_event_link={"uri": "https://drive.google.com/poster"})
        kept = parse_nodes(imported, own, exclude_localist_imports=True)
        assert [draft.native_id for draft in kept] == ["own"]
        assert len(parse_nodes(imported, own)) == 2

    @pytest.mark.parametrize(
        "broken",
        [
            node(identifier=""),
            {"type": "node--hs_event", "attributes": {}},
            node(field_hs_event_date=None),
            node(field_hs_event_date={"value": 20261118}),
            node(field_hs_event_date={"value": "tomorrow"}),
            "junk",
        ],
    )
    def test_a_malformed_resource_is_skipped(self, broken: Any) -> None:
        drafts = parse_nodes(broken, node("ok"))
        assert [draft.native_id for draft in drafts] == ["ok"]

    def test_a_resource_whose_draft_cannot_be_built_is_skipped(self) -> None:
        resource = node("bad", relationships={"field_hs_event_speaker": [link(SPEAKER, "s1")]})
        drafts = parse_nodes(
            resource,
            node("ok"),
            included=[term(SPEAKER, "s1", label="Ada")],
            speaker_affiliation_attribute="x",
        )
        assert [draft.native_id for draft in drafts] == ["bad", "ok"]
        broken = node("bad", body={"value": 5}, path={"alias": 7})
        assert [draft.url for draft in parse_nodes(broken)] == [None]

    def test_a_validation_error_skips_the_resource(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from seminars.sources.generic import drupal_jsonapi

        real = drupal_jsonapi.EventDraft

        def picky(**values: Any) -> EventDraft:
            if values.get("title") == "bad":
                raise ValueError("refused")
            return real(**values)

        monkeypatch.setattr(drupal_jsonapi, "EventDraft", picky)
        drafts = parse_nodes(node("a", title="bad"), node("b"))
        assert [draft.native_id for draft in drafts] == ["b"]

    def test_non_ascii_text_is_kept(self) -> None:
        (draft,) = parse_nodes(
            node(title="Campana\u2013P\u0103un I", field_hs_event_location="Caf\u00e9")
        )
        assert draft.title == "Campana\u2013P\u0103un I"
        assert draft.location == "Caf\u00e9"

    def test_html_entities_are_left_to_normalization(self) -> None:
        (draft,) = parse_nodes(node(title="Q&amp;A", body={"value": "<p>&nbsp;x</p>"}))
        assert draft.title == "Q&amp;A"
        assert draft.abstract_html == "<p>&nbsp;x</p>"
