"""Browser tests of the selection of talks on the talk list and its download.

Every listed talk has a checkbox named by its title, its day and its time, so talks of the
same title (TBA) are told apart. The bar above the list counts the
selection, says how many selected talks the filters hide, and offers Select all shown,
Clear selection and Download selected (.ics). A tick or a press of a bar button updates
the checkboxes and the bar in place, so the focus, the scroll position and the list
stay as they are. While talks are selected and the bar is scrolled away, a dock at the
bottom of the list shows their count and a second Download button. The saved file is
checked in Python: with the feed validator of the pipeline
(``seminars.outputs.ics.validate_ics``) and against a cut of the served feed made line by
line (``expected_calendar`` of test_site_browser_calendar.py). The server, the browser
and the sessions are those of test_site_browser.py; its fixtures are bound to names of
this module, so pytest finds them here as well. Marked ``browser`` like that module:
``uv run pytest -m browser``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import test_site_browser as harness
from seminars.models import TBA_TITLE
from seminars.outputs.ics import validate_ics
from seminars.textio import read_text
from site_sample import UID_DOMAIN, XSS_TITLE
from test_site_browser import (
    DESKTOP,
    OVERFLOW,
    PHONE,
    READY,
    UPCOMING,
    Session,
    assert_page_in,
    catalog,
    sample,
    serve_json,
    widen_text,
)
from test_site_browser_calendar import (
    FETCH_TIMEOUT_MS,
    block_of,
    crlf_only,
    expected_calendar,
    parts,
)
from test_site_browser_loading import loading
from test_site_browser_search import LIST_KEPT, MARK_LIST, paused

pytestmark = pytest.mark.browser

site_dir = harness.site_dir
base_url = harness.base_url
browser = harness.browser
session = harness.session

FEED_PATH = "feeds/all.ics"
FILE_NAME = "seminars-selected.ics"
STATUS = "#selection-status"
NOTE = "#selection-note"
SELECT_SHOWN = "#select-shown"
CLEAR = "#select-clear"
DOWNLOAD = "#select-download"
BUTTONS = (SELECT_SHOWN, CLEAR, DOWNLOAD)
DOCK = "#selection-dock"
DOCK_COUNT = "#selection-dock-count"
DOCK_DOWNLOAD = "#selection-dock-download"
MIDDLE_TALK = "sample-all-day"  # in the middle of the list
ONE_TIME_COPY = "#selection .selection-copy"  # a downloaded file is a one-time copy
SUBSCRIBE_LINK = f'{ONE_TIME_COPY} a[href="#subscribe"]'  # its last sentence
CS_TALKS = ["sample-xss", "sample-online", "sample-defense"]  # shown with field=cs
NO_TALK = "qqqzzz"  # a search that no talk matches
UID_VALUE = re.compile(r"^UID:([^\r\n]*)", re.MULTILINE)
UNFOLD = re.compile(r"\r\n[ \t]")
INJECTED = "script, img, svg, iframe, object, embed, b, i, u, [onerror], [onload]"
# Failures the download logs on purpose: the request the browser reports, the error of
# the page script.
FAILED_REQUEST = r"Failed to load resource: the server responded with a status of 404"
FEED_ERROR = r"feeds/all\.ics: HTTP status 404"

CHECKED = "es => es.filter((e) => e.checked).map((e) => e.dataset.uid)"
CHECKED_ONE = (
    '(uid) => document.querySelector(`input.select-talk[data-uid="${CSS.escape(uid)}"]`).checked'
)
# The uid of the focused checkbox, or the id of the focused element.
FOCUSED = "() => document.activeElement.dataset.uid || document.activeElement.id"
FOCUSED_TALK = "() => document.activeElement.closest('article')?.dataset.uid ?? null"
SCROLLED = "() => window.scrollY"
# The box of each checkbox in the viewport.
BOXES = """() => [...document.querySelectorAll("input.select-talk")].map((box) => {
  const rect = box.getBoundingClientRect();
  return { left: rect.left, right: rect.right, width: rect.width, height: rect.height };
})"""
# Scroll anchoring off, as in Safari: a change of height above the viewport moves what is
# on screen instead of the scroll position.
NO_SCROLL_ANCHORING = """() => {
  document.documentElement.style.overflowAnchor = "none";
  document.body.style.overflowAnchor = "none";
}"""
# Scroll so that the bottom of the bar is a fifth of the way down the viewport, with the
# first talks of the list below it.
BAR_ON_SCREEN = """() => {
  const bottom = document.getElementById("selection").getBoundingClientRect().bottom;
  window.scrollTo(0, window.scrollY + bottom - window.innerHeight / 5);
}"""
# What names the checkbox of a talk, in order: the hidden label, the title of the talk,
# the heading of its day and its time.
NAME_PARTS = """(uid) => {
  const article = document.querySelector(`#events article[data-uid="${CSS.escape(uid)}"]`);
  return [
    document.getElementById("select-label"),
    article.querySelector(".event-title"),
    article.closest(".day").querySelector(".day-heading"),
    article.querySelector(".event-when"),
  ].map((element) => element.textContent.trim());
}"""
LISTED = "es => es.map((e) => e.dataset.uid)"
# Where a same-page link leads: the id of its target and what labels that target.
LINK_TARGET = """(selector) => {
  const link = document.querySelector(selector);
  const target = document.getElementById(link.hash.slice(1));
  return {
    same_page: link.href.split("#")[0] === location.href.split("#")[0],
    id: target?.id ?? null,
    labelled_by: target?.getAttribute("aria-labelledby") ?? null,
  };
}"""
IN_VIEWPORT = """(selector) => {
  const rect = document.querySelector(selector).getBoundingClientRect();
  return rect.top >= 0 && rect.bottom <= window.innerHeight;
}"""
# Counts the changes below each of ``selectors`` (its children, their text and their
# attributes) in window.__changes; window.__alert keeps the alert of the note.
WATCH = """(selectors) => {
  window.__alert = document.querySelector("#selection-note [role=alert]");
  window.__changes = {};
  for (const selector of selectors) {
    window.__changes[selector] = 0;
    new MutationObserver((records) => {
      window.__changes[selector] += records.length;
    }).observe(document.querySelector(selector), {
      childList: true,
      characterData: true,
      attributes: true,
      subtree: true,
    });
  }
}"""
# The changes counted since the last call, and a new count from zero.
CHANGES = """() => {
  const changes = window.__changes;
  window.__changes = Object.fromEntries(Object.keys(changes).map((key) => [key, 0]));
  return changes;
}"""
SAME_ALERT = '() => document.querySelector("#selection-note [role=alert]") === window.__alert'
# Waits until the page has drawn two more frames: the bar is observed on or off screen.
SETTLE = "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))"
# Puts a talk in the middle of the viewport.
CENTER_TALK = """(uid) => document
  .querySelector(`#events article[data-uid="${CSS.escape(uid)}"]`)
  .scrollIntoView({ block: "center" })"""
# Where the dock is in the viewport, and whether any of the bar is on screen.
DOCK_PLACE = """() => {
  const dock = document.getElementById("selection-dock").getBoundingClientRect();
  const bar = document.getElementById("selection").getBoundingClientRect();
  return {
    top: dock.top,
    bottom: dock.bottom,
    viewport: window.innerHeight,
    bar: bar.bottom > 0 && bar.top < window.innerHeight,
  };
}"""
# Whether the bar is wholly below the viewport.
BAR_BELOW = "() => document.getElementById('selection').getBoundingClientRect().top >= innerHeight"
# Whether a click at the middle of an element reaches it: nothing covers it there.
REACHED = """(selector) => {
  const element = document.querySelector(selector);
  const rect = element.getBoundingClientRect();
  const x = rect.left + rect.width / 2;
  return element.contains(document.elementFromPoint(x, rect.top + rect.height / 2));
}"""
# Whether the focused element and its focus ring (2px outline, 2px offset) are above
# the dock; null while the dock is hidden or holds the focus.
FOCUS_CLEAR = """() => {
  const dock = document.getElementById("selection-dock");
  const focused = document.activeElement;
  if (!dock.classList.contains("is-shown") || dock.contains(focused)) {
    return null;
  }
  return focused.getBoundingClientRect().bottom + 4 <= dock.getBoundingClientRect().top;
}"""
# Focuses, without scrolling, the element that Tab leaves for the element ``selector``.
FOCUS_BEFORE = """(selector) => {
  const target = document.querySelector(selector);
  const stops = [...document.querySelectorAll("a[href], input, button, select, summary")]
    .filter((element) => element.getClientRects().length > 0);
  stops[stops.indexOf(target) - 1].focus({ preventScroll: true });
}"""
# Scrolls the page so that the last ``pixels`` of the bar are on screen, and returns the
# scroll position and the top of the Download button of the bar (above the viewport).
BAR_EDGE_ON_SCREEN = """(pixels) => {
  const bottom = document.getElementById("selection").getBoundingClientRect().bottom;
  window.scrollTo(0, window.scrollY + bottom - pixels);
  const top = document.getElementById("select-download").getBoundingClientRect().top;
  return { scrolled: window.scrollY, top };
}"""
# Counts the clicks that reach a button (a key press clicks it too).
COUNT_PRESSES = """(selector) => {
  window.__presses = 0;
  document.querySelector(selector).addEventListener("click", () => {
    window.__presses += 1;
  });
}"""
# The focused element, whether it is unavailable, and how its focus ring is drawn: the
# opacity of the element dims the ring with it. ``token`` is the focus colour of the page.
FOCUS_RING = """() => {
  const element = document.activeElement;
  const style = getComputedStyle(element);
  return {
    id: element.id,
    unavailable: element.getAttribute("aria-disabled"),
    visible: element.matches(":focus-visible"),
    opacity: style.opacity,
    outline: [style.outlineStyle, style.outlineWidth, style.outlineColor],
    border: style.borderTopStyle,
    token: getComputedStyle(document.documentElement).getPropertyValue("--focus").trim(),
  };
}"""
# Where the checkbox of a talk is in the viewport, the scroll position and the height of
# the bar.
PLACE = """(uid) => {
  const box = document.querySelector(`input.select-talk[data-uid="${CSS.escape(uid)}"]`);
  const rect = box.getBoundingClientRect();
  return {
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
    scrolled: window.scrollY,
    bar: document.getElementById("selection").getBoundingClientRect().height,
  };
}"""


def title_shown(uid: str, lang: str) -> str:
    """The title of a sample talk as the list shows it in ``lang``."""
    event = next(event for event in sample("events.json")["events"] if event["uid"] == uid)
    return catalog(lang)["site.event.tba"] if event["title"] == TBA_TITLE else event["title"]


def talk_name(current: Session, uid: str) -> str:
    """The accessible name the checkbox of a listed talk has: what NAME_PARTS reads."""
    return " ".join(current.page.evaluate(NAME_PARTS, uid))


def box_of(uid: str) -> str:
    """The selector of the checkbox of a sample talk (its uid needs no escaping)."""
    return f'input.select-talk[data-uid="{uid}"]'


def checkbox(current: Session, uid: str, lang: str = "en") -> Any:
    """The checkbox of a sample talk, found by its accessible name: the label, the title
    in ``lang``, the day and the time."""
    name = talk_name(current, uid)
    assert name.startswith(f"{catalog(lang)['site.select.talk']} {title_shown(uid, lang)} ")
    return current.page.get_by_role("checkbox", name=name, exact=True)


def counted(lang: str, base: str, count: int) -> str:
    """A plural message of ``lang`` for ``count`` (Chinese has one form)."""
    form = "one" if lang == "en" and count == 1 else "other"
    return catalog(lang)[f"{base}_{form}"].format(count=count)


def status_text(lang: str, selected: int, hidden: int = 0) -> str:
    """What the bar says about a selection of ``selected`` talks, ``hidden`` of them hidden."""
    if selected == 0:
        return catalog(lang)["site.select.none"]
    count = counted(lang, "site.select.count", selected)
    if hidden == 0:
        return count
    return catalog(lang)["site.select.with_hidden"].format(
        selected=count, hidden=counted(lang, "site.select.hidden", hidden)
    )


def checked(current: Session) -> list[str]:
    return current.page.eval_on_selector_all("input.select-talk", CHECKED)


def status(current: Session) -> str:
    return current.page.locator(STATUS).text_content() or ""


def note(current: Session) -> str:
    return current.page.locator(NOTE).text_content() or ""


def assert_one_time_copy_shown(current: Session, lang: str) -> None:
    """The bar shows, in ``lang``, that a downloaded file is a one-time copy, and links
    to the subscription panel."""
    messages = catalog(lang)
    copy = current.page.locator(ONE_TIME_COPY)
    link = current.page.locator(SUBSCRIBE_LINK)
    assert copy.is_visible()
    assert link.is_visible()
    assert copy.locator("span").text_content() == messages["site.select.one_time"]
    assert link.text_content() == messages["site.select.subscribe_link"]
    assert copy.text_content() == messages["site.select.one_time"] + link.text_content()


def in_viewport(current: Session, selector: str) -> bool:
    """The element ``selector`` is within the viewport (wholly, from top to bottom)."""
    return current.page.evaluate(IN_VIEWPORT, selector)


def select(current: Session, *uids: str) -> None:
    for uid in uids:
        checkbox(current, uid).check()


def search_for(current: Session, text: str) -> None:
    """Search the list for ``text`` at once (Enter, without the pause of typing)."""
    current.page.locator("#search").fill(text)
    current.page.locator("#search").press("Enter")


def type_search(current: Session, text: str, shown: list[str]) -> None:
    """Type ``text`` into the search box key by key and wait until the list is ``shown``."""
    page = current.page
    page.locator("#search").press_sequentially(text)
    page.wait_for_function(
        "(shown) => JSON.stringify([...document.querySelectorAll('#events article')]"
        ".map((article) => article.dataset.uid)) === JSON.stringify(shown)",
        arg=shown,
    )


def settle(current: Session) -> None:
    current.page.evaluate(SETTLE)


def dock_shown_at(current: Session, uid: str) -> None:
    """Scroll the talk ``uid`` (far below the bar, far above the end of the list) to the
    middle of the viewport, where the dock is shown at the bottom of the screen."""
    current.page.evaluate(CENTER_TALK, uid)
    current.page.locator(DOCK).wait_for(state="visible")
    place = current.page.evaluate(DOCK_PLACE)
    assert place["bar"] is False
    assert place["bottom"] == pytest.approx(place["viewport"], abs=1)


def download(current: Session, button: str = DOWNLOAD) -> str:
    """Press a Download button and return the text of the saved file."""
    page = current.page
    with page.expect_download() as info:
        page.locator(button).click()
    saved = info.value
    assert saved.suggested_filename == FILE_NAME
    return Path(saved.path()).read_bytes().decode("utf-8")


def assert_file_of(text: str, feed: str, uids: list[str]) -> None:
    """``text`` is a valid calendar of exactly the talks ``uids``, copied from ``feed``."""
    assert validate_ics(text) == []
    assert crlf_only(text)
    found = UID_VALUE.findall(UNFOLD.sub("", text))
    assert sorted(found) == sorted(f"{uid}@{UID_DOMAIN}" for uid in uids)
    wanted = set(uids)
    assert parts(text).events == [event for event in parts(feed).events if event[0] in wanted]
    assert text == expected_calendar(feed, uids)


def tap_in_place(current: Session, uid: str) -> None:
    """Tap the checkbox of a talk where it is; the bar, the box and the scroll stay put."""
    page = current.page
    before = page.evaluate(PLACE, uid)
    assert 0 < before["y"] < page.viewport_size["height"]
    ticked = page.evaluate(CHECKED_ONE, uid)
    page.mouse.click(before["x"], before["y"])
    assert page.evaluate(CHECKED_ONE, uid) is not ticked
    assert page.evaluate(PLACE, uid) == before


def rgb(color: str) -> str:
    """A colour of the form #rrggbb as getComputedStyle writes it."""
    red, green, blue = (int(color[index : index + 2], 16) for index in (1, 3, 5))
    return f"rgb({red}, {green}, {blue})"


