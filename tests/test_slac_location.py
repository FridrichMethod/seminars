"""Where a talk of the SLAC Drupal theme takes place.

A card can name its place in ``.c-card__location`` ("Kavli Auditorium and
zoom", after a location icon whose title is "Location"). The upcoming cards
of the SLAC Colloquium leave it out; its home page says that the in-person
part of every talk is in the Kavli Auditorium, which ``default_location``
gives a card that names no place.
"""

from __future__ import annotations

import functools
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from factories import make_source, make_window
from fakes import FakeHttpClient
from seminars.config import load_sources
from seminars.http import offline_client
from seminars.models import EventDraft, SourceConfig
from seminars.sources.custom.slac_drupal import SlacDrupalAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]
RECORDED = datetime(2026, 11, 11, 17, 28, tzinfo=UTC)
LIST = "https://events.example.org/upcoming"
CARD_DATE = "Monday, November 16, 2026 \u00b7 4:00 p.m.\u20135:00 p.m. PT"
LOCATION_ICON = '<svg class="c-icon"><title>Location</title><use></use></svg>'


@functools.cache
def colloquium() -> SourceConfig:
    sources = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
    return sources["bayarea-slac-colloquium"]


def run(source: SourceConfig, http: Any) -> list[EventDraft]:
    adapter = SlacDrupalAdapter()
    return adapter.parse(adapter.fetch(http, source, window=make_window(RECORDED)), source)


def card(location: str | None) -> str:
    place = f'<div class="c-card__location">{LOCATION_ICON} {location} </div>' if location else ""
    return (
        '<div class="c-view__row"><div class="c-card">'
        '<h3 class="c-card__title"><a href="/events/a-talk">A talk</a></h3>'
        f'<div class="c-card__date">{CARD_DATE}</div>{place}</div></div>'
    )


def example_source(**params: Any) -> SourceConfig:
    cards = {
        "card": "div.c-view__row div.c-card",
        "title": "h3.c-card__title a",
        "link": "h3.c-card__title a",
        "date_text": ".c-card__date",
        "location": ".c-card__location",
    }
    return make_source(
        id="bayarea-example",
        group="bayarea",
        adapter="slac_drupal",
        url=LIST,
        institution="slac",
        default_fields=["physics"],
        params={"list": cards, **params},
    )


def one(source: SourceConfig, location: str | None) -> EventDraft:
    http = FakeHttpClient({LIST: f"<html><body>{card(location)}</body></html>"})
    (draft,) = run(source, http)
    return draft


class TestCards:
    def test_the_place_of_a_card_without_its_icon(self) -> None:
        draft = one(example_source(), "Kavli Auditorium and zoom")
        assert draft.location == "Kavli Auditorium and zoom"

    def test_a_card_without_a_place_has_none(self) -> None:
        assert one(example_source(), None).location is None

    def test_the_default_place_fills_a_card_without_one(self) -> None:
        source = example_source(default_location="Kavli Auditorium")
        assert one(source, None).location == "Kavli Auditorium"
        assert one(source, "Panofsky Auditorium").location == "Panofsky Auditorium"


class TestRecordedColloquium:
    def test_every_upcoming_talk_is_in_the_kavli_auditorium(self) -> None:
        source = colloquium()
        drafts = run(source, offline_client(source.id))
        assert len(drafts) == 4
        assert {draft.location for draft in drafts} == {"Kavli Auditorium"}
