"""Browser tests of the talk list while events.json is still on its way.

A bookmarked or shared link holds filters in its address. Until the talks arrive the
address is the state of the page: the controls show its filters, and a change made in
the meantime (typing, a select, the picks box) is added to them instead of replacing
them. Once the talks are there the filters are checked against what exists.

events.json is held back by a route of the test and released when the test says so; the
page clock is paused as in test_site_browser_search.py. The server, the browser and the
sessions are those of test_site_browser.py; its fixtures are bound to names of this
module, so pytest finds them here as well. Marked ``browser`` like that module:
``uv run pytest -m browser``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

import test_site_browser as harness
from test_site_browser import READY, UPCOMING, Session, catalog
from test_site_browser_search import SEARCH_DELAY_MS, paused

pytestmark = pytest.mark.browser

site_dir = harness.site_dir
base_url = harness.base_url
browser = harness.browser
session = harness.session

# The messages are in the page (the search box has its placeholder).
TRANSLATED = "() => document.getElementById('search').placeholder !== ''"

PRESSED = "es => es.map((e) => e.dataset.value)"


@dataclass
class Loading:
    """A talk list whose events.json request is held until ``arrive``."""

    current: Session
    held: list[Any]

    @property
    def page(self) -> Any:
        return self.current.page

    def arrive(self) -> None:
        assert len(self.held) == 1
        self.held[0].continue_()
        self.page.wait_for_function(READY)

    def pressed(self, container: str) -> list[str]:
        return self.page.eval_on_selector_all(f"{container} button[aria-pressed=true]", PRESSED)


def loading(session: Callable[..., Session], path: str) -> Loading:
    """Open ``path`` and wait for the messages, with events.json held back."""
    current = paused(session, open_page=False)
    held: list[Any] = []

    def hold(request_route: Any) -> None:
        held.append(request_route)

    current.page.route("**/events.json", hold)
    current.page.goto(current.base_url + path)
    current.page.wait_for_function(TRANSLATED)
    assert current.shown() == []
    assert len(held) == 1
    return Loading(current=current, held=held)


class TestWhileTheTalksLoad:
    def test_text_typed_on_a_bare_page_is_kept(self, session: Callable[..., Session]) -> None:
        state = loading(session, "index.html")
        state.page.locator("#search").press_sequentially("zoe muller")
        state.arrive()
        assert state.page.locator("#search").input_value() == "zoe muller"
        assert state.current.shown() == ["sample-tba"]
        assert state.current.query() == {"q": ["zoe muller"]}
        state.current.assert_clean()

    @pytest.mark.parametrize("pause_before_the_talks", [True, False])
    def test_text_typed_into_a_shared_link_keeps_its_filters(
        self, session: Callable[..., Session], pause_before_the_talks: bool
    ) -> None:
        state = loading(session, "index.html?field=cs&range=week")
        page = state.page
        assert page.locator("#range").input_value() == "week"
        assert page.locator("#clear").is_enabled()
        page.locator("#search").press_sequentially("lunch")
        expected = {"field": ["cs"], "range": ["week"], "q": ["lunch"]}
        if pause_before_the_talks:
            page.clock.run_for(SEARCH_DELAY_MS)
            assert state.current.query() == expected
        state.arrive()
        assert state.current.query() == expected
        assert page.locator("#search").input_value() == "lunch"
        assert page.locator("#range").input_value() == "week"
        assert state.pressed("#field-chips") == ["cs"]
        assert state.current.shown() == ["sample-xss"]
        page.clock.run_for(10 * SEARCH_DELAY_MS)
        assert state.current.query() == expected
        state.current.assert_clean()

    def test_the_controls_show_the_link_and_a_change_is_added_to_it(
        self, session: Callable[..., Session]
    ) -> None:
        link = "field=ai-ml&inst=stanford&host=Stanford+Bioengineering&q=protein&lang=zh"
        state = loading(session, f"index.html?{link}")
        page = state.page
        assert page.locator("#search").input_value() == "protein"
        assert page.locator("#host").input_value() == "Stanford Bioengineering"
        assert page.locator("#range").input_value() == "all"
        assert not page.locator("#picks").is_checked()

        page.locator("#range").select_option("two_weeks")
        page.locator("#picks").check()
        expected = {
            "field": ["ai-ml"],
            "inst": ["stanford"],
            "host": ["Stanford Bioengineering"],
            "q": ["protein"],
            "range": ["two_weeks"],
            "picks": ["1"],
            "lang": ["zh"],
        }
        assert state.current.query() == expected
        notice = page.locator("#events .notice").text_content()
        assert notice == catalog("zh-CN")["site.meta.loading"]

        state.arrive()
        assert state.current.query() == expected
        assert state.current.shown() == ["sample-pick"]
        assert state.pressed("#field-chips") == ["ai-ml"]
        assert state.pressed("#institution-chips") == ["stanford"]
        assert page.locator("#host").input_value() == "Stanford Bioengineering"
        assert page.locator("#range").input_value() == "two_weeks"
        assert page.locator("#picks").is_checked()
        assert page.locator("#search").input_value() == "protein"
        state.current.assert_clean()

    def test_values_that_do_not_exist_are_dropped_once_the_talks_are_there(
        self, session: Callable[..., Session]
    ) -> None:
        state = loading(session, "index.html?field=nope&field=cs&inst=mars&host=Nobody")
        page = state.page
        assert page.locator("#host").input_value() == "Nobody"
        page.locator("#picks").check()
        assert state.current.query() == {
            "field": ["nope", "cs"],
            "inst": ["mars"],
            "host": ["Nobody"],
            "picks": ["1"],
        }
        state.arrive()
        assert state.pressed("#field-chips") == ["cs"]
        assert state.pressed("#institution-chips") == []
        assert page.locator("#host").input_value() == ""
        assert state.current.shown() == []
        page.locator("#picks").uncheck()
        assert state.current.query() == {"field": ["cs"]}
        assert state.current.shown() == ["sample-xss", "sample-online", "sample-defense"]
        page.locator("#clear").click()
        assert state.current.shown() == UPCOMING
        state.current.assert_clean()
