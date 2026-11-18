"""Registry sources and deadlines for the tests of the deadlines.

The meetings here are made up: their names, pages and dates belong to no real
edition, so no test states a fact about a real meeting. ``SYMPOSIUM`` is a
Bay Area edition with five milestones, two of them of one kind, one of them
before the window of a run at ``factories.REFERENCE_NOW`` (2026-11-10, whose
window starts on 2026-10-27); ``SCHOOL`` is a ``world`` edition, which only
carries its milestones.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Final

from factories import REFERENCE_NOW
from seminars.models import Deadline, SourceConfig

SYMPOSIUM_URL: Final = "https://symposium.example.org/2027/"
SCHOOL_URL: Final = "https://school.example.org/2027/"

SYMPOSIUM: Final[dict[str, Any]] = {
    "id": "example-symposium-2027",
    "title": "Example Symposium 2027: Design of Molecules",
    "series": "Example Symposium",
    "host_org": "Example Society",
    "start": date(2027, 3, 29),
    "end": date(2027, 4, 1),
    "region": "bay-area",
    "location": "Example Hall, San Francisco, CA",
    "url": SYMPOSIUM_URL,
    "fields": ["bio", "chem"],
    "event_type": "conference",
    "status": "confirmed",
    "verified": {"date": date(2026, 11, 9), "url": SYMPOSIUM_URL},
    "milestones": [
        {
            "kind": "abstract",
            "date": date(2026, 12, 3),
            "time": "23:59",
            "zone": "MT",
            "label": "Short talk abstract deadline",
        },
        {
            "kind": "registration_open",
            "date": date(2027, 2, 1),
            "qualifier": "by",
            "label": "Registration opens",
        },
        {"kind": "abstract", "date": date(2027, 3, 8), "label": "Poster abstract deadline"},
        {
            "kind": "scholarship",
            "date": date(2026, 11, 20),
            "zone": "AoE",
            "label": "Scholarship deadline",
        },
        {"kind": "other", "date": date(2026, 10, 1), "label": "Call for abstracts opens"},
    ],
}

SCHOOL: Final[dict[str, Any]] = {
    "id": "example-school-2027",
    "title": "Example Summer School 2027",
    "short_title": "Example School",
    "series": "Example Summer School",
    "host_org": "Example Institute",
    "start": date(2027, 7, 5),
    "end": date(2027, 7, 9),
    "region": "world",
    "location": "Example Institute, Basel, Switzerland",
    "url": SCHOOL_URL,
    "fields": ["math"],
    "event_type": "workshop",
    "status": "confirmed",
    "verified": {"date": date(2026, 11, 2), "url": SCHOOL_URL},
    "milestones": [
        {
            "kind": "application",
            "date": date(2027, 1, 15),
            "time": "12:00",
            "zone": "Europe/Zurich",
            "label": "Application deadline",
        },
    ],
}

SYMPOSIUM_UIDS: Final = (
    "dl.example-symposium-2027.scholarship",
    "dl.example-symposium-2027.abstract",
    "dl.example-symposium-2027.registration_open",
    "dl.example-symposium-2027.abstract-2",
)
"""The uids of the milestones of SYMPOSIUM in the window of REFERENCE_NOW, by date."""

SCHOOL_UID: Final = "dl.example-school-2027.application"


def registry_source(*meetings: dict[str, Any], **overrides: Any) -> SourceConfig:
    """A registry source in the group ``conferences`` holding ``meetings``."""
    values: dict[str, Any] = {
        "id": "conf-registry",
        "name": "Conference registry (kept by hand)",
        "group": "conferences",
        "adapter": "registry",
        "url": "https://github.com/example/seminars/blob/main/config/sources.d/conferences.yaml",
        "institution": "other",
        "host_org": "Conference registry",
        "default_event_type": "conference",
        "meeting_horizon": True,
        "params": {"allow_empty": True, "meetings": list(meetings)},
    }
    return SourceConfig.model_validate({**values, **overrides})


def make_deadline(**overrides: Any) -> Deadline:
    """The scholarship deadline of SYMPOSIUM as a run at REFERENCE_NOW stores it."""
    values: dict[str, Any] = {
        "uid": "dl.example-symposium-2027.scholarship",
        "edition_id": "example-symposium-2027",
        "edition_title": SYMPOSIUM["title"],
        "edition_short": "Example Symposium 2027",
        "series": SYMPOSIUM["series"],
        "region": "bay-area",
        "edition_start": SYMPOSIUM["start"],
        "edition_end": SYMPOSIUM["end"],
        "location": SYMPOSIUM["location"],
        "url": SYMPOSIUM_URL,
        "fields": ["bio", "chem"],
        "kind": "scholarship",
        "label": "Scholarship deadline",
        "date": date(2026, 11, 20),
        "time": None,
        "zone": "AoE",
        "qualifier": "exact",
        "verified_on": date(2026, 11, 9),
        "first_seen": REFERENCE_NOW,
        "last_changed": REFERENCE_NOW,
        "sequence": 0,
    }
    return Deadline.model_validate({**values, **overrides})


LATER: Final = datetime(2026, 11, 17, 20, 0, tzinfo=UTC)
"""A run one week after REFERENCE_NOW."""
