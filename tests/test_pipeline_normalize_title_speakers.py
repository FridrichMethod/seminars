"""Tests for the speakers that normalization reads from the title of a draft.

A draft that names no speaker, in a field or in a speaker line, gets the
people its title names in one of the common forms; every other draft keeps
the speakers it names. The key of a day never takes a speaker of the title.
"""

from __future__ import annotations

from typing import Any

from factories import make_draft, make_source
from seminars.models import SpeakerDraft
from test_pipeline_normalize import one

LOCALIST = make_source(
    id="localist-stanford",
    name="Stanford Events",
    group="localist",
    adapter="localist",
    url="https://events.stanford.edu/api/2/events",
    homepage="https://events.stanford.edu/",
    host_org="Stanford University",
    default_fields=[],
    aggregator=True,
)


def people(source: Any = None, **overrides: Any) -> list[tuple[str, str | None]]:
    draft = make_draft(source_id=(source or make_source()).id, **overrides)
    return [(s.name, s.affiliation) for s in one(draft, source).event.speakers]


class TestDraftsWithoutSpeakers:
    def test_the_campus_calendar_gets_the_speaker_of_its_title(self) -> None:
        found = people(
            LOCALIST,
            speaker_text=None,
            title="PhD Dissertation Defense: Alyssa Cutter, Bando Lab",
        )
        assert found == [("Alyssa Cutter", "Bando Lab")]

    def test_every_source_gets_the_speaker_of_its_title(self) -> None:
        found = people(speaker_text=None, title="Evolgenome Seminar: Ami Bhatt")
        assert found == [("Ami Bhatt", None)]

    def test_the_abstract_vouches_for_a_name_first_title(self) -> None:
        title = "Jonathan Kipnis - Navigating turbulent CNS waters"
        assert people(speaker_text=None, title=title, abstract="A talk.") == []
        abstract = "Jonathan Kipnis, PhD, is a professor of pathology."
        assert people(speaker_text=None, title=title, abstract=abstract) == [
            ("Jonathan Kipnis", None)
        ]

    def test_a_status_marker_is_not_part_of_the_name(self) -> None:
        found = people(speaker_text=None, title="Cancelled: Grand Rounds: Gaurav Parmar")
        assert found == [("Gaurav Parmar", None)]

    def test_a_series_name_or_a_talk_title_gives_no_speaker(self) -> None:
        assert people(speaker_text=None, title="Norton Lecture") == []
        assert people(speaker_text=None, title="Protein design with diffusion models") == []
        assert people(speaker_text=None, title="TBA") == []

    def test_a_topic_after_a_series_is_stored_without_a_speaker(self) -> None:
        assert people(LOCALIST, speaker_text=None, title="Grand Rounds: Heart Failure") == []
        abstract = "We review new treatments for patients with reduced ejection fraction."
        found = people(
            LOCALIST, speaker_text=None, title="Grand Rounds: Heart Failure", abstract=abstract
        )
        assert found == []


class TestDraftsWithSpeakers:
    def test_a_speaker_line_wins_over_the_title(self) -> None:
        found = people(speaker_text="Jane Doe (MIT)", title="Evolgenome Seminar: Ami Bhatt")
        assert found == [("Jane Doe", "MIT")]

    def test_a_structured_speaker_wins_over_the_title(self) -> None:
        found = people(
            speakers=[SpeakerDraft(name="John Roe")],
            speaker_text=None,
            title="Evolgenome Seminar: Ami Bhatt",
        )
        assert found == [("John Roe", None)]

    def test_a_placeholder_speaker_lets_the_title_speak(self) -> None:
        found = people(speaker_text="TBA", title="Evolgenome Seminar: Ami Bhatt")
        assert found == [("Ami Bhatt", None)]


class TestKeys:
    def test_the_key_of_a_day_keeps_the_title_and_not_its_speaker(self) -> None:
        draft = make_draft(
            native_id=None, url=None, speaker_text=None, title="Evolgenome Seminar: Ami Bhatt"
        )
        event = one(draft).event
        assert [s.name for s in event.speakers] == ["Ami Bhatt"]
        assert event.source_ids == [
            "sites-bioengineering:day:2026-11-17:evolgenome-seminar-ami-bhatt"
        ]

    def test_the_key_of_a_day_takes_a_speaker_the_draft_names(self) -> None:
        draft = make_draft(
            native_id=None,
            url=None,
            speaker_text="Ami Bhatt",
            title="Evolgenome Seminar: Ami Bhatt",
        )
        assert one(draft).event.source_ids == ["sites-bioengineering:day:2026-11-17:ami-bhatt"]
