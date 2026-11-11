"""Factories and hypothesis strategies for the shared models.

Import them directly (``from factories import make_event``) or use the
fixtures of the same name defined in conftest.py. Every factory returns a
valid object and accepts keyword overrides for any field.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Final
from zoneinfo import ZoneInfo

from hypothesis import strategies as st

from seminars.models import (
    FIELD_IDS,
    Candidate,
    Event,
    EventDraft,
    EventHints,
    FetchedDocument,
    RawPayload,
    SourceConfig,
    Speaker,
    TimeWindow,
)

PACIFIC: Final = ZoneInfo("America/Los_Angeles")
REFERENCE_NOW: Final = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)
"""Fixed "now" for tests: Tuesday 2026-11-10 12:00 in Pacific Standard Time."""


def make_window(
    now: datetime = REFERENCE_NOW, *, days_back: int = 14, days_ahead: int = 120
) -> TimeWindow:
    """The default run window (14 days back, 120 days ahead) around ``now``."""
    return TimeWindow.around(
        now, days_back=days_back, days_ahead=days_ahead, timezone="America/Los_Angeles"
    )


def make_speaker(**overrides: Any) -> Speaker:
    defaults: dict[str, Any] = {"name": "Jane Doe", "affiliation": "Stanford University"}
    return Speaker.model_validate({**defaults, **overrides})


def make_event(**overrides: Any) -> Event:
    """A confirmed in-person seminar one week after REFERENCE_NOW."""
    url = "https://bioengineering.stanford.edu/events/protein-design-seminar"
    defaults: dict[str, Any] = {
        "uid": "sites-bioengineering-1234",
        "title": "Protein design with diffusion models",
        "speakers": [make_speaker()],
        "series": "Bioengineering Seminar",
        "host_org": "Stanford Bioengineering",
        "institution": "stanford",
        "event_type": "seminar",
        "start": datetime(2026, 11, 17, 16, 0, tzinfo=PACIFIC),
        "end": datetime(2026, 11, 17, 17, 0, tzinfo=PACIFIC),
        "all_day": False,
        "location": "Shriram Center, Room 104",
        "is_online": False,
        "stream_url": None,
        "abstract": "A talk about generative models for de novo protein design.",
        "url": url,
        "source_urls": [url],
        "source_ids": ["sites-bioengineering:1234"],
        "fields": ["bio"],
        "field_tag_method": "source",
        "picks_score": 0.0,
        "picks_reasons": [],
        "status": "confirmed",
        "extraction": "feed",
        "first_seen": REFERENCE_NOW,
        "last_changed": REFERENCE_NOW,
        "missing_runs": 0,
        "sequence": 0,
    }
    return Event.model_validate({**defaults, **overrides})


def make_draft(**overrides: Any) -> EventDraft:
    """A draft as a feed adapter would return it for ``make_event()``."""
    defaults: dict[str, Any] = {
        "source_id": "sites-bioengineering",
        "extraction": "feed",
        "native_id": "1234",
        "url": "https://bioengineering.stanford.edu/events/protein-design-seminar",
        "title": "Protein design with diffusion models",
        "start": datetime(2026, 11, 17, 16, 0, tzinfo=PACIFIC),
        "end": datetime(2026, 11, 17, 17, 0, tzinfo=PACIFIC),
        "speaker_text": "Jane Doe (Stanford University)",
        "location": "Shriram Center, Room 104",
        "abstract": "A talk about generative models for de novo protein design.",
    }
    return EventDraft.model_validate({**defaults, **overrides})


def make_source(**overrides: Any) -> SourceConfig:
    """A source entry in the stanford-sites group."""
    defaults: dict[str, Any] = {
        "id": "sites-bioengineering",
        "name": "Stanford Bioengineering",
        "group": "stanford-sites",
        "adapter": "rss",
        "url": "https://bioengineering.stanford.edu/events/all/rss",
        "institution": "stanford",
        "host_org": "Stanford Bioengineering",
        "default_fields": ["bio"],
    }
    return SourceConfig.model_validate({**defaults, **overrides})


def make_candidate(event: Event | None = None, **hint_overrides: Any) -> Candidate:
    """An unclassified candidate: empty ``fields`` plus hints from one source."""
    base = event if event is not None else make_event(fields=[], field_tag_method="source")
    hints: dict[str, Any] = {"source_ids": ["sites-bioengineering"], "default_fields": ["bio"]}
    return Candidate(event=base, hints=EventHints.model_validate({**hints, **hint_overrides}))


def make_document(
    url: str = "https://bioengineering.stanford.edu/events/all/rss",
    text: str = "<rss></rss>\n",
    **overrides: Any,
) -> FetchedDocument:
    defaults: dict[str, Any] = {
        "url": url,
        "final_url": url,
        "status": 200,
        "content_type": "application/rss+xml",
        "text": text,
        "fetched_at": REFERENCE_NOW,
    }
    return FetchedDocument.model_validate({**defaults, **overrides})


def make_payload(*documents: FetchedDocument, **overrides: Any) -> RawPayload:
    defaults: dict[str, Any] = {
        "source_id": "sites-bioengineering",
        "fetched_at": REFERENCE_NOW,
        "window": make_window(),
        "documents": list(documents) or [make_document()],
    }
    return RawPayload.model_validate({**defaults, **overrides})


# --- hypothesis strategies ------------------------------------------------------------------

_FIXED_OFFSETS: Final = (
    UTC,
    timezone(timedelta(hours=-8)),
    timezone(timedelta(hours=-7)),
    timezone(timedelta(hours=5, minutes=30)),
)
_TEXT: Final = st.text(st.characters(exclude_categories=["Cs"]), min_size=1, max_size=30)
_SLUG: Final = st.from_regex(r"[a-z0-9]{1,12}", fullmatch=True)


def aware_datetimes() -> st.SearchStrategy[datetime]:
    """Aware datetimes with fixed offsets, the form stored in data/events.json."""
    return st.datetimes(
        min_value=datetime(2020, 1, 1),
        max_value=datetime(2035, 1, 1),
        timezones=st.sampled_from(_FIXED_OFFSETS),
    )


def http_urls() -> st.SearchStrategy[str]:
    return st.builds(lambda host, path: f"https://{host}.example.edu/{path}", _SLUG, _SLUG)


@st.composite
def events(draw: st.DrawFn) -> Event:
    """Arbitrary valid events, including odd text such as control characters."""
    start = draw(aware_datetimes())
    duration = draw(st.none() | st.integers(min_value=0, max_value=3 * 24 * 60))
    speakers = st.builds(Speaker, name=_TEXT, affiliation=st.none() | _TEXT)
    return make_event(
        uid=draw(st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._-]{0,20}", fullmatch=True)),
        title=draw(_TEXT),
        speakers=draw(st.lists(speakers, max_size=3)),
        series=draw(st.none() | _TEXT),
        event_type=draw(st.sampled_from(["seminar", "colloquium", "defense", "other"])),
        start=start,
        end=None if duration is None else start + timedelta(minutes=duration),
        all_day=draw(st.booleans()),
        location=draw(st.none() | _TEXT),
        abstract=draw(st.none() | _TEXT),
        url=draw(http_urls()),
        source_urls=draw(st.lists(http_urls(), min_size=1, max_size=3)),
        source_ids=draw(st.lists(_SLUG, min_size=1, max_size=3)),
        fields=draw(st.lists(st.sampled_from(FIELD_IDS), max_size=4)),
        field_tag_method=draw(st.sampled_from(["source", "rules", "llm"])),
        picks_score=draw(st.floats(min_value=-50, max_value=50, allow_nan=False)),
        picks_reasons=draw(st.lists(_TEXT, max_size=2)),
        status=draw(st.sampled_from(["confirmed", "tentative", "cancelled"])),
        first_seen=draw(aware_datetimes()),
        last_changed=draw(aware_datetimes()),
        missing_runs=draw(st.integers(min_value=0, max_value=3)),
        sequence=draw(st.integers(min_value=0, max_value=20)),
    )


def event_lists(max_size: int = 8) -> st.SearchStrategy[list[Event]]:
    """Lists of events with distinct uids."""
    return st.lists(events(), max_size=max_size, unique_by=lambda event: event.uid)