def assert_focus_ring(current: Session, button_id: str) -> None:
    """The focused button is ``button_id``, unavailable, with the full focus ring."""
    ring = current.page.evaluate(FOCUS_RING)
    focus = rgb(ring.pop("token"))
    assert ring == {
        "id": button_id,
        "unavailable": "true",
        "visible": True,
        "opacity": "1",
        "outline": ["solid", "2px", focus],
        "border": "dashed",
    }


def drop_expected_errors(current: Session, *patterns: str) -> None:
    """Take the console errors a test causes on purpose out of the session.

    Each pattern has to match an error, and each error a pattern.
    """
    errors = current.console_errors
    assert [error for error in errors if not any(re.search(p, error) for p in patterns)] == []
    assert [p for p in patterns if not any(re.search(p, error) for error in errors)] == []
    errors.clear()


@pytest.fixture
def current(session: Callable[..., Session]) -> Session:
    return session().open("index.html")


@pytest.fixture(scope="module")
def feed(site_dir: Path) -> str:
    """The feed of every talk as the site serves it."""
    return read_text(site_dir / FEED_PATH)


class TestSelecting:
    def test_nothing_is_selected_at_first(self, current: Session) -> None:
        page = current.page
        assert page.locator("input.select-talk").count() == len(UPCOMING)
        assert checked(current) == []
        assert status(current) == catalog("en")["site.select.none"]
        # A screen reader hears the status and the note when they change, without a jump.
        assert page.locator(STATUS).get_attribute("role") == "status"
        assert page.locator(STATUS).get_attribute("aria-live") == "polite"
        assert page.locator(NOTE).get_attribute("aria-live") == "polite"
        assert page.locator(DOWNLOAD).is_disabled()
        assert page.locator(CLEAR).is_disabled()
        assert page.locator(SELECT_SHOWN).is_enabled()
        assert_one_time_copy_shown(current, "en")
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        page.locator(DOWNLOAD).dispatch_event("click")
        page.locator(CLEAR).dispatch_event("click")
        assert requests == []
        assert note(current) == ""
        assert status(current) == catalog("en")["site.select.none"]
        current.assert_clean()

    def test_a_click_selects_a_talk_and_keeps_the_focus(self, current: Session) -> None:
        page = current.page
        page.evaluate(MARK_LIST)
        box = checkbox(current, "sample-pick")
        box.click()
        assert box.is_checked()
        assert checked(current) == ["sample-pick"]
        assert page.evaluate(FOCUSED) == "sample-pick"
        assert status(current) == "1 talk selected"
        assert page.locator(DOWNLOAD).is_enabled()
        assert page.locator(CLEAR).is_enabled()
        box.click()
        assert checked(current) == []
        assert page.evaluate(FOCUSED) == "sample-pick"
        assert status(current) == catalog("en")["site.select.none"]
        assert page.locator(DOWNLOAD).is_disabled()
        assert page.evaluate(LIST_KEPT) is True
        current.assert_clean()

    def test_space_selects_a_talk_and_keeps_the_focus_and_the_scroll(
        self, current: Session
    ) -> None:
        page = current.page
        page.evaluate(MARK_LIST)
        box = checkbox(current, "sample-defense")
        # The keyboard reaches the checkbox: it is the stop before the title of its talk.
        page.locator('#events article[data-uid="sample-defense"] .event-title a').focus()
        page.keyboard.press("Shift+Tab")
        assert page.evaluate(FOCUSED) == "sample-defense"
        box.scroll_into_view_if_needed()
        scrolled = page.evaluate(SCROLLED)
        assert scrolled > 0
        page.keyboard.press("Space")
        assert checked(current) == ["sample-defense"]
        assert page.evaluate(FOCUSED) == "sample-defense"
        assert page.evaluate(SCROLLED) == scrolled
        page.keyboard.press("Space")
        assert checked(current) == []
        page.keyboard.press("Space")
        assert status(current) == "1 talk selected"
        assert page.evaluate(LIST_KEPT) is True
        # The checkbox comes before the links of its talk.
        page.keyboard.press("Tab")
        assert page.evaluate(FOCUSED_TALK) == "sample-defense"
        assert page.evaluate("document.activeElement.tagName") == "A"
        current.assert_clean()

    @pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=["phone", "desktop"])
    def test_a_tick_keeps_the_bar_and_the_list_in_place(
        self, session: Callable[..., Session], viewport: dict[str, int]
    ) -> None:
        current = session(viewport=viewport).open("index.html")
        page = current.page
        page.evaluate(NO_SCROLL_ANCHORING)
        page.evaluate(BAR_ON_SCREEN)
        tap_in_place(current, "sample-xss")  # the first tick: the count takes the hint's place
        download(current)
        page.locator(f"{NOTE} .note-saved").wait_for()
        page.evaluate(BAR_ON_SCREEN)
        tap_in_place(current, "sample-cancelled")  # the note on the download stays
        tap_in_place(current, "sample-cancelled")
        tap_in_place(current, "sample-xss")  # the last talk unticked: the hint is back
        assert status(current) == catalog("en")["site.select.none"]
        assert note(current) == counted("en", "site.select.saved", 1)
        assert_one_time_copy_shown(current, "en")
        current.assert_clean()

    def test_an_unchanged_status_is_not_written_again(self, current: Session) -> None:
        # The status is a live region: written again, it would be said again.
        page = current.page
        select(current, "sample-pick")
        page.evaluate(WATCH, [STATUS, "#events"])
        type_search(current, "protein", ["sample-pick"])
        assert page.evaluate(CHANGES)["#events"] > 0  # the list was drawn again
        page.locator("#picks").check()
        page.locator("#picks").uncheck()
        changes = page.evaluate(CHANGES)
        assert changes["#events"] > 0
        assert changes[STATUS] == 0
        assert current.shown() == ["sample-pick"]
        assert status(current) == "1 talk selected"
        current.assert_clean()

    def test_the_bar_counts_in_english_and_chinese(self, current: Session) -> None:
        page = current.page
        select(current, "sample-online")
        assert status(current) == "1 talk selected"
        select(current, "sample-arc")
        assert status(current) == "2 talks selected"
        page.locator('.lang-switch button[lang="zh-CN"]').click()
        assert status(current) == status_text("zh-CN", 2)
        assert checked(current) == ["sample-online", "sample-arc"]
        assert_page_in(current, "zh-CN")
        assert_one_time_copy_shown(current, "zh-CN")
        checkbox(current, "sample-arc", "zh-CN").uncheck()
        assert status(current) == status_text("zh-CN", 1)
        page.locator('.lang-switch button[lang="en"]').click()
        assert status(current) == "1 talk selected"
        assert_page_in(current, "en")
        current.assert_clean()

    @pytest.mark.parametrize("lang", ["en", "zh-CN"])
    def test_the_note_on_a_one_time_copy_links_to_the_subscription_panel(
        self, session: Callable[..., Session], lang: str
    ) -> None:
        # On a phone the panel comes after the whole list.
        current = session(viewport=PHONE).open(
            "index.html" if lang == "en" else "index.html?lang=zh"
        )
        page = current.page
        assert_one_time_copy_shown(current, lang)
        target = page.evaluate(LINK_TARGET, SUBSCRIBE_LINK)
        assert target == {"same_page": True, "id": "subscribe", "labelled_by": "subscribe-heading"}
        heading = page.locator("#subscribe-heading")
        assert heading.text_content() == catalog(lang)["site.subscribe.title"]
        assert not in_viewport(current, "#subscribe-heading")
        query = current.query()
        page.locator(SUBSCRIBE_LINK).click()
        assert page.evaluate("document.querySelector(':target')?.id") == "subscribe"
        assert in_viewport(current, "#subscribe-heading")
        assert current.query() == query
        assert_page_in(current, lang)
        current.assert_clean()

    @pytest.mark.parametrize("lang", ["en", "zh-CN"])
    def test_every_checkbox_is_named_by_its_title_day_and_time(
        self, session: Callable[..., Session], lang: str
    ) -> None:
        current = session().open("index.html" if lang == "en" else "index.html?lang=zh")
        for uid in UPCOMING:
            box = checkbox(current, uid, lang)
            assert box.count() == 1, uid
            assert box.get_attribute("data-uid") == uid
        if lang == "en":
            assert talk_name(current, "sample-tba") == "Select TBA Wednesday, November 18 4:30 PM"
        assert current.page.locator("input.select-talk[aria-label]").count() == 0
        current.assert_clean()

    def test_talks_of_the_same_title_have_different_names(
        self, session: Callable[..., Session]
    ) -> None:
        payload = sample("events.json")
        tba = next(event for event in payload["events"] if event["uid"] == "sample-tba")
        payload["events"] += [
            {**tba, "uid": "sample-tba-evening", "start": "2026-11-18T18:30:00-08:00"},
            {**tba, "uid": "sample-tba-next-day", "start": "2026-11-19T16:30:00-08:00"},
        ]
        current = session()
        serve_json(current, "events.json", payload)
        current.open("index.html")
        page = current.page
        listed = page.eval_on_selector_all("input.select-talk", LISTED)
        assert {"sample-tba", "sample-tba-evening", "sample-tba-next-day"} <= set(listed)
        names = [talk_name(current, uid) for uid in listed]
        assert len(set(names)) == len(names)
        for uid, name in zip(listed, names, strict=True):
            box = page.get_by_role("checkbox", name=name, exact=True)
            assert box.get_attribute("data-uid") == uid
        current.assert_clean()


