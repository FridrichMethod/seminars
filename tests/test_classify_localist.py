"""Stanford Events records through the shipped rules: filter, field tagger and scope.

Drafts come from the localist adapter, on the recorded pages of
tests/fixtures/localist-stanford/ (offline) or on records written here, with
the real source entry of config/sources.d/localist.yaml. Normalization turns
them into candidates, and the stages run in the order of the pipeline:
filter, tagger, out-of-scope check. Merging is left out (the records are
distinct instances of one source) and there is no LLM, so the rules decide
alone, as in a run without one.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from factories import make_document, make_payload, make_window
from seminars.classify.filters import (
    OUT_OF_SCOPE_CATEGORY_DETAIL,
    OUT_OF_SCOPE_DETAIL,
    build_event_filter,
)
from seminars.classify.rules import build_field_tagger
from seminars.config import load_settings, load_taxonomy
from seminars.config.loader import load_sources
from seminars.http import offline_client
from seminars.models import Candidate, EventDraft, SourceConfig, TagDecision
from seminars.pipeline.normalize import NormalizeContext, normalize_drafts
from seminars.sources.generic.localist import LocalistAdapter

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
FIXTURES: Final = REPO_ROOT / "tests" / "fixtures"
SETTINGS: Final = load_settings(REPO_ROOT / "config" / "settings.yaml")
TAXONOMY: Final = load_taxonomy(REPO_ROOT / "config" / "taxonomy.yaml")
EVENT_FILTER: Final = build_event_filter(TAXONOMY, SETTINGS)
TAGGER: Final = build_field_tagger(TAXONOMY)
RECORDED: Final = datetime(2026, 11, 11, 15, 49, 33, tzinfo=UTC)
"""When the event pages of the fixture were recorded."""
WINDOW: Final = make_window(RECORDED)
ENDPOINT: Final = "https://events.stanford.edu/api/2/events"
CME: Final = "Stanford Center for Continuing Medical Education"
CLASS_SEMINAR: Final = {"id": 37952570025304, "name": "Class/Seminar"}


@functools.cache
def stanford_source() -> SourceConfig:
    sources = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
    return sources["localist-stanford"]


@dataclass(frozen=True)
class Outcome:
    """What became of one record."""

    candidate: Candidate
    decision: TagDecision | None = None  # None when the filter excluded the record
    reason: str | None = None  # the exclusion reason; None when the event is in scope
    detail: str | None = None
    fields: tuple[str, ...] = ()


def classify(drafts: list[EventDraft]) -> dict[str, Outcome]:
    """Normalize, filter, tag and scope ``drafts``; outcomes by native id, in draft order."""
    normalized = normalize_drafts(
        drafts,
        stanford_source(),
        NormalizeContext(settings=SETTINGS, now=RECORDED, window=WINDOW),
    )
    assert normalized.invalid == ()
    assert normalized.outside_window == 0
    native_ids = {
        tuple(candidate.event.source_ids): draft.native_id or ""
        for draft, candidate in zip(drafts, normalized.candidates, strict=True)
    }
    assert len(native_ids) == len(drafts)
    candidates = {tuple(c.event.source_ids): c for c in normalized.candidates}
    filtered = EVENT_FILTER.filter(normalized.candidates)
    outcomes: dict[str, Outcome] = {}
    for item in filtered.excluded:
        key = tuple(item.source_ids)
        outcomes[native_ids[key]] = Outcome(candidates[key], reason=item.reason, detail=item.detail)
    for candidate in filtered.kept:
        decision = TAGGER.tag(candidate)
        event = candidate.event.evolve(fields=decision.fields, field_tag_method=decision.method)
        excluded = EVENT_FILTER.drop_out_of_scope([event]).excluded
        outcomes[native_ids[tuple(event.source_ids)]] = Outcome(
            candidate,
            decision=decision,
            reason=excluded[0].reason if excluded else None,
            detail=excluded[0].detail if excluded else None,
            fields=tuple(decision.fields),
        )
    return {draft.native_id or "": outcomes[draft.native_id or ""] for draft in drafts}


# --- the recorded pages ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def recorded() -> dict[str, Outcome]:
    source = stanford_source()
    http = offline_client("localist-stanford", fixtures_dir=FIXTURES)
    adapter = LocalistAdapter()
    return classify(adapter.parse(adapter.fetch(http, source, window=WINDOW), source))


def test_every_recorded_record_is_decided(recorded: dict[str, Outcome]) -> None:
    assert len(recorded) == 188
    for outcome in recorded.values():
        assert (outcome.reason is None) == bool(outcome.fields)


def test_the_physician_leadership_course_is_out_of_scope(recorded: dict[str, Outcome]) -> None:
    """School of Medicine maps to med, but the continuing education center decides."""
    outcome = recorded["54225562985263"]
    assert outcome.candidate.event.title == "Stanford Physician Leadership Certificate Program"
    assert {"School of Medicine", CME} <= set(outcome.candidate.hints.categories)
    assert outcome.candidate.hints.field_hints == ["med"]
    assert (outcome.reason, outcome.detail) == (
        "out_of_scope",
        OUT_OF_SCOPE_CATEGORY_DETAIL.format(label=CME),
    )
    assert outcome.decision is None


def test_every_continuing_education_course_is_out_of_scope(
    recorded: dict[str, Outcome],
) -> None:
    courses = {
        native_id: outcome
        for native_id, outcome in recorded.items()
        if CME in outcome.candidate.hints.categories
    }
    assert sorted(courses) == [
        "52853945971574",
        "53809595903126",
        "53921488445501",
        "54225562985263",
        "54239600094101",
        "54239642831812",
    ]
    assert {(item.reason, item.detail) for item in courses.values()} == {
        ("out_of_scope", OUT_OF_SCOPE_CATEGORY_DETAIL.format(label=CME))
    }


@pytest.mark.parametrize(
    ("native_id", "reason", "detail"),
    [
        ("54064922323927", "academic_calendar", 'title matches "abstract deadline"'),
        ("54064532593527", "academic_calendar", 'title matches "abstract deadline"'),
        ("54420408876947", "admissions", 'title matches "info session"'),
        ("54432183744117", "admissions", 'title matches "info session"'),
        ("54365877853340", "career", 'title matches "clinical internship"'),
        ("54381269449577", "fitness", 'title matches "access to experts webinar"'),
    ],
)
def test_recorded_non_talks_are_excluded(
    recorded: dict[str, Outcome], native_id: str, reason: str, detail: str
) -> None:
    outcome = recorded[native_id]
    assert (outcome.reason, outcome.detail) == (reason, detail)
    assert outcome.decision is None


def test_the_recorded_non_talks_come_from_mapped_departments(
    recorded: dict[str, Outcome],
) -> None:
    """Without the phrases, the department alone would have put them into a field."""
    for native_id in ("54064922323927", "54064532593527"):
        assert recorded[native_id].candidate.hints.field_hints == ["med"]


@pytest.mark.parametrize("native_id", ["54286951070575", "54472930002186"])
def test_the_surgery_work_in_progress_sessions_are_talks_of_med(
    recorded: dict[str, Outcome], native_id: str
) -> None:
    """S-SPIRE's monthly sessions: faculty and trainees present their research.

    The records are typed Lecture/Presentation/Talk and name a talk title in
    the description; the Department of Surgery maps them to med.
    """
    outcome = recorded[native_id]
    assert outcome.candidate.event.title == "Work In Progress Session"
    assert "Department of Surgery" in outcome.candidate.hints.categories
    assert outcome.reason is None
    assert outcome.fields == ("med",)
    assert outcome.decision is not None
    assert outcome.decision.method == "source"


@pytest.mark.parametrize(
    ("native_id", "title"),
    [
        ("53741880932247", "Portraiture as a Creative Path for Mindful Well-being"),
        ("53741881111471", "Landscapes as a Creative Path for Mindful Well-being"),
    ],
)
def test_a_title_that_says_canceled_marks_the_event_cancelled(
    recorded: dict[str, Outcome], native_id: str, title: str
) -> None:
    """The record is still "live"; the word in the title is what normalization reads.

    The filter keeps a cancelled event (it is a talk that does not take
    place). These two are wellness classes of an unmapped unit, so it is the
    scope check after classification that leaves them out.
    """
    outcome = recorded[native_id]
    assert outcome.candidate.event.status == "cancelled"
    assert outcome.candidate.event.title == title
    assert outcome.decision is not None
    assert (outcome.reason, outcome.detail) == ("out_of_scope", OUT_OF_SCOPE_DETAIL)


def test_a_tag_that_names_a_subject_does_not_decide_the_field(
    recorded: dict[str, Outcome],
) -> None:
    """A history of science talk tagged "mathematics" is not a talk in mathematics."""
    outcome = recorded["54124995976202"]
    assert outcome.candidate.event.title.startswith("The Greek and Arabic Reception of Ptolemy")
    assert "mathematics" in outcome.candidate.hints.tags
    assert "mathematics" not in outcome.candidate.hints.categories
    assert (outcome.reason, outcome.detail) == ("out_of_scope", OUT_OF_SCOPE_DETAIL)


def test_a_talk_on_child_health_is_med_by_its_title_not_by_its_tag(
    recorded: dict[str, Outcome],
) -> None:
    """Tagged "pediatrics"; before tags were kept apart, the abstract added math and physics."""
    outcome = recorded["54355994709896"]
    assert outcome.candidate.event.title.startswith("Team Science in Child Health Research")
    assert "pediatrics" in outcome.candidate.hints.tags
    assert outcome.reason is None
    assert outcome.fields == ("med",)


def test_the_department_of_a_tagged_talk_still_decides(recorded: dict[str, Outcome]) -> None:
    outcome = recorded["54251541115699"]
    assert outcome.candidate.event.title == "MRC Public Lecture: Tadashi Tokieda"
    assert "Department of Mathematics" in outcome.candidate.hints.categories
    assert outcome.reason is None
    assert "math" in outcome.fields


@pytest.mark.parametrize(
    "native_id",
    [
        "54427374962578",  # Pediatric Grand Rounds (CME), Office of Pediatric Education
        "54435561022415",  # Anesthesiology Grand Rounds
    ],
)
def test_recorded_grand_rounds_are_med(recorded: dict[str, Outcome], native_id: str) -> None:
    outcome = recorded[native_id]
    assert outcome.reason is None
    assert "med" in outcome.fields
    assert outcome.decision is not None
    assert any(
        'series "' in reason and "Grand Rounds" in reason for reason in outcome.decision.reasons
    )


def test_the_oceans_seminar_series_is_bio_without_hopkins(recorded: dict[str, Outcome]) -> None:
    """Only the Oceans Department lists this instance; the series name places it in bio.

    The instances that Hopkins Marine Station also lists get bio from that
    department, so every talk of the series is published the same way.
    """
    outcome = recorded["54444360554235"]
    assert outcome.candidate.event.title.startswith("Oceans Seminar Series | Peter Edmunds")
    assert outcome.candidate.hints.field_hints == []
    assert outcome.reason is None
    assert outcome.fields == ("bio",)
    assert outcome.decision is not None
    assert outcome.decision.reasons == ['bio: series "Oceans Seminar"']


@pytest.mark.parametrize(
    ("native_id", "field", "keyword"),
    [
        ("54408950715393", "ai-ml", "OpenAI"),  # Research Acceleration: The View Inside OpenAI
        ("54437336521132", "physics", "diamond semiconductor"),  # Building Diamond Semiconductors
    ],
)
def test_a_title_word_places_a_talk_of_an_unmapped_unit(
    recorded: dict[str, Outcome], native_id: str, field: str, keyword: str
) -> None:
    """Talks of units outside the department map, placed by a word of their title."""
    outcome = recorded[native_id]
    assert outcome.candidate.hints.field_hints == []
    assert keyword.casefold() in outcome.candidate.event.title.casefold()
    assert outcome.reason is None
    assert outcome.fields == (field,)
    assert outcome.decision is not None
    (reason,) = outcome.decision.reasons
    assert reason.startswith(f"{field}: keywords ")
    assert f'"{keyword}"' in reason


@pytest.mark.parametrize(
    "native_id",
    [
        "54444054768103",  # Earth System Science Seminar Series: Dr. Jenan Kharbush
        "54471918308209",  # Earth & Planetary Sciences Seminar - Prof. Peng Ni
        "54480326046124",  # ESE Seminar - Ruben Juanes: "Photoporomechanics ..."
    ],
)
def test_earth_and_planetary_seminars_are_left_to_the_llm(
    recorded: dict[str, Outcome], native_id: str
) -> None:
    """Their titles name no field of the seven; only the abstract does, and it never decides
    alone. Without an LLM they are out of scope.
    """
    outcome = recorded[native_id]
    assert outcome.decision is not None
    assert outcome.decision.needs_llm
    assert (outcome.reason, outcome.detail) == ("out_of_scope", OUT_OF_SCOPE_DETAIL)


# --- records written here ----------------------------------------------------------------------


def record(title: str, *departments: str, instance_id: int = 1, **fields: Any) -> dict[str, Any]:
    """A Localist record (``events[].event``) of a one-hour event on 2026-11-17.

    Without ``departments`` the record has no key "departments", as in the
    record of the Medicine Grand Rounds that the research found.
    """
    entry: dict[str, Any] = {
        "id": instance_id,
        "title": title,
        "localist_url": f"https://events.stanford.edu/event/{instance_id}",
        "event_instances": [
            {
                "event_instance": {
                    "id": instance_id,
                    "event_id": instance_id,
                    "start": "2026-11-17T12:00:00-08:00",
                    "end": "2026-11-17T13:00:00-08:00",
                    "all_day": False,
                }
            }
        ],
        "experience": "inperson",
        "status": "live",
        "filters": {"event_types": [CLASS_SEMINAR]},
        **fields,
    }
    if departments:
        entry["departments"] = [
            {"id": number, "name": name} for number, name in enumerate(departments, 1)
        ]
    return entry


def run_records(*records: dict[str, Any]) -> list[Outcome]:
    source = stanford_source()
    text = json.dumps({"events": [{"event": item} for item in records], "page": {"current": 1}})
    document = make_document(url=f"{ENDPOINT}?page=1", text=text, content_type="application/json")
    payload = make_payload(document, source_id=source.id, window=WINDOW, fetched_at=RECORDED)
    return list(classify(LocalistAdapter().parse(payload, source)).values())


def run_one(title: str, *departments: str, **fields: Any) -> Outcome:
    (outcome,) = run_records(record(title, *departments, **fields))
    return outcome


@pytest.mark.parametrize(
    "departments",
    [("Oceans Department",), ("Hopkins Marine Station", "Oceans Department"), ()],
)
def test_every_oceans_seminar_is_bio(departments: tuple[str, ...]) -> None:
    outcome = run_one(
        "Oceans Seminar Series | Jane Doe | Enabling Low-Cost Ocean Instrumentation", *departments
    )
    assert outcome.reason is None
    assert outcome.fields == ("bio",)


@pytest.mark.parametrize(
    ("title", "series"),
    [
        ("Medicine Grand Rounds: Sepsis in 2026", "Medicine Grand Rounds"),
        ("Medicine Grand Rounds", "Medicine Grand Rounds"),
        (
            "Department of Medicine Grand Rounds - Jane Doe, MD",
            "Department of Medicine Grand Rounds",
        ),
        ("Surgery Grand Rounds: Robotic Hernia Repair", "Surgery Grand Rounds"),
        (
            "Anesthesiology Grand Rounds with the California Surgeon General",
            "Anesthesiology Grand Rounds",
        ),
        ("Pediatric Grand Rounds (CME): Beyond Blood", "Pediatric Grand Rounds"),
        ("Pediatrics Grand Rounds", "Pediatrics Grand Rounds"),
        ("Ophthalmology Grand Rounds: retinal imaging", "Grand Rounds"),
    ],
)
def test_grand_rounds_without_a_department_are_med(title: str, series: str) -> None:
    """The research found the Medicine Grand Rounds without any department.

    The most specific series name is the one the reason gives.
    """
    outcome = run_one(title)
    assert outcome.candidate.hints.field_hints == []
    assert outcome.reason is None
    assert outcome.decision is not None
    assert "med" in outcome.fields
    assert outcome.decision.method == "rules"
    assert not outcome.decision.needs_llm
    med = [reason for reason in outcome.decision.reasons if reason.startswith("med: ")]
    assert med[0].startswith(f'med: series "{series}"'), med


def test_a_cancelled_grand_rounds_stays_in_as_cancelled() -> None:
    outcome = run_one("CANCELED: Medicine Grand Rounds - John Roe")
    event = outcome.candidate.event
    assert (event.title, event.status) == ("Medicine Grand Rounds - John Roe", "cancelled")
    assert outcome.reason is None
    assert outcome.fields == ("med",)


@pytest.mark.parametrize(
    "title",
    [
        "CANCELED:Biology Seminar Series - Jane Doe",
        "CANCELED | Biology Seminar Series - Jane Doe",
        "Biology Seminar Series - Jane Doe - Event Cancelled",
    ],
)
def test_cancelled_talks_of_mapped_departments_stay_in(title: str) -> None:
    outcome = run_one(title, "Department of Biology")
    assert outcome.candidate.event.status == "cancelled"
    assert outcome.candidate.event.title == "Biology Seminar Series - Jane Doe"
    assert (outcome.reason, outcome.fields) == (None, ("bio",))


@pytest.mark.parametrize(
    ("title", "department", "reason"),
    [
        ("Surgery M&M", "Department of Surgery", "administrative"),
        ("Surgery M & M", "Department of Surgery", "administrative"),
        (
            "Pediatric Surgery Morbidity and Mortality Conference",
            "Department of Surgery",
            "administrative",
        ),
        ("Stanford Physician Leadership Certificate Program", "School of Medicine", "course"),
        ("Clinical Research Certificate Programs", "Department of Medicine", "course"),
        ("Certificate Course in Global Health", "Center for Innovation in Global Health", "course"),
        ("Physics Coffee Hour", "Department of Physics", "social"),
        ("Bio-X Coffee Hours", "Bio-X Program", "social"),
        ("Graduate Student Happy Hour", "Chemistry Department", "social"),
        ("Neuroscience Happy Hour", "Wu Tsai Neurosciences Institute", "social"),
        ("Abstract Deadline: Surgical Research Day", "Department of Surgery", "academic_calendar"),
        ("Info Session: MS in Bioengineering", "Bioengineering", "admissions"),
        ("Coterm Information Session", "Electrical Engineering", "admissions"),
    ],
)
def test_non_talks_of_mapped_departments_are_excluded(
    title: str, department: str, reason: str
) -> None:
    outcome = run_one(title, department)
    assert outcome.candidate.hints.field_hints != []
    assert outcome.reason == reason
    assert outcome.decision is None


@pytest.mark.parametrize(
    "title",
    [
        "Trends in morbidity and mortality after cardiac surgery",
        "M. M. Rivera: Outcomes research in trauma surgery",
        "Surgery Grand Rounds: Morbidity and Mortality Conferences in the Age of AI",
    ],
)
def test_research_on_morbidity_and_initials_are_no_case_review(title: str) -> None:
    # Only an "M&M" or "morbidity and mortality" conference of a department is internal.
    outcome = run_one(title, "Department of Surgery")
    assert outcome.reason is None
    assert outcome.fields == ("med",)


def test_an_all_day_abstract_deadline_is_excluded() -> None:
    instance = {
        "id": 7,
        "event_id": 7,
        "start": "2026-11-15T00:00:00-08:00",
        "end": None,
        "all_day": True,
    }
    (outcome,) = run_records(
        record(
            "The Pacific Association of Pediatric Surgeons (PAPS) Abstract Deadline",
            "Department of Surgery",
            instance_id=7,
            event_instances=[{"event_instance": instance}],
        )
    )
    assert outcome.candidate.event.all_day
    assert outcome.reason == "academic_calendar"


@pytest.mark.parametrize(
    ("title", "department", "field"),
    [
        ("Proof certificates for SAT solvers", "Computer Science Department", "cs"),
        ("Certified robustness of neural networks", "Stanford Data Science", "ai-ml"),
        ("Coffee rust and the ecology of plant pathogens", "Department of Biology", "bio"),
        ("The hour-glass model of animal development", "Developmental Biology", "bio"),
        ("Quantum error correction: work in progress", "Department of Physics", "physics"),
        ("Work In Progress Session", "Department of Surgery", "med"),
        ("Works-in-Progress Session: Resident Research", "Department of Surgery", "med"),
        ("Neurosurgery Work-in-Progress Sessions", "Department of Neurosurgery", "med"),
        (
            "Progress in work function engineering of electrodes",
            "Materials Science and Engineering",
            "chem",
        ),
        ("Happy cells: stress responses in yeast", "Department of Biology", "bio"),
        ("Information flow in sessions of a protocol", "Information Systems Lab", "cs"),
        ("Grand Rounds in Bioengineering: implantable sensors", "Bioengineering", "bio"),
        ("Stress management interventions in oncology", "Department of Medicine", "med"),
        ("Access to experts: LLM triage of patient messages", "Department of Medicine", "med"),
        ("Internships, mentoring and the training of pre-med students", "Pediatrics", "med"),
    ],
)
def test_talks_with_words_of_the_new_phrases_are_kept(
    title: str, department: str, field: str
) -> None:
    outcome = run_one(title, department)
    assert outcome.reason is None
    assert field in outcome.fields


def test_a_continuing_education_course_is_out_of_scope_whatever_else_it_carries() -> None:
    outcome = run_one(
        "Medicine Grand Rounds: Updates in Sepsis Care",
        "School of Medicine",
        "Department of Medicine",
        CME,
    )
    assert outcome.candidate.hints.field_hints == ["med"]
    assert (outcome.reason, outcome.detail) == (
        "out_of_scope",
        OUT_OF_SCOPE_CATEGORY_DETAIL.format(label=CME),
    )
