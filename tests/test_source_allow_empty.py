"""An ``html_list`` page without items: empty only while the page is still the list.

``allow_empty`` lets a page whose ``selectors.item`` matches nothing give no
drafts, but only when ``empty_marker`` is on the page: an element that shows
the page is still the listing page (Biohub: its past events). Without it the
page was redesigned or replaced, and that is a ``ParseError``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from selectolax.lexbor import LexborHTMLParser

from factories import make_source, make_window
from fakes import FakeHttpClient
from seminars.config import load_sources
from seminars.models import EventDraft, SourceConfig
from seminars.sources.base import ParseError, get_adapter
from seminars.sources.generic.common import AdapterConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCES = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
BIOHUB = SOURCES["bayarea-biohub"]
BIOHUB_LIST = REPO_ROOT / "tests" / "fixtures" / "bayarea-biohub" / "events-e78c38af.html"
WINDOW = make_window(datetime(2026, 11, 12, 6, 37, 13, tzinfo=UTC))
LIST_URL = "https://talks.example.edu/seminar/"


def run(source: SourceConfig, http: Any) -> list[EventDraft]:
    adapter = get_adapter(source.adapter)
    return adapter.parse(adapter.fetch(http, source, window=WINDOW), source)


def biohub_page(*removed: str) -> str:
    """The recorded Biohub list without the elements that ``removed`` match."""
    tree = LexborHTMLParser(BIOHUB_LIST.read_text(encoding="utf-8"))
    for selector in removed:
        for node in tree.css(selector):
            node.decompose()
    return tree.html or ""


def serve(url: str, text: str) -> FakeHttpClient:
    http = FakeHttpClient()
    http.add(url, text)
    return http


def list_source(**params: Any) -> SourceConfig:
    base: dict[str, Any] = {
        "selectors": {"item": "ul.talks > li", "title": "h3", "date": ".date"},
    }
    return make_source(
        id="custom-example",
        group="stanford-custom",
        adapter="html_list",
        url=LIST_URL,
        params={**base, **params},
    )


class TestBiohub:
    def test_no_upcoming_event_while_the_past_events_are_listed_is_an_empty_list(self) -> None:
        text = biohub_page("ul.content-type-events li")
        http = serve(BIOHUB.all_urls[0], text)
        assert run(BIOHUB, http) == []
        assert http.requests == ["https://biohub.org/events/"]

    def test_a_page_without_upcoming_and_past_cards_is_broken(self) -> None:
        text = biohub_page("ul.content-type-events li", "ul.content-type-past_events li")
        with pytest.raises(ParseError, match="empty_marker"):
            run(BIOHUB, serve(BIOHUB.all_urls[0], text))

    def test_a_page_whose_cards_were_renamed_is_broken(self) -> None:
        text = BIOHUB_LIST.read_text(encoding="utf-8").replace(
            "multisite-query-loop-item", "event-card"
        )
        with pytest.raises(ParseError, match="matches nothing"):
            run(BIOHUB, serve(BIOHUB.all_urls[0], text))


class TestEmptyMarker:
    EMPTY = "<html><body><ul class='talks'></ul><nav class='archive'>Past talks</nav></body></html>"

    def test_the_marker_on_a_page_without_items_gives_no_drafts(self) -> None:
        source = list_source(allow_empty=True, empty_marker="nav.archive")
        assert run(source, serve(LIST_URL, self.EMPTY)) == []

    def test_a_page_without_items_and_without_the_marker_is_a_parse_error(self) -> None:
        source = list_source(allow_empty=True, empty_marker="nav.archive")
        text = "<html><body><p>We moved</p></body></html>"
        with pytest.raises(ParseError, match=r"empty_marker 'nav\.archive' is not on it"):
            run(source, serve(LIST_URL, text))

    def test_without_allow_empty_a_page_without_items_is_a_parse_error(self) -> None:
        with pytest.raises(ParseError, match="matches nothing"):
            run(list_source(), serve(LIST_URL, self.EMPTY))

    def test_items_need_no_marker(self) -> None:
        source = list_source(allow_empty=True, empty_marker="nav.archive")
        text = (
            "<html><body><ul class='talks'><li><h3>Talk</h3><span class='date'>2026-11-17"
            "</span></li></ul></body></html>"
        )
        [draft] = run(source, serve(LIST_URL, text))
        assert draft.title == "Talk"

    def test_a_page_redirected_off_the_source_is_never_empty(self) -> None:
        source = list_source(allow_empty=True, empty_marker="nav.archive")
        http = FakeHttpClient()
        http.add(LIST_URL, self.EMPTY, final_url="https://login.example.org/sso")
        with pytest.raises(ParseError, match="matches nothing"):
            run(source, http)

    @pytest.mark.parametrize(
        ("params", "message"),
        [
            ({"allow_empty": True}, "allow_empty needs empty_marker"),
            ({"empty_marker": "nav.archive"}, "empty_marker needs allow_empty"),
        ],
    )
    def test_allow_empty_and_the_marker_go_together(
        self, params: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(AdapterConfigError, match=message):
            run(list_source(**params), serve(LIST_URL, self.EMPTY))