class TestBarButtons:
    def test_select_all_shown_and_clear_selection(self, session: Callable[..., Session]) -> None:
        current = session().open("index.html?field=cs")
        page = current.page
        page.evaluate(MARK_LIST)
        select(current, "sample-online")
        page.locator(SELECT_SHOWN).focus()
        page.keyboard.press("Enter")
        assert checked(current) == CS_TALKS
        assert status(current) == "3 talks selected"
        assert page.evaluate(FOCUSED) == "select-shown"
        assert page.locator(SELECT_SHOWN).is_disabled()
        assert page.evaluate(LIST_KEPT) is True

        page.locator("#clear").click()
        assert current.shown() == UPCOMING
        assert checked(current) == CS_TALKS
        assert page.locator(SELECT_SHOWN).is_enabled()

        page.evaluate(MARK_LIST)
        page.locator(CLEAR).focus()
        page.keyboard.press("Enter")
        assert checked(current) == []
        assert status(current) == catalog("en")["site.select.none"]
        assert page.evaluate(FOCUSED) == "select-clear"
        assert page.locator(CLEAR).is_disabled()
        assert page.locator(DOWNLOAD).is_disabled()
        assert page.evaluate(LIST_KEPT) is True
        current.assert_clean()

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_an_unavailable_button_keeps_its_focus_ring(
        self, session: Callable[..., Session], scheme: str
    ) -> None:
        # Opacity would dim the ring along with the button; the unavailable look is
        # muted text and a dashed border instead.
        current = session(color_scheme=scheme).open("index.html")
        page = current.page
        select(current, "sample-pick")
        page.locator(SELECT_SHOWN).focus()
        page.keyboard.press("Enter")  # every shown talk is selected now
        assert_focus_ring(current, "select-shown")
        page.keyboard.press("Tab")
        page.keyboard.press("Enter")  # Clear selection: nothing is selected
        assert_focus_ring(current, "select-clear")
        page.keyboard.press("Tab")
        assert_focus_ring(current, "select-download")
        current.assert_clean()

    @pytest.mark.parametrize("talks_shown", [True, False], ids=["field", "no-talk"])
    def test_clear_selection_drops_the_talks_the_filters_hide_too(
        self, current: Session, talks_shown: bool
    ) -> None:
        page = current.page
        select(current, "sample-arc", "sample-xss")
        if talks_shown:
            page.locator('#field-chips button[data-value="cs"]').click()  # hides sample-arc
            assert current.shown() == CS_TALKS
            assert checked(current) == ["sample-xss"]
        else:
            search_for(current, NO_TALK)
            assert current.shown() == []
        assert status(current) == status_text("en", 2, hidden=1 if talks_shown else 2)
        page.locator(CLEAR).click()
        assert status(current) == catalog("en")["site.select.none"]
        assert page.locator(CLEAR).is_disabled()
        assert page.locator(DOWNLOAD).is_disabled()
        page.locator("#clear").click()
        assert current.shown() == UPCOMING
        assert checked(current) == []
        assert status(current) == catalog("en")["site.select.none"]
        current.assert_clean()

    def test_select_all_shown_adds_to_the_selection(self, current: Session) -> None:
        page = current.page
        select(current, "sample-arc")  # not a talk of the field cs
        page.locator('#field-chips button[data-value="cs"]').click()
        assert current.shown() == CS_TALKS
        page.locator(SELECT_SHOWN).click()
        assert checked(current) == CS_TALKS
        assert status(current) == status_text("en", 4, hidden=1)
        page.locator("#clear").click()
        assert checked(current) == ["sample-xss", "sample-online", "sample-arc", "sample-defense"]
        assert status(current) == "4 talks selected"
        current.assert_clean()

    def test_select_all_shown_is_unavailable_when_no_talk_is_shown(self, current: Session) -> None:
        page = current.page
        select(current, "sample-arc")
        search_for(current, NO_TALK)
        assert current.shown() == []
        assert status(current) == status_text("en", 1, hidden=1)
        assert page.locator(SELECT_SHOWN).is_disabled()
        assert page.locator(CLEAR).is_enabled()
        assert page.locator(DOWNLOAD).is_enabled()
        page.locator(SELECT_SHOWN).dispatch_event("click")
        assert status(current) == status_text("en", 1, hidden=1)
        page.locator("#clear").click()
        assert checked(current) == ["sample-arc"]
        assert status(current) == "1 talk selected"
        current.assert_clean()

    def test_a_selected_talk_the_filters_hide_is_counted_and_downloaded(
        self, current: Session, feed: str
    ) -> None:
        page = current.page
        select(current, "sample-cancelled", "sample-xss")
        page.locator('#field-chips button[data-value="cs"]').click()
        assert current.shown() == CS_TALKS
        assert checked(current) == ["sample-xss"]
        assert status(current) == "2 talks selected, 1 hidden by the filters"
        assert status(current) == status_text("en", 2, hidden=1)
        assert page.locator(SELECT_SHOWN).is_enabled()
        assert_file_of(download(current), feed, ["sample-xss", "sample-cancelled"])
        assert note(current) == counted("en", "site.select.saved", 2)
        page.locator('.lang-switch button[lang="zh-CN"]').click()
        assert status(current) == status_text("zh-CN", 2, hidden=1)
        assert note(current) == counted("zh-CN", "site.select.saved", 2)
        current.assert_clean()

    @pytest.mark.parametrize("talks_shown", [True, False], ids=["picks", "no-talk"])
    def test_a_selected_talk_that_ends_leaves_the_selection(
        self, session: Callable[..., Session], feed: str, talks_shown: bool
    ) -> None:
        # 12:30 in Stanford on November 12: sample-xss (12:00 to 13:00) is under way.
        current = session(now="2026-11-12T20:30:00Z").open("index.html")
        page = current.page
        select(current, "sample-xss", "sample-online")
        assert status(current) == "2 talks selected"
        page.clock.set_fixed_time("2026-11-12T21:30:00Z")  # 13:30: sample-xss has ended
        # The list is drawn again, with the picks or with no talk at all.
        if talks_shown:
            page.locator("#picks").check()
        else:
            search_for(current, NO_TALK)
            assert current.shown() == []
        assert status(current) == status_text("en", 1, hidden=1)
        page.locator("#clear").click()
        assert "sample-xss" not in current.shown()
        assert checked(current) == ["sample-online"]
        assert status(current) == "1 talk selected"
        assert page.locator(CLEAR).is_enabled()
        assert_file_of(download(current), feed, ["sample-online"])
        current.assert_clean()

    def test_the_selection_lives_in_the_page_only(self, current: Session) -> None:
        page = current.page
        select(current, "sample-tba", "sample-biohub")
        search_for(current, "zoe muller")
        assert current.shown() == ["sample-tba"]
        assert status(current) == status_text("en", 2, hidden=1)
        page.locator('.lang-switch button[lang="zh-CN"]').click()
        page.locator('.lang-switch button[lang="en"]').click()
        page.locator("#clear").click()
        assert checked(current) == ["sample-tba", "sample-biohub"]
        assert status(current) == "2 talks selected"
        assert page.url == current.base_url + "index.html"  # no query, no fragment
        stored = page.evaluate(
            "JSON.stringify([history.state, { ...localStorage, ...sessionStorage }])"
        )
        assert "sample-" not in stored
        page.reload()
        page.wait_for_function(READY)
        assert checked(current) == []
        assert status(current) == catalog("en")["site.select.none"]
        current.assert_clean()


