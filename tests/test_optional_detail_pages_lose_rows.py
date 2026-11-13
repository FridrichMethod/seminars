"""When an optional detail page of ``html_list`` still loses its row.

``detail_page.required: false`` keeps a row whose page could not be fetched,
with what the list page gives, so such a page loses no talk and the source
still counts as read in full. Two settings drop the row all the same: tags
that must be on the page (``keep_tags``) and times that only the page has (no
``selectors.date``). A failed page of such a source is a skipped page, as for
a required one.
"""

from __future__ import annotations

from typing import Any

import pytest

from factories import REFERENCE_NOW, make_source
from fakes import FakeClock, FakeHttpClient
from seminars.config import AppConfig
from seminars.models import SourceConfig, SourceRunResult
from seminars.pipeline.collect import collect_sources
from seminars.pipeline.normalize import NormalizeContext
from seminars.sources.generic.common import detail_pages_optional

LIST = "https://talks.example.edu/seminar/"
ROW = (
    "<li><h3><a href='/talks/{slug}'>{title}</a></h3><span class='date'>{day}</span>"
    "<span class='time'>4:30 - 5:30pm</span></li>"
)
PAGE = (
    "<html><body><time datetime='{day}T16:30:00-08:00'>4:30</time>"
    "<p class='place'>Room 1</p><span class='tag'>Seminars</span></body></html>"
)
SELECTORS = {"item": "ul > li", "title": "h3", "link": "h3 a", "date": ".date", "time": ".time"}


def source(*, selectors: dict[str, str] = SELECTORS, **params: Any) -> SourceConfig:
    detail = {"location": ".place", "times": "time", "tag": ".tag", "required": False}
    values = {
        "selectors": selectors,
        "link_attribute": "href",
        "detail_page": {**detail, **params.pop("detail_page", {})},
        **params,
    }
    return make_source(
        id="custom-example", group="stanford-custom", adapter="html_list", url=LIST, params=values
    )


def site(day: str) -> FakeHttpClient:
    """Two rows; the detail page of the second one cannot be fetched."""
    rows = [
        ROW.format(slug=f"talk-{n}", title=f"Talk {n} on protein design", day=day) for n in (1, 2)
    ]
    http = FakeHttpClient()
    http.add(LIST, "<html><body><ul>" + "".join(rows) + "</ul></body></html>")
    http.add("https://talks.example.edu/talks/talk-1", PAGE.format(day=day))
    http.add("https://talks.example.edu/talks/talk-2", "gone", status=500)
    return http


@pytest.fixture
def context(app_config: AppConfig) -> NormalizeContext:
    settings = app_config.settings
    return NormalizeContext(
        settings=settings, now=REFERENCE_NOW, window=settings.window_at(REFERENCE_NOW)
    )


def collect(context: NormalizeContext, config: SourceConfig) -> SourceRunResult:
    day = (REFERENCE_NOW.date()).isoformat()
    [outcome] = collect_sources(
        [config], lambda chosen: site(day), context=context, extractor=None, timer=FakeClock()
    )
    return outcome.result


WITHOUT_DATE = {key: value for key, value in SELECTORS.items() if key != "date"}


class TestWhichSourcesKeepTheirRows:
    def test_a_row_with_its_own_date_is_kept_without_its_page(self) -> None:
        assert detail_pages_optional(source())

    def test_rows_that_need_a_tag_of_the_page_are_not(self) -> None:
        assert not detail_pages_optional(source(keep_tags=["Seminars"]))

    def test_rows_whose_times_only_the_page_has_are_not(self) -> None:
        assert not detail_pages_optional(source(selectors=WITHOUT_DATE))

    def test_a_required_page_is_not_optional(self) -> None:
        assert not detail_pages_optional(source(detail_page={"required": True}))

    def test_a_source_without_detail_pages_is_not(self) -> None:
        config = make_source(
            id="custom-example", group="stanford-custom", params={"selectors": SELECTORS}
        )
        assert not detail_pages_optional(config)


class TestTheRunCountsWhatIsLost:
    def test_a_kept_row_leaves_the_source_read_in_full(self, context: NormalizeContext) -> None:
        result = collect(context, source())
        assert (result.drafts, result.skipped_pages, result.read_in_full) == (2, 0, True)

    @pytest.mark.parametrize(
        "changes",
        [
            pytest.param({"keep_tags": ["Seminars"]}, id="keep_tags"),
            pytest.param({"selectors": WITHOUT_DATE}, id="times from the page only"),
        ],
    )
    def test_a_dropped_row_makes_the_page_a_skipped_one(
        self, context: NormalizeContext, changes: dict[str, Any]
    ) -> None:
        result = collect(context, source(**changes))
        assert (result.drafts, result.skipped_pages, result.read_in_full) == (1, 1, False)
        assert result.warnings == [
            "1 page(s) could not be fetched and were left out: the source was not read in full"
        ]
