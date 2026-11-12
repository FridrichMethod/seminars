"""Builds the sample public payloads committed under tests/fixtures/_site/.

The web site is developed and tested against these two files:

* tests/fixtures/_site/events.json   a PublicEventsPayload
* tests/fixtures/_site/status.json   a PublicStatusPayload

They are generated from the models, so they always follow the schema.
tests/test_public_sample.py fails when the files and this module disagree;
regenerate them with ``uv run python tests/site_sample.py``.

The events cover what the site has to cope with: HTML and script injection
in every text field, an all-day event, a cancelled and a tentative event,
online and hybrid events, a "TBA" title, a missing end time, several
speakers, non-ASCII names, picks with reasons of every kind the picks scorer
writes, a past event, a far-future event, all seven fields and all
institutions. The sources have every state, and two of them a last run
that was not read in full (pages that could not be fetched, a listing far
smaller than usual) or that took a smaller listing as the usual size.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from factories import PACIFIC, make_event
from seminars.models import (
    Event,
    PublicEvent,
    PublicEventsPayload,
    PublicSourceStatus,
    PublicStatusPayload,
    dump_json,
)
from seminars.textio import write_text_atomic

SAMPLE_DIR = Path(__file__).resolve().parent / "fixtures" / "_site"
GENERATED_AT = datetime(2026, 11, 10, 12, 30, tzinfo=UTC)
PICKS_THRESHOLD = 3.0

# Non-ASCII letters for realistic names; written as code points to keep this file ASCII.
A_ACUTE = chr(0xE1)
E_ACUTE = chr(0xE9)
U_UMLAUT = chr(0xFC)

XSS_TITLE = '<script>alert("xss-title")</script> Robust learning <b>under</b> attack'
XSS_ABSTRACT = (
    '<img src=x onerror=alert("xss-abstract")> "><svg onload=alert(1)> '
    "javascript:alert(2) &lt;b&gt;entities stay literal&lt;/b&gt; "
    "</script><script>document.title='pwned'</script>"
)


def _at(month: int, day: int, hour: int, minute: int = 0, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=PACIFIC)


def _event(uid: str, title: str, start: datetime, **overrides: Any) -> Event:
    url = f"https://example.stanford.edu/events/{uid}"
    defaults: dict[str, Any] = {
        "uid": uid,
        "title": title,
        "start": start,
        "end": start.replace(hour=start.hour + 1),
        "url": url,
        "source_urls": [url],
        "source_ids": [f"sample:{uid}"],
        "speakers": [],
        "series": None,
        "abstract": None,
        "location": None,
    }
    return make_event(**{**defaults, **overrides})


def sample_events() -> list[Event]:
    """The stored events the sample payload is projected from."""
    return [
        _event(
            "sample-past-math",
            "Moduli of curves and tropical geometry",
            _at(11, 9, 14, 30),
            speakers=[{"name": "Ana Souza", "affiliation": "UC Berkeley"}],
            series="Algebraic Geometry Seminar",
            host_org="Stanford Mathematics",
            location="Building 380, Room 383-N",
            abstract="A survey of recent results on tropical compactifications.",
            fields=["math"],
            picks_score=3.0,
            picks_reasons=[
                'series "Algebraic Geometry Seminar" (+2)',
                '"algebraic geometry" in abstract (+1)',
            ],
        ),
        _event(
            "sample-xss",
            XSS_TITLE,
            _at(11, 12, 12),
            speakers=[{"name": "<i>Mallory</i> O'Brien", "affiliation": '"><b>Evil Corp</b>'}],
            series="Security <Lunch>",
            host_org="Stanford Computer Science & <Friends>",
            location="Gates <u>104</u>",
            abstract=XSS_ABSTRACT,
            fields=["cs", "ai-ml"],
        ),
        _event(
            "sample-cancelled",
            "Random matrices and number theory",
            _at(11, 13, 16),
            speakers=[{"name": "Wei Zhang", "affiliation": "MIT"}],
            series="Math Colloquium",
            host_org="Stanford Mathematics",
            event_type="colloquium",
            location="Building 380, Room 380-C",
            fields=["math"],
            status="cancelled",
        ),
        _event(
            "sample-online",
            "Serving large models on small budgets",
            _at(11, 14, 15, 30),
            speakers=[{"name": "Priya Raman", "affiliation": "Example Labs"}],
            series="MLSys Seminar",
            host_org="Stanford MLSys",
            is_online=True,
            stream_url="https://stanford.zoom.us/j/1234567890",
            abstract="Online only. Systems tricks for cheap inference.",
            fields=["cs", "ai-ml"],
        ),
        _event(
            "sample-pick",
            "Protein design with diffusion models",
            _at(11, 17, 16),
            speakers=[
                {
                    "name": "Jane Doe",
                    "affiliation": "Stanford University",
                    "url": "https://example.stanford.edu/people/jane-doe",
                }
            ],
            series="Bioengineering Seminar",
            host_org="Stanford Bioengineering",
            location="Shriram Center, Room 104",
            abstract="Generative models for de novo enzyme design, with wet-lab validation.",
            fields=["bio", "ai-ml"],
            picks_score=9.0,
            picks_reasons=[
                '"protein design" in title (+3)',
                '"diffusion model" in title (+3)',
                'series "Bioengineering Seminar" (+3)',
            ],
        ),
        _event(
            "sample-tba",
            "TBA",
            _at(11, 18, 16, 30),
            end=None,
            speakers=[
                {"name": f"Zo{E_ACUTE} M{U_UMLAUT}ller", "affiliation": f"ETH Z{U_UMLAUT}rich"}
            ],
            series="Physical Chemistry Seminar",
            host_org="Stanford Chemistry",
            location="Sapp Center Lecture Hall",
            fields=["chem"],
        ),
        _event(
            "sample-hybrid-med",
            "Grand Rounds: cell therapies for solid tumors",
            _at(11, 19, 8),
            speakers=[
                {"name": "Maria Chen", "affiliation": "Stanford Medicine"},
                {"name": "Omar Haddad", "affiliation": "UCSF"},
            ],
            host_org="Stanford Medicine",
            event_type="lecture",
            location="Li Ka Shing Center, Berg Hall",
            is_online=True,
            abstract="Hybrid event; the livestream link is on the detail page.",
            fields=["med", "bio"],
        ),
        _event(
            "sample-all-day",
            "Bay Area Symposium on Structural Biology",
            _at(11, 20, 0),
            end=_at(11, 21, 0),
            all_day=True,
            host_org="SLAC",
            institution="slac",
            event_type="conference",
            location="SLAC, Kavli Auditorium",
            abstract="A full day of talks on cryo-EM, crystallography and structure prediction.",
            fields=["chem", "bio", "physics"],
            picks_score=4.5,
            picks_reasons=[
                '"cryo-EM" in abstract (+3)',
                '"structure prediction" in abstract (+3)',
                'penalty "undergraduate" (-1.5)',
            ],
        ),
        _event(
            "sample-tentative-physics",
            "Dark matter searches with quantum sensors",
            _at(11, 23, 15, 30),
            speakers=[{"name": "Kenji Watanabe", "affiliation": "KIPAC"}],
            series="SLAC Colloquium",
            host_org="SLAC",
            institution="slac",
            event_type="colloquium",
            location="Panofsky Auditorium",
            fields=["physics"],
            status="tentative",
        ),
        _event(
            "sample-biohub",
            "Single-cell atlases of the immune system",
            _at(12, 3, 11),
            speakers=[
                {"name": f"Lucia Fern{A_ACUTE}ndez", "affiliation": "CZ Biohub San Francisco"}
            ],
            series="Biohub Seminar Series",
            host_org="CZ Biohub",
            institution="biohub",
            location="499 Illinois Street, San Francisco",
            fields=["bio", "med"],
        ),
        _event(
            "sample-arc",
            "Language models for genome engineering",
            _at(12, 10, 13),
            speakers=[{"name": "Sam Okafor", "affiliation": "Arc Institute"}],
            host_org="Arc Institute",
            institution="arc",
            event_type="workshop",
            location="Arc Institute, Palo Alto",
            fields=["ai-ml", "bio"],
            picks_score=3.5,
            picks_reasons=['"language model" in title (+3)', 'speaker "Sam Okafor" (+0.5)'],
        ),
        _event(
            "sample-defense",
            "PhD Defense: verified compilers for accelerators",
            _at(2, 12, 10, year=2027),
            speakers=[{"name": "Alex Kim"}],
            host_org="Stanford Electrical Engineering",
            institution="other",
            event_type="defense",
            location="Packard 101",
            fields=["cs"],
        ),
    ]


def sample_events_payload() -> PublicEventsPayload:
    """The content of tests/fixtures/_site/events.json."""
    ordered = sorted(sample_events(), key=lambda event: (event.start, event.uid))
    return PublicEventsPayload(
        generated_at=GENERATED_AT,
        timezone="America/Los_Angeles",
        events=[
            PublicEvent.from_event(event, picks_threshold=PICKS_THRESHOLD) for event in ordered
        ],
    )


def sample_status_payload() -> PublicStatusPayload:
    """The content of tests/fixtures/_site/status.json."""
    ran_at = datetime(2026, 11, 10, 12, 23, tzinfo=UTC)
    week_ago = datetime(2026, 11, 3, 12, 24, tzinfo=UTC)
    sources = [
        PublicSourceStatus(
            id="bayarea-arc",
            name="Arc Institute",
            group="bayarea",
            url="https://arcinstitute.org/",
            state="empty",
            last_run_at=ran_at,
            last_success_at=week_ago,
        ),
        PublicSourceStatus(
            id="custom-nlp",
            name="Stanford NLP Seminar <script>alert('xss-status')</script>",
            group="stanford-custom",
            url="https://nlp.stanford.edu/seminar",
            state="failed",
            last_run_at=ran_at,
            last_success_at=week_ago,
            consecutive_failures=3,
            error="HTTP 503: https://nlp.stanford.edu/seminar <img src=x onerror=alert(1)>",
        ),
        PublicSourceStatus(
            id="custom-theory",
            name="Stanford Theory Seminar",
            group="stanford-custom",
            url="https://theory.stanford.edu/",
            state="disabled",
        ),
        PublicSourceStatus(
            id="localist-stanford",
            name="Stanford Events",
            group="localist",
            url="https://events.stanford.edu/",
            state="ok",
            events=85,
            last_run_at=ran_at,
            last_success_at=ran_at,
        ),
        PublicSourceStatus(
            id="rsorg-new-series",
            name="researchseminars.org: a series that never ran",
            group="researchseminars",
            state="unknown",
        ),
        PublicSourceStatus(
            id="sites-chemistry",
            name="Stanford Chemistry",
            group="stanford-sites",
            url="https://chemistry.stanford.edu/events/upcoming-events",
            state="ok",
            events=12,
            last_run_at=ran_at,
            last_success_at=ran_at,
        ),
        PublicSourceStatus(
            id="sites-mathematics",
            name="Stanford Mathematics",
            group="stanford-sites",
            url="https://mathematics.stanford.edu/events",
            state="ok",
            events=4,
            last_run_at=ran_at,
            last_success_at=ran_at,
            read_in_full=False,
            skipped_pages=2,
            listed=5,
            usual_size=20,
        ),
        PublicSourceStatus(
            id="sites-statistics",
            name="Stanford Statistics",
            group="stanford-sites",
            url="https://statistics.stanford.edu/events",
            state="ok",
            events=6,
            last_run_at=ran_at,
            last_success_at=ran_at,
            listed=6,
            resized_from=18,
        ),
    ]
    return PublicStatusPayload(
        generated_at=GENERATED_AT,
        last_run_at=datetime(2026, 11, 10, 12, 29, tzinfo=UTC),
        last_run_ok=True,
        event_count=len(sample_events()),
        llm_provider="claude-cli",
        sources=sources,
    )


def sample_files() -> dict[Path, str]:
    """Path and expected text of each committed sample file."""
    return {
        SAMPLE_DIR / "events.json": dump_json(sample_events_payload()),
        SAMPLE_DIR / "status.json": dump_json(sample_status_payload()),
    }


if __name__ == "__main__":
    for path, text in sample_files().items():
        write_text_atomic(path, text)
        print(f"wrote {path}")