class TestDownload:
    def test_the_file_holds_exactly_the_selected_talks(self, current: Session, feed: str) -> None:
        selected = ["sample-online", "sample-tba", "sample-arc"]  # in list order
        select(current, *reversed(selected))
        text = download(current)
        assert not text.isascii()  # the accented speaker name of the TBA talk
        assert_file_of(text, feed, selected)
        assert note(current) == "A calendar file with 3 talks was passed to your browser to save."
        assert current.page.locator(f"{NOTE} [role=alert]").count() == 0
        assert checked(current) == selected
        current.assert_clean()

    def test_a_cancelled_talk_is_selected_and_downloaded(self, current: Session, feed: str) -> None:
        selected = ["sample-cancelled", "sample-tentative-physics"]
        select(current, *selected)
        text = download(current)
        assert "STATUS:CANCELLED" in text
        assert "STATUS:TENTATIVE" in text
        assert_file_of(text, feed, selected)
        current.assert_clean()

    def test_the_button_is_busy_while_the_file_is_prepared(
        self, current: Session, feed: str
    ) -> None:
        page = current.page
        held: list[Any] = []
        page.route(f"**/{FEED_PATH}", lambda route: held.append(route))
        select(current, "sample-pick")
        page.locator(DOWNLOAD).focus()
        with page.expect_request(f"**/{FEED_PATH}"):
            page.keyboard.press("Enter")
        button = page.locator(DOWNLOAD)
        assert button.get_attribute("aria-busy") == "true"
        assert button.is_disabled()
        # The Download button of the dock shares the state.
        assert page.locator(DOCK_DOWNLOAD).get_attribute("aria-busy") == "true"
        assert page.locator(DOCK_DOWNLOAD).is_disabled()
        assert page.evaluate(FOCUSED) == "select-download"
        assert note(current) == catalog("en")["site.select.preparing"]
        page.keyboard.press("Enter")  # a second press while the file is prepared does nothing
        with page.expect_download() as info:
            held[0].continue_()
        assert_file_of(Path(info.value.path()).read_bytes().decode("utf-8"), feed, ["sample-pick"])
        page.locator(f"{NOTE} .note-saved").wait_for()
        assert len(held) == 1
        assert button.get_attribute("aria-busy") == "false"
        assert button.is_enabled()
        assert page.locator(DOCK_DOWNLOAD).get_attribute("aria-busy") == "false"
        assert page.locator(DOCK_DOWNLOAD).is_enabled()
        assert page.evaluate(FOCUSED) == "select-download"
        current.assert_clean()

    @pytest.mark.parametrize("change", ["clear", "swap"])
    def test_the_file_holds_the_talks_selected_at_the_press(
        self, current: Session, feed: str, change: str
    ) -> None:
        page = current.page
        held: list[Any] = []
        page.route(f"**/{FEED_PATH}", lambda route: held.append(route))
        select(current, "sample-pick", "sample-arc")
        with page.expect_request(f"**/{FEED_PATH}"):
            page.locator(DOWNLOAD).click()
        assert note(current) == catalog("en")["site.select.preparing"]
        # The selection changes while the file is prepared.
        if change == "clear":
            page.locator(CLEAR).click()
            assert checked(current) == []
        else:
            checkbox(current, "sample-arc").uncheck()
            checkbox(current, "sample-online").check()
            assert checked(current) == ["sample-online", "sample-pick"]
        with page.expect_download() as info:
            held[0].continue_()
        text = Path(info.value.path()).read_bytes().decode("utf-8")
        assert_file_of(text, feed, ["sample-pick", "sample-arc"])
        page.locator(f"{NOTE} .note-saved").wait_for()
        assert note(current) == counted("en", "site.select.saved", 2)
        assert len(held) == 1
        current.assert_clean()

    def test_the_second_click_of_a_double_click_saves_nothing_more(
        self, current: Session, feed: str
    ) -> None:
        page = current.page
        downloads: list[Any] = []
        page.on("download", lambda saved: downloads.append(saved))
        select(current, "sample-pick")
        page.locator(DOWNLOAD).scroll_into_view_if_needed()
        box = page.locator(DOWNLOAD).bounding_box()
        assert box is not None
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        with page.expect_download() as info:
            page.mouse.down()
            page.mouse.up()
        assert_file_of(Path(info.value.path()).read_bytes().decode("utf-8"), feed, ["sample-pick"])
        # The file was saved before the second click came, as when the feed arrives fast.
        # A request for the feed would now be held, and the button would stay busy.
        page.route(f"**/{FEED_PATH}", lambda route: None)
        page.mouse.down(click_count=2)
        page.mouse.up(click_count=2)
        assert page.locator(DOWNLOAD).get_attribute("aria-busy") == "false"
        assert note(current) == counted("en", "site.select.saved", 1)
        assert len(downloads) == 1
        page.unroute(f"**/{FEED_PATH}")
        with page.expect_download():  # a click of its own saves the file again
            page.mouse.down()
            page.mouse.up()
        assert len(downloads) == 2
        current.assert_clean()

    @pytest.mark.parametrize("button", [DOWNLOAD, DOCK_DOWNLOAD], ids=["bar", "dock"])
    def test_holding_enter_saves_one_file(self, current: Session, feed: str, button: str) -> None:
        page = current.page
        downloads: list[Any] = []
        page.on("download", lambda saved: downloads.append(saved))
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        page.evaluate(COUNT_PRESSES, button)
        select(current, "sample-pick")
        if button == DOCK_DOWNLOAD:
            dock_shown_at(current, MIDDLE_TALK)
        page.locator(button).focus()
        with page.expect_download() as info:
            page.keyboard.down("Enter")
        assert_file_of(Path(info.value.path()).read_bytes().decode("utf-8"), feed, ["sample-pick"])
        page.locator(f"{NOTE} .note-saved").wait_for()
        # The key is still held: the keyboard repeats it, as it does after a short delay.
        for _ in range(5):
            page.keyboard.down("Enter")
        page.keyboard.up("Enter")
        assert page.evaluate("window.__presses") == 1
        assert page.locator(button).get_attribute("aria-busy") == "false"
        assert note(current) == counted("en", "site.select.saved", 1)
        assert [url for url in requests if "/feeds/" in url] == [current.base_url + FEED_PATH]
        assert len(downloads) == 1
        with page.expect_download():  # a press of its own saves the file again
            page.keyboard.press("Enter")
        assert len(downloads) == 2
        current.assert_clean()

    def test_a_failed_request_saves_nothing_and_a_retry_succeeds(
        self, current: Session, feed: str
    ) -> None:
        page = current.page
        downloads: list[Any] = []
        page.on("download", lambda saved: downloads.append(saved))
        page.route(f"**/{FEED_PATH}", lambda route: route.fulfill(status=404, body=""))
        select(current, "sample-pick", "sample-arc")
        page.locator(DOWNLOAD).click()
        alert = page.locator(f"{NOTE} [role=alert]")
        alert.wait_for()
        assert alert.text_content() == catalog("en")["site.select.failed"]
        assert page.locator(DOWNLOAD).is_enabled()
        assert checked(current) == ["sample-pick", "sample-arc"]
        assert downloads == []
        drop_expected_errors(current, FAILED_REQUEST, FEED_ERROR)

        page.unroute(f"**/{FEED_PATH}")
        assert_file_of(download(current), feed, ["sample-pick", "sample-arc"])
        assert alert.count() == 0
        assert note(current) == counted("en", "site.select.saved", 2)
        current.assert_clean()

    def test_a_tick_or_a_filter_leaves_the_failure_note_alone(self, current: Session) -> None:
        # The note is drawn when a download ends: drawn again, its alert would be said again.
        page = current.page
        page.route(f"**/{FEED_PATH}", lambda route: route.fulfill(status=404, body=""))
        select(current, "sample-pick")
        page.locator(DOWNLOAD).click()
        page.locator(f"{NOTE} [role=alert]").wait_for()
        drop_expected_errors(current, FAILED_REQUEST, FEED_ERROR)
        page.evaluate(WATCH, [NOTE, "#events"])

        def assert_note_kept() -> None:
            changes = page.evaluate(CHANGES)
            assert changes[NOTE] == 0
            assert page.evaluate(SAME_ALERT) is True

        select(current, "sample-online")
        assert status(current) == "2 talks selected"
        assert_note_kept()
        page.locator('#field-chips button[data-value="cs"]').click()
        assert current.shown() == CS_TALKS
        assert_note_kept()
        type_search(current, "compilers", ["sample-defense"])
        assert_note_kept()
        page.locator(SELECT_SHOWN).click()
        assert checked(current) == ["sample-defense"]
        assert status(current) == status_text("en", 3, hidden=2)
        assert_note_kept()
        assert note(current) == catalog("en")["site.select.failed"]
        current.assert_clean()

    def test_a_stalled_request_is_given_up_and_a_retry_succeeds(
        self, session: Callable[..., Session], feed: str
    ) -> None:
        current = paused(session)
        page = current.page
        downloads: list[Any] = []
        page.on("download", lambda saved: downloads.append(saved))
        held: list[Any] = []
        page.route(f"**/{FEED_PATH}", lambda route: held.append(route))
        select(current, "sample-pick", "sample-arc")
        with page.expect_request(f"**/{FEED_PATH}"):
            page.locator(DOWNLOAD).click()
        page.clock.run_for(FETCH_TIMEOUT_MS - 1)
        assert page.locator(DOWNLOAD).get_attribute("aria-busy") == "true"
        assert note(current) == catalog("en")["site.select.preparing"]
        page.clock.run_for(1)
        alert = page.locator(f"{NOTE} [role=alert]")
        alert.wait_for()
        assert alert.text_content() == catalog("en")["site.select.failed"]
        assert page.locator(DOWNLOAD).get_attribute("aria-busy") == "false"
        assert page.locator(DOWNLOAD).is_enabled()
        assert checked(current) == ["sample-pick", "sample-arc"]
        assert downloads == []
        drop_expected_errors(current, rf"{re.escape(FEED_PATH)}: no answer within")

        page.unroute(f"**/{FEED_PATH}")
        assert_file_of(download(current), feed, ["sample-pick", "sample-arc"])
        assert alert.count() == 0
        assert len(held) == 1
        current.assert_clean()

    def test_the_feed_is_the_one_events_json_lists_as_all(
        self, session: Callable[..., Session], feed: str
    ) -> None:
        payload = sample("events.json")
        moved = "feeds/every-talk.ics"
        payload["feeds"] = [
            {**entry, "path": moved} if entry["id"] == "all" else entry
            for entry in payload["feeds"]
        ]
        current = session()
        serve_json(current, "events.json", payload)
        current.page.route(
            f"**/{moved}",
            lambda route: route.fulfill(status=200, content_type="text/calendar", body=feed),
        )
        current.open("index.html")
        page = current.page
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        select(current, "sample-pick", "sample-arc")
        assert_file_of(download(current), feed, ["sample-pick", "sample-arc"])
        assert [url for url in requests if "/feeds/" in url] == [current.base_url + moved]
        current.assert_clean()

    def test_without_a_feed_of_every_talk_nothing_is_fetched_or_saved(
        self, session: Callable[..., Session]
    ) -> None:
        payload = sample("events.json")
        payload["feeds"] = [entry for entry in payload["feeds"] if entry["id"] != "all"]
        current = session()
        serve_json(current, "events.json", payload)
        current.open("index.html")
        page = current.page
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        downloads: list[Any] = []
        page.on("download", lambda saved: downloads.append(saved))
        select(current, "sample-pick")
        page.locator(DOWNLOAD).click()
        alert = page.locator(f"{NOTE} [role=alert]")
        alert.wait_for()
        assert alert.text_content() == catalog("en")["site.select.failed"]
        assert [url for url in requests if "/feeds/" in url] == []
        assert downloads == []
        assert page.locator(DOWNLOAD).is_enabled()
        drop_expected_errors(current, r"events\.json lists no feed of every talk")
        current.assert_clean()

    def test_a_talk_missing_from_the_feed_is_left_out_and_counted(
        self, current: Session, feed: str
    ) -> None:
        page = current.page
        shorter = feed.replace(block_of(feed, "sample-pick"), "")
        assert validate_ics(shorter) == []
        page.route(
            f"**/{FEED_PATH}",
            lambda route: route.fulfill(status=200, content_type="text/calendar", body=shorter),
        )
        select(current, "sample-pick", "sample-arc", "sample-online")
        assert_file_of(download(current), feed, ["sample-arc", "sample-online"])
        english = catalog("en")["site.select.partial"].format(
            saved=counted("en", "site.select.saved", 2),
            missing=counted("en", "site.select.missing", 1),
        )
        assert english == (
            "A calendar file with 2 talks was passed to your browser to save. "
            "1 selected talk is not in the calendar feed and was left out."
        )
        assert note(current) == english
        assert page.locator(f"{NOTE} [role=alert]").count() == 0
        page.locator('.lang-switch button[lang="zh-CN"]').click()
        assert note(current) == catalog("zh-CN")["site.select.partial"].format(
            saved=counted("zh-CN", "site.select.saved", 2),
            missing=counted("zh-CN", "site.select.missing", 1),
        )
        current.assert_clean()

    @pytest.mark.parametrize(
        ("body", "error"),
        [
            ("not a calendar\r\n", r"not a content line"),
            (
                "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//EN\r\nEND:VCALENDAR\r\n",
                r"holds none of the 1 selected talks",
            ),
        ],
        ids=["unreadable", "without-the-talks"],
    )
    def test_a_feed_without_a_usable_talk_saves_nothing(
        self, current: Session, body: str, error: str
    ) -> None:
        page = current.page
        downloads: list[Any] = []
        page.on("download", lambda saved: downloads.append(saved))
        page.route(
            f"**/{FEED_PATH}",
            lambda route: route.fulfill(status=200, content_type="text/calendar", body=body),
        )
        select(current, "sample-pick")
        page.locator(DOWNLOAD).click()
        alert = page.locator(f"{NOTE} [role=alert]")
        alert.wait_for()
        assert alert.text_content() == catalog("en")["site.select.failed"]
        assert downloads == []
        drop_expected_errors(current, error)
        current.assert_clean()

    def test_the_injected_title_is_selected_and_saved_as_text(
        self, current: Session, feed: str
    ) -> None:
        page = current.page
        box = checkbox(current, "sample-xss")
        assert talk_name(current, "sample-xss").startswith(f"Select {XSS_TITLE} ")
        box.check()
        assert checked(current) == ["sample-xss"]
        assert_file_of(download(current), feed, ["sample-xss"])
        assert page.locator("#selection, #events").locator(INJECTED).count() == 0
        assert page.title() == catalog("en")["site.page.index_title"]
        current.assert_clean()


