"""The abstract of a Localist record keeps the blocks of its description apart.

``description_text`` runs adjacent HTML blocks together ("OverviewThis
one-hour activity", "Steve RamirezBoston University"); ``description`` is
the same text with its headings and paragraphs, which normalization turns
into lines. So the HTML is the abstract, and the plain text only stands in
when a record has no HTML with text in it.
"""

from __future__ import annotations

import functools
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from factories import make_document, make_payload, make_source, make_window
from seminars.config.loader import load_sources
from seminars.http import offline_client
from seminars.models import EventDraft
from seminars.pipeline.text import html_to_text
from seminars.sources.generic.localist import LocalistAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]
RECORDED = datetime(2026, 11, 11, 15, 49, 33, tzinfo=UTC)
ENDPOINT = "https://calendar.example.edu/api/2/events"


@functools.cache
def recorded() -> dict[str, EventDraft]:
    sources = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
    source = sources["localist-stanford"]
    adapter = LocalistAdapter()
    payload = adapter.fetch(
        offline_client("localist-stanford"), source, window=make_window(RECORDED)
    )
    return {draft.native_id or "": draft for draft in adapter.parse(payload, source)}


def parse(**fields: Any) -> EventDraft:
    instance = {"id": 1, "start": "2026-11-17T16:00:00-08:00", "end": None, "all_day": False}
    item = {"id": 10, "title": "A talk", "event_instances": [{"event_instance": instance}]}
    text = json.dumps(
        {"events": [{"event": {**item, **fields}}], "page": {"current": 1, "next_page": None}}
    )
    document = make_document(url=f"{ENDPOINT}?page=1", text=text, content_type="application/json")
    source = make_source(
        id="localist-example", group="localist", adapter="localist", url=ENDPOINT, params={}
    )
    (draft,) = LocalistAdapter().parse(make_payload(document, source_id=source.id), source)
    return draft


class TestRecordedCalendar:
    def test_a_heading_and_its_paragraph_stay_apart(self) -> None:
        draft = recorded()["53809595903126"]
        assert draft.abstract is None
        assert draft.abstract_html is not None
        lines = [line.strip() for line in html_to_text(draft.abstract_html).split("\n")]
        assert "Overview" in lines
        assert "OverviewThis" not in html_to_text(draft.abstract_html)

    def test_every_record_with_a_description_gives_its_html(self) -> None:
        drafts = recorded().values()
        assert drafts
        assert all(draft.abstract is None for draft in drafts if draft.abstract_html)


class TestRecords:
    def test_the_html_description_is_the_abstract(self) -> None:
        draft = parse(
            description_text="Steve RamirezBoston University",
            description="<h4>Steve Ramirez</h4><p>Boston University</p>",
        )
        assert (draft.abstract, draft.abstract_html) == (
            None,
            "<h4>Steve Ramirez</h4><p>Boston University</p>",
        )

    @pytest.mark.parametrize("description", [None, "", "  ", "<p> </p>", "<p><br></p>"])
    def test_the_plain_text_stands_in_for_html_without_text(self, description: str | None) -> None:
        draft = parse(description_text="Abstract by Fran\u00e7ois", description=description)
        assert (draft.abstract, draft.abstract_html) == ("Abstract by Fran\u00e7ois", None)

    def test_neither_gives_no_abstract(self) -> None:
        draft = parse(description_text=" ", description=None)
        assert (draft.abstract, draft.abstract_html) == (None, None)
