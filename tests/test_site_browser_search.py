"""Browser tests of the search box of the talk list.

Typing updates the list and the address once the typing pauses, so a long list is not
rebuilt on every key, and a browser that refuses to update the address does not stop
the list. The page clock is paused, so the pause is run with ``page.clock.run_for``
instead of being waited for. The server, the browser and the sessions are those of
test_site_browser.py; its fixtures are bound to names of this module, so pytest finds
them here as well. Marked ``browser`` like that module: ``uv run pytest -m browser``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

import test_site_browser as harness
from test_site_browser import NOW, READY, UPCOMING, Session

pytestmark = pytest.mark.browser

site_dir = harness.site_dir
base_url = harness.base_url
browser = harness.browser
session = harness.session

SEARCH_DELAY_MS = 200  # as in site/app.js

# Counts the address updates.
COUNT_UPDATES = """
window.__addressUpdates = 0;
const replaceState = history.replaceState.bind(history);
history.replaceState = (...args) => {
  window.__addressUpdates += 1;
  return replaceState(...args);
};
"""

# Marks the first day of the list; a rebuilt list no longer holds the marked element.
MARK_LIST = "() => { window.__marked = document.querySelector('#events section'); }"
LIST_KEPT = "() => window.__marked.isConnected"

REFUSE_ADDRESS_UPDATES = """
history.replaceState = () => {
  throw new DOMException("too many address updates", "SecurityError");
};
"""


def paused(session: Callable[..., Session], *, open_page: bool = True) -> Session:
    """A session on the talk list whose clock only moves when the test moves it."""
    current = session()
    current.page.clock.install(time=NOW)
    current.page.clock.pause_at(NOW)
    current.page.add_init_script(COUNT_UPDATES)
    return current.open("index.html") if open_page else current


def updates(current: Session) -> int:
    """How often the address was updated so far."""
    return current.page.evaluate("window.__addressUpdates")


class TestSearchBox:
    def test_the_list_follows_the_typing_once_it_pauses(
        self, session: Callable[..., Session]
    ) -> None:
        current = paused(session)
        page = current.page
        before = updates(current)
        page.evaluate(MARK_LIST)
        page.locator("#search").press_sequentially("zoe muller")
        assert page.evaluate(LIST_KEPT) is True
        assert current.query() == {}
        assert updates(current) == before
        assert page.locator("#clear").is_enabled()

        page.clock.run_for(SEARCH_DELAY_MS - 1)
        assert page.evaluate(LIST_KEPT) is True
        page.clock.run_for(1)
        assert current.shown() == ["sample-tba"]
        assert current.query() == {"q": ["zoe muller"]}
        assert updates(current) == before + 1
        current.assert_clean()

    def test_enter_applies_the_search_at_once(self, session: Callable[..., Session]) -> None:
        current = paused(session)
        current.page.locator("#search").press_sequentially("evil corp")
        current.page.locator("#search").press("Enter")
        assert current.shown() == ["sample-xss"]
        assert current.query() == {"q": ["evil corp"]}
        before = updates(current)
        current.page.evaluate(MARK_LIST)
        current.page.clock.run_for(10 * SEARCH_DELAY_MS)
        assert updates(current) == before
        assert current.page.evaluate(LIST_KEPT) is True
        current.assert_clean()

    def test_another_filter_takes_the_typed_text_along(
        self, session: Callable[..., Session]
    ) -> None:
        current = paused(session)
        page = current.page
        page.locator("#search").press_sequentially("lunch")
        page.locator('#field-chips button[data-value="cs"]').click()
        assert page.locator("#search").input_value() == "lunch"
        assert current.query() == {"q": ["lunch"], "field": ["cs"]}
        assert current.shown() == ["sample-xss"]
        before = updates(current)
        page.evaluate(MARK_LIST)
        page.clock.run_for(10 * SEARCH_DELAY_MS)
        assert updates(current) == before
        assert page.evaluate(LIST_KEPT) is True

        # A click that leaves the focus in the box (as from assistive technology).
        page.locator("#search").press_sequentially("es")
        page.locator("#clear").dispatch_event("click")
        assert page.locator("#search").input_value() == ""
        assert current.query() == {}
        assert current.shown() == UPCOMING
        before = updates(current)
        page.evaluate(MARK_LIST)
        page.clock.run_for(10 * SEARCH_DELAY_MS)
        assert updates(current) == before
        assert page.evaluate(LIST_KEPT) is True
        current.assert_clean()

    def test_text_typed_while_the_talks_load_is_kept(self, session: Callable[..., Session]) -> None:
        current = paused(session, open_page=False)
        page = current.page
        held: list[Any] = []

        def hold(request_route: Any) -> None:
            held.append(request_route)

        page.route("**/events.json", hold)
        page.goto(current.base_url + "index.html")
        page.wait_for_function("() => document.getElementById('search').placeholder !== ''")
        page.locator("#search").press_sequentially("zoe muller")
        assert current.shown() == []
        assert len(held) == 1
        held[0].continue_()
        page.wait_for_function(READY)
        assert page.locator("#search").input_value() == "zoe muller"
        assert current.shown() == ["sample-tba"]
        assert current.query() == {"q": ["zoe muller"]}
        current.assert_clean()

    def test_a_refused_address_update_does_not_stop_the_list(
        self, session: Callable[..., Session]
    ) -> None:
        current = session()
        current.page.add_init_script(REFUSE_ADDRESS_UPDATES)
        warnings: list[str] = []
        current.page.on(
            "console",
            lambda message: warnings.append(message.text) if message.type == "warning" else None,
        )
        current.open("index.html")
        page = current.page
        page.locator('#field-chips button[data-value="cs"]').click()
        assert "sample-xss" in current.shown()
        assert "sample-tba" not in current.shown()
        page.locator("#search").fill("evil corp")
        page.locator("#search").press("Enter")
        assert current.shown() == ["sample-xss"]
        page.locator('.lang-switch button[lang="zh-CN"]').click()
        assert page.evaluate("document.documentElement.lang") == "zh-CN"
        assert current.shown() == ["sample-xss"]
        assert current.query() == {}
        assert len(warnings) == 3
        assert all("too many address updates" in warning for warning in warnings)
        current.assert_clean()