class TestDock:
    @pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=["phone", "desktop"])
    def test_the_dock_keeps_the_count_and_the_download_in_reach(
        self, session: Callable[..., Session], feed: str, viewport: dict[str, int]
    ) -> None:
        current = session(viewport=viewport).open("index.html")
        page = current.page
        region = page.get_by_role("region", name=catalog("en")["site.select.label"], exact=True)
        assert region.get_attribute("id") == "selection"  # a landmark to jump back to
        # Nothing selected: no dock, wherever the list is scrolled.
        page.evaluate(CENTER_TALK, "sample-arc")
        settle(current)
        assert page.evaluate(DOCK_PLACE)["bar"] is False
        assert page.locator(DOCK).is_hidden()
        tap_in_place(current, "sample-arc")  # far below the bar
        page.locator(DOCK).wait_for(state="visible")
        page.evaluate(CENTER_TALK, "sample-defense")
        tap_in_place(current, "sample-defense")
        dock_shown_at(current, MIDDLE_TALK)
        assert page.locator(DOCK_COUNT).text_content() == "2 talks selected"
        assert status(current) == "2 talks selected"
        # The status of the bar is the one live region that announces the count.
        for selector in (DOCK, DOCK_COUNT):
            assert page.locator(selector).get_attribute("role") is None
            assert page.locator(selector).get_attribute("aria-live") is None
        text = download(current, DOCK_DOWNLOAD)
        assert_file_of(text, feed, ["sample-arc", "sample-defense"])
        page.locator(f"{NOTE} .note-saved").wait_for()
        assert note(current) == counted("en", "site.select.saved", 2)
        assert page.locator(DOCK).is_visible()
        # The bar back on screen: the dock goes, and the two are never shown together.
        page.evaluate(BAR_ON_SCREEN)
        page.locator(DOCK).wait_for(state="hidden")
        assert page.evaluate(DOCK_PLACE)["bar"] is True
        dock_shown_at(current, MIDDLE_TALK)
        page.evaluate(CENTER_TALK, "sample-defense")
        tap_in_place(current, "sample-defense")
        assert page.locator(DOCK_COUNT).text_content() == "1 talk selected"
        page.evaluate(CENTER_TALK, "sample-arc")
        tap_in_place(current, "sample-arc")  # nothing selected: the dock goes
        page.locator(DOCK).wait_for(state="hidden")
        assert page.evaluate(DOCK_PLACE)["bar"] is False
        current.assert_clean()

    def test_the_dock_stays_hidden_while_the_bar_is_below_the_screen(
        self, session: Callable[..., Session]
    ) -> None:
        # At the top of the page on a phone the bar is still below the viewport, and the
        # dock would cover the filters the reader scrolled back up to.
        current = session(viewport=PHONE).open("index.html")
        page = current.page
        page.evaluate(CENTER_TALK, "sample-arc")
        tap_in_place(current, "sample-arc")
        page.locator(DOCK).wait_for(state="visible")
        page.evaluate("window.scrollTo(0, 0)")
        page.locator(DOCK).wait_for(state="hidden")
        assert page.evaluate(BAR_BELOW) is True
        assert in_viewport(current, "#host")
        assert page.evaluate(REACHED, "#host") is True
        # From there past the whole bar in one step: the dock is back.
        dock_shown_at(current, MIDDLE_TALK)
        assert page.locator(DOCK_COUNT).text_content() == "1 talk selected"
        current.assert_clean()

    @pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=["phone", "desktop"])
    def test_the_keyboard_focus_stays_above_the_dock(
        self, session: Callable[..., Session], viewport: dict[str, int]
    ) -> None:
        current = session(viewport=viewport).open("index.html")
        page = current.page
        select(current, "sample-xss")
        page.locator(DOWNLOAD).focus()
        # Tab through the list to the Download button of the dock, the stop after it.
        above_the_dock = []
        for _ in range(200):
            page.keyboard.press("Tab")
            settle(current)
            focused = page.evaluate(FOCUSED)
            if focused == "selection-dock-download":
                break
            clear = page.evaluate(FOCUS_CLEAR)
            assert clear is not False, focused
            if clear and focused in UPCOMING:
                above_the_dock.append(focused)
        assert page.evaluate(FOCUSED) == "selection-dock-download"
        # The dock was shown below every checkbox from the middle of the list on.
        middle = UPCOMING.index(MIDDLE_TALK)
        assert above_the_dock[middle - len(UPCOMING) :] == UPCOMING[middle:]
        # The bar back on screen hides the dock: its focus goes to the Download of the bar.
        page.evaluate(BAR_ON_SCREEN)
        page.locator(DOCK).wait_for(state="hidden")
        assert page.evaluate(FOCUSED) == "select-download"
        current.assert_clean()

    @pytest.mark.parametrize("device", ["mouse", "keyboard"])
    @pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=["phone", "desktop"])
    def test_a_dock_that_hides_hands_its_focus_to_the_bar_in_place(
        self, session: Callable[..., Session], viewport: dict[str, int], device: str
    ) -> None:
        # The Download of the bar is still above the screen when the last pixels of the
        # bar come in: focused, it would be scrolled to the middle of the screen.
        current = session(viewport=viewport).open("index.html")
        page = current.page
        select(current, "sample-arc")
        dock_shown_at(current, MIDDLE_TALK)
        if device == "mouse":
            box = page.locator(DOCK_DOWNLOAD).bounding_box()
            assert box is not None
            with page.expect_download():
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.locator(f"{NOTE} .note-saved").wait_for()
        else:
            page.evaluate(FOCUS_BEFORE, DOCK_DOWNLOAD)
            page.keyboard.press("Tab")
        assert page.evaluate(FOCUSED) == "selection-dock-download"
        ring = device == "keyboard"
        assert page.locator(DOCK_DOWNLOAD).evaluate("(b) => b.matches(':focus-visible')") is ring
        settle(current)
        edge = page.evaluate(BAR_EDGE_ON_SCREEN, 3)
        assert edge["top"] < 0
        page.locator(DOCK).wait_for(state="hidden")
        settle(current)
        assert page.evaluate(FOCUSED) == "select-download"
        if ring:
            # The focus ring is scrolled onto the screen, the least way.
            assert page.evaluate(SCROLLED) == pytest.approx(edge["scrolled"] + edge["top"], abs=1)
            assert in_viewport(current, DOWNLOAD)
        else:
            assert page.evaluate(SCROLLED) == edge["scrolled"]  # a click never scrolls
        current.assert_clean()

    def test_a_dock_that_appears_over_the_focus_moves_the_focus_above_it(
        self, session: Callable[..., Session]
    ) -> None:
        current = session().open("index.html")
        page = current.page
        select(current, "sample-xss")
        # A viewport that holds the bar (one pixel of it) and the checkbox of sample-pick
        # in its last 20 pixels, where the dock appears.
        bar = page.evaluate("document.getElementById('selection').getBoundingClientRect().bottom")
        box = page.evaluate(PLACE, "sample-pick")
        top = math.floor(page.evaluate("window.scrollY") + bar) - 1
        height = round(box["scrolled"] + box["y"] + 10 - top + 20)
        assert 300 < height < DESKTOP["height"] * 2
        page.set_viewport_size({"width": DESKTOP["width"], "height": height})
        page.evaluate(f"window.scrollTo(0, {top})")
        settle(current)
        page.evaluate(FOCUS_BEFORE, box_of("sample-pick"))
        page.keyboard.press("Tab")  # the checkbox is on screen: Tab does not scroll
        settle(current)
        assert page.evaluate(FOCUSED) == "sample-pick"
        assert page.evaluate(SCROLLED) == top
        assert page.locator(DOCK).is_hidden()
        page.evaluate("window.scrollBy(0, 2)")  # the bar leaves the screen
        page.locator(DOCK).wait_for(state="visible")
        settle(current)
        assert page.evaluate(FOCUSED) == "sample-pick"
        assert page.evaluate(FOCUS_CLEAR) is True
        current.assert_clean()


