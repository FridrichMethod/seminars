"""Picks reasons as lines of text and taken apart (seminars.models.public.PickReason).

The picks scorer writes every reason as one line of ASCII text, which the ICS
feeds show as it is. The site and the digest read the same line back with
``PickReason.parse`` and say it in the reader's language, so every line the
scorer writes has to come back as the reason it was made from.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from factories import make_event, make_speaker
from seminars.classify.picks import RulePicksScorer
from seminars.config import Interests
from seminars.models.public import (
    PickReason,
    PickReasonKind,
    PickReasonPlace,
    PublicEvent,
    signed_points,
)

INTERESTS = Interests.model_validate(
    {
        "threshold": 3.0,
        "multipliers": {"title": 3.0, "series": 2.0, "abstract": 1.0},
        "keywords": {"protein design": 1.0, "cryo-EM": 0.5, "optimal transport": 1.0},
        "series_allowlist": ["Math Colloquium"],
        "series_bonus": 3.0,
        "speaker_watchlist": ["David Baker"],
        "speaker_bonus": 2.0,
        "exclusions": {"undergraduate": 2.0},
    }
)


def keyword(term: str, place: PickReasonPlace, points: float) -> PickReason:
    return PickReason(kind="keyword", term=term, place=place, points=points)


LINES = [
    ('"protein design" in title (+3)', keyword("protein design", "title", 3.0)),
    ('"cryo-EM" in abstract (+0.5)', keyword("cryo-EM", "abstract", 0.5)),
    ('"topology" in series (+1)', keyword("topology", "series", 1.0)),
    ('series "Math Colloquium" (+3)', PickReason(kind="series", term="Math Colloquium", points=3)),
    ('speaker "David Baker" (+2)', PickReason(kind="speaker", term="David Baker", points=2.0)),
    ('penalty "undergraduate" (-2)', PickReason(kind="penalty", term="undergraduate", points=-2)),
    ('penalty "high school" (-1e+06)', PickReason(kind="penalty", term="high school", points=-1e6)),
]

KINDS: list[PickReasonKind] = ["keyword", "series", "speaker", "penalty"]
PLACES: list[PickReasonPlace] = ["title", "series", "abstract"]
TERMS = st.text(st.characters(codec="utf-8", exclude_categories=("Cs",)), min_size=1)
POINTS = st.integers(-10_000, 10_000).map(lambda value: value / 4)


@st.composite
def reasons(draw: st.DrawFn) -> PickReason:
    kind = draw(st.sampled_from(KINDS))
    place = draw(st.sampled_from(PLACES)) if kind == "keyword" else None
    points = draw(POINTS)
    if kind == "penalty":
        points = -abs(points)
    return PickReason(kind=kind, term=draw(TERMS), place=place, points=points)


class TestLines:
    @pytest.mark.parametrize(("line", "reason"), LINES)
    def test_every_kind_has_its_line(self, line: str, reason: PickReason) -> None:
        assert reason.text == line
        assert PickReason.parse(line) == reason

    @given(reasons())
    def test_a_line_reads_back_as_the_reason_it_was_written_from(self, reason: PickReason) -> None:
        assert PickReason.parse(reason.text) == reason

    def test_points_are_read_as_the_line_writes_them(self) -> None:
        line = PickReason(kind="series", term="S", points=1.23456789).text
        assert line == 'series "S" (+1.23457)'
        assert PickReason.parse(line).points == 1.23457

    def test_no_negative_zero(self) -> None:
        assert [signed_points(value) for value in (3.0, -2.0, 0.6, 0.0, -0.0)] == [
            "+3",
            "-2",
            "+0.6",
            "+0",
            "+0",
        ]
        assert PickReason(kind="penalty", term="x", points=-0.0).text == 'penalty "x" (+0)'

    @pytest.mark.parametrize(
        "line",
        [
            "title: protein design",
            "fake score 3",
            '"protein design" in speaker (+3)',
            'series "Math Colloquium" (3)',
            'series "Math Colloquium" (+3) and more',
            "series Math Colloquium (+3)",
        ],
    )
    def test_a_line_of_no_known_form_is_kept_as_it_is(self, line: str) -> None:
        reason = PickReason.parse(line)
        assert reason == PickReason(kind="other", term=line)
        assert reason.text == line

    def test_a_term_with_quotes_in_it_reads_back(self) -> None:
        reason = PickReason(kind="keyword", term='the "x" in title (+1)', place="title", points=3.0)
        assert PickReason.parse(reason.text) == reason

    @pytest.mark.parametrize(
        "parts",
        [
            {"kind": "keyword", "term": "x", "points": 1.0},
            {"kind": "series", "term": "x", "place": "title", "points": 1.0},
            {"kind": "speaker", "term": "x"},
            {"kind": "other", "term": "x", "points": 1.0},
            {"kind": "other", "term": ""},
            {"kind": "series", "term": "x", "points": float("inf")},
            {"kind": "praise", "term": "x", "points": 1.0},
        ],
    )
    def test_parts_that_do_not_fit_together_are_refused(self, parts: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            PickReason.model_validate(parts)


class TestScorerLines:
    @pytest.mark.parametrize(
        ("fields", "speaker"),
        [
            ({"title": "Protein design and cryo-EM", "series": "Math Colloquium"}, None),
            (
                {"title": "Optimal transport", "abstract": "For undergraduate students. Cryo-EM."},
                None,
            ),
            ({"title": "TBA", "series": "Protein design seminar"}, "David Baker"),
        ],
    )
    def test_every_line_of_the_scorer_reads_back_and_adds_up_to_the_score(
        self, fields: dict[str, str], speaker: str | None
    ) -> None:
        speakers = [make_speaker(name=speaker)] if speaker else []
        event = make_event(speakers=speakers, **fields)
        result = RulePicksScorer(INTERESTS).score(event)
        assert result.reasons
        parsed = [PickReason.parse(line) for line in result.reasons]
        assert all(reason.kind != "other" for reason in parsed)
        assert [reason.text for reason in parsed] == result.reasons
        assert sum(reason.points or 0.0 for reason in parsed) == pytest.approx(result.score)

    def test_the_public_event_carries_the_reasons_taken_apart(self) -> None:
        event = make_event(
            picks_score=6.0,
            picks_reasons=['"protein design" in title (+3)', 'series "Math Colloquium" (+3)', ""],
        )
        public = PublicEvent.from_event(event, picks_threshold=3.0)
        assert public.picks_reasons == [
            PickReason(kind="keyword", term="protein design", place="title", points=3.0),
            PickReason(kind="series", term="Math Colloquium", points=3.0),
        ]
        assert public.model_dump(mode="json")["picks_reasons"][1] == {
            "kind": "series",
            "term": "Math Colloquium",
            "place": None,
            "points": 3.0,
        }