class TestStates:
    def test_the_bar_waits_for_the_talks(self, session: Callable[..., Session]) -> None:
        state = loading(session, "index.html")
        page = state.page
        assert all(page.locator(button).is_disabled() for button in (*BUTTONS, DOCK_DOWNLOAD))
        assert page.locator(DOCK).is_hidden()
        assert status(state.current) == catalog("en")["site.select.none"]
        for button in (*BUTTONS, DOCK_DOWNLOAD):
            page.locator(button).dispatch_event("click")
        assert note(state.current) == ""
        state.arrive()
        assert page.locator(SELECT_SHOWN).is_enabled()
        assert page.locator(DOWNLOAD).is_disabled()
        page.locator(SELECT_SHOWN).click()
        assert checked(state.current) == UPCOMING
        state.current.assert_clean()

    def test_the_bar_stays_disabled_when_the_talks_fail_to_load(
        self, session: Callable[..., Session]
    ) -> None:
        current = session()
        serve_json(current, "events.json", {"detail": "missing"}, status=404)
        current.open("index.html")
        page = current.page
        assert page.locator("#events .notice[role=alert]").count() == 1
        assert page.locator("input.select-talk").count() == 0
        assert all(page.locator(button).is_disabled() for button in (*BUTTONS, DOCK_DOWNLOAD))
        assert page.locator(DOCK).is_hidden()
        assert status(current) == catalog("en")["site.select.none"]
        drop_expected_errors(current, FAILED_REQUEST, r"events\.json: HTTP status 404")
        current.assert_clean()


class TestPhone:
    @pytest.mark.parametrize("wider", [False, True], ids=["fonts", "wider-text"])
    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_the_checkboxes_and_the_bar_fit_a_phone(
        self, session: Callable[..., Session], site_dir: Path, scheme: str, wider: bool
    ) -> None:
        current = session(viewport=PHONE, color_scheme=scheme)
        if wider:
            widen_text(current, site_dir)
        current.open("index.html?lang=zh" if wider else "index.html")
        page = current.page
        boxes = page.evaluate(BOXES)
        assert len(boxes) == len(UPCOMING)
        width = PHONE["width"]
        assert all(box["width"] > 0 and box["height"] > 0 for box in boxes)
        assert all(box["left"] >= 0 and box["right"] <= width for box in boxes)
        lang = "zh-CN" if wider else "en"
        checkbox(current, "sample-cancelled", lang).check()
        checkbox(current, "sample-xss", lang).check()
        page.locator('#field-chips button[data-value="cs"]').click()
        assert status(current) == status_text(lang, 2, hidden=1)
        download(current)
        assert note(current) == counted(lang, "site.select.saved", 2)
        assert all(page.locator(button).is_visible() for button in BUTTONS)
        result = page.evaluate(OVERFLOW)
        assert result["scroll"] <= result["width"]
        assert result["wide"] == []
        # The dock, shown below the bar scrolled away, fits as well and hides the list
        # behind it with the background of the bar.
        page.locator("#clear").click()
        dock_shown_at(current, MIDDLE_TALK)
        assert page.locator(DOCK_COUNT).text_content() == counted(lang, "site.select.count", 2)
        box = page.locator(DOCK).bounding_box()
        assert box is not None
        assert box["x"] >= 0
        assert box["x"] + box["width"] <= width
        backgrounds = page.evaluate(
            "[...document.querySelectorAll('#selection, #selection-dock')]"
            ".map((element) => getComputedStyle(element).backgroundColor)"
        )
        assert backgrounds[0] == backgrounds[1] != "rgba(0, 0, 0, 0)"
        result = page.evaluate(OVERFLOW)
        assert result["scroll"] <= result["width"]
        assert result["wide"] == []
        current.assert_clean()
