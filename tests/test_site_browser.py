"""Browser smoke tests of the web site (Playwright with Chromium).

The site is assembled the way it is deployed (``ProjectPaths``: the files of
site/ at the root, locales/<lang>.json, events.json and status.json) in a
temporary directory, from the sample payloads in tests/fixtures/_site/, and
served on 127.0.0.1. The browser clock is fixed to noon in Stanford on the
day the sample was generated, so "upcoming" and the time ranges are stable.

Every test opens its pages through ``Session``, which records console
errors, uncaught exceptions, Content-Security-Policy violations, dialogs
(``alert`` from an injected script would open one) and every request that
leaves the local server (such requests are aborted). ``assert_clean``
requires all of them to be empty.

These tests are marked ``browser`` and are not part of a normal run:
``uv run pytest -m browser`` (Chromium: ``uv run playwright install chromium``).
"""

from __future__ import annotations

import functools
import http.server
import json
import re
import shutil
import socketserver
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from seminars.i18n import flatten
from seminars.models import FEED_IDS
from site_sample import XSS_ABSTRACT, XSS_TITLE

pytestmark = pytest.mark.browser

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = REPO_ROOT / "tests" / "fixtures" / "_site"
NOW = "2026-11-10T20:00:00Z"  # 12:00 in Stanford, the day the sample payloads were generated
TIMEZONE = "America/Los_Angeles"
DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 360, "height": 740}

# Upcoming sample events at NOW, in list order (the past one is left out).
UPCOMING = [
    "sample-xss",
    "sample-cancelled",
    "sample-online",
    "sample-pick",
    "sample-tba",
    "sample-hybrid-med",
    "sample-all-day",
    "sample-tentative-physics",
    "sample-biohub",
    "sample-arc",
    "sample-defense",
]

READY = """() => {
  const alert = document.querySelector(".notice[role=alert]");
  const index = document.getElementById("count");
  const status = document.getElementById("summary");
  return Boolean(alert || (index && index.textContent) || (status && status.children.length));
}"""

# Interface text of the page: text nodes and labelled attributes, without the language
# names in the switch (each in its own language) and the generated feed addresses.
INTERFACE_TEXTS = """() => {
  const skipped = (element) => element.closest(".lang-switch, .feed-address");
  const texts = [document.title];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const value = node.nodeValue.trim();
    if (value && !skipped(node.parentElement)) {
      texts.push(value);
    }
  }
  for (const element of document.querySelectorAll("[aria-label], [placeholder], [title]")) {
    for (const name of ["aria-label", "placeholder", "title"]) {
      const value = element.getAttribute(name);
      if (value && !skipped(element)) {
        texts.push(value.trim());
      }
    }
  }
  return texts;
}"""

RECORD_VIOLATIONS = """
window.__violations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  window.__violations.push(`${event.violatedDirective} ${event.blockedURI}`);
});
"""

CJK = re.compile(r"[\u3400-\u9fff]")
LATIN = re.compile(r"[A-Za-z]")
KEY_LIKE = re.compile(r"\b(?:site|fields|feeds)\.[a-z0-9_-]+(?:\.[a-z0-9_-]+)*")
WEB_PROTOCOLS = {"http", "https"}


def catalog(lang: str) -> dict[str, str]:
    return flatten(json.loads((REPO_ROOT / "locales" / f"{lang}.json").read_text("utf-8")))


def sample(name: str) -> dict[str, Any]:
    return json.loads((SAMPLE_DIR / name).read_text(encoding="utf-8"))


def strings_in(value: Any) -> set[str]:
    """Every string in a JSON value, as given and trimmed."""
    if isinstance(value, str):
        return {value, value.strip()}
    if isinstance(value, dict):
        return set().union(*(strings_in(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(strings_in(item) for item in value))
    return set()


PAYLOAD_TEXT = strings_in(sample("events.json")) | strings_in(sample("status.json"))


# --- server and browser ---------------------------------------------------------------------


class LocalServer(http.server.ThreadingHTTPServer):
    """A static file server on 127.0.0.1 that never asks for its own host name."""

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        """Requests are not logged; the test output stays readable."""


@pytest.fixture(scope="module")
def site_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("public")
    shutil.copytree(REPO_ROOT / "site", root, dirs_exist_ok=True)
    (root / "locales").mkdir()
    for lang in ("en", "zh-CN"):
        shutil.copy(REPO_ROOT / "locales" / f"{lang}.json", root / "locales" / f"{lang}.json")
    for name in ("events.json", "status.json"):
        shutil.copy(SAMPLE_DIR / name, root / name)
    return root


@pytest.fixture(scope="module")
def base_url(site_dir: Path) -> Iterator[str]:
    handler = functools.partial(QuietHandler, directory=str(site_dir))
    server = LocalServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture(scope="module")
def browser(playwright_sync_api: Any) -> Iterator[Any]:
    with playwright_sync_api.sync_playwright() as playwright:
        chromium = playwright.chromium.launch()
        try:
            yield chromium
        finally:
            chromium.close()


@dataclass
class Session:
    """One browser context and page, with everything that went wrong in it."""

    context: Any
    page: Any
    base_url: str
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)
    foreign_requests: list[str] = field(default_factory=list)

    def open(self, path: str) -> Session:
        self.page.goto(self.base_url + path)
        self.page.wait_for_function(READY)
        return self

    def violations(self) -> list[str]:
        return self.page.evaluate("window.__violations || []")

    def assert_clean(self) -> None:
        assert self.console_errors == []
        assert self.page_errors == []
        assert self.dialogs == []
        assert self.foreign_requests == []
        assert self.violations() == []

    def query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.page.url).query)

    def shown(self) -> list[str]:
        return self.page.eval_on_selector_all("#events article", "es => es.map(e => e.dataset.uid)")

    def texts(self) -> list[str]:
        return self.page.evaluate(INTERFACE_TEXTS)


@pytest.fixture
def session(browser: Any, base_url: str) -> Iterator[Callable[..., Session]]:
    """Factory of sessions; every context is closed after the test."""
    contexts: list[Any] = []

    def build(
        viewport: dict[str, int] = DESKTOP,
        color_scheme: str = "light",
        permissions: tuple[str, ...] = (),
        now: str = NOW,
    ) -> Session:
        context = browser.new_context(
            viewport=viewport,
            color_scheme=color_scheme,
            locale="en-US",
            timezone_id=TIMEZONE,
            permissions=list(permissions),
        )
        contexts.append(context)
        page = context.new_page()
        page.clock.set_fixed_time(now)
        page.add_init_script(RECORD_VIOLATIONS)
        result = Session(context=context, page=page, base_url=base_url)

        def route(request_route: Any) -> None:
            if request_route.request.url.startswith(base_url):
                request_route.continue_()
            else:
                result.foreign_requests.append(request_route.request.url)
                request_route.abort()

        context.route("**/*", route)
        page.on(
            "console",
            lambda message: (
                result.console_errors.append(message.text) if message.type == "error" else None
            ),
        )
        page.on("pageerror", lambda error: result.page_errors.append(str(error)))

        def dismiss(dialog: Any) -> None:
            result.dialogs.append(dialog.message)
            dialog.dismiss()

        page.on("dialog", dismiss)
        return result

    yield build
    for context in contexts:
        context.close()


def serve_json(session: Session, name: str, payload: Any, status: int = 200) -> None:
    """Answer requests for ``name`` with ``payload`` instead of the sample file."""

    def fulfill(request_route: Any) -> None:
        request_route.fulfill(
            status=status, content_type="application/json", body=json.dumps(payload)
        )

    session.page.route(f"**/{name}", fulfill)


# --- untrusted text -------------------------------------------------------------------------


class TestUntrustedText:
    def test_injected_markup_in_events_is_shown_as_text(
        self, session: Callable[..., Session]
    ) -> None:
        current = session().open("index.html")
        page = current.page
        article = page.locator('article[data-uid="sample-xss"]')
        assert article.locator(".event-title").text_content() == XSS_TITLE
        speakers = article.locator(".speakers").text_content() or ""
        assert "<i>Mallory</i> O'Brien" in speakers
        assert '"><b>Evil Corp</b>' in speakers
        facts = article.locator(".facts").text_content() or ""
        for value in (
            "Security <Lunch>",
            "Stanford Computer Science & <Friends>",
            "Gates <u>104</u>",
        ):
            assert value in facts
        article.locator(".abstract summary").click()
        assert article.locator(".abstract p").text_content() == XSS_ABSTRACT
        injected = "script, img, svg, iframe, object, embed, b, i, u, [onerror], [onload]"
        assert page.locator("#events").locator(injected).count() == 0
        assert page.evaluate("[...document.scripts].map((s) => s.src)") == [
            current.base_url + "app.js"
        ]
        assert page.title() == catalog("en")["site.page.index_title"]
        current.assert_clean()

    def test_injected_markup_in_the_status_is_shown_as_text(
        self, session: Callable[..., Session]
    ) -> None:
        current = session().open("status.html")
        card = current.page.locator('article[data-source="custom-nlp"]')
        name = card.locator(".source-name").text_content()
        assert name == "Stanford NLP Seminar <script>alert('xss-status')</script>"
        error = card.locator("code").text_content()
        assert error == "HTTP 503: https://nlp.stanford.edu/seminar <img src=x onerror=alert(1)>"
        assert current.page.locator("#sources").locator("script, img, [onerror]").count() == 0
        assert current.page.title() == catalog("en")["site.page.status_title"]
        current.assert_clean()

    def test_only_web_addresses_become_links(self, session: Callable[..., Session]) -> None:
        payload = sample("events.json")
        xss = next(event for event in payload["events"] if event["uid"] == "sample-xss")
        xss["url"] = "javascript:alert(document.domain)"
        xss["stream_url"] = "data:text/html,<script>alert(1)</script>"
        xss["speakers"][0]["url"] = "vbscript:msgbox(1)"
        online = next(event for event in payload["events"] if event["uid"] == "sample-online")
        online["url"] = "https://user:secret@example.org/talk"
        # Schemes with a host name are refused as well: only http(s) becomes a link.
        hybrid = next(event for event in payload["events"] if event["uid"] == "sample-hybrid-med")
        hybrid["url"] = "file://example.org/etc/passwd"
        hybrid["stream_url"] = "ftp://example.org/stream"
        hybrid["speakers"][0]["url"] = "ftp://example.org/x"
        hybrid["speakers"][1]["url"] = "x-app://example.org/talk"
        current = session()
        serve_json(current, "events.json", payload)
        current.open("index.html")
        page = current.page
        assert page.locator('article[data-uid="sample-xss"] a').count() == 0
        assert page.locator('article[data-uid="sample-hybrid-med"] a').count() == 0
        hybrid_names = page.locator('article[data-uid="sample-hybrid-med"] .speaker-name')
        assert hybrid_names.all_text_contents() == ["Maria Chen", "Omar Haddad"]
        assert page.locator('article[data-uid="sample-online"] .event-title a').count() == 0
        assert page.locator('article[data-uid="sample-online"] .stream a').count() == 1
        links = page.eval_on_selector_all(
            "a[href]", "es => es.map((e) => [e.href, e.getAttribute('rel') || ''])"
        )
        assert links
        for href, rel in links:
            parts = urlsplit(href)
            assert parts.scheme in WEB_PROTOCOLS | {"webcal"}, href
            if parts.scheme in WEB_PROTOCOLS and not href.startswith(current.base_url):
                assert set(rel.split()) >= {"noopener", "noreferrer"}, href
        current.assert_clean()

    def test_records_that_cannot_be_shown_are_left_out(
        self, session: Callable[..., Session]
    ) -> None:
        payload = sample("events.json")
        arc = next(event for event in payload["events"] if event["uid"] == "sample-arc")
        days = {**arc, "uid": "days", "all_day": True, "start": "2026-11-11T00:00:00-08:00"}
        days["end"] = "2026-11-14T00:00:00-08:00"
        night = {**arc, "uid": "night", "start": "2026-11-11T22:00:00-08:00"}
        night["end"] = "2026-11-12T01:00:00-08:00"
        odd = {
            **arc,
            "uid": "odd",
            "start": "2026-11-11T09:00:00-08:00",
            "end": "2026-11-11T08:00:00-08:00",
            "fields": ["ai-ml", "astrology"],
            "institution": "atlantis",
            "event_type": "party",
            "status": "rumoured",
            "speakers": [None, {"name": ""}, {"name": "Real Person", "affiliation": 7}],
            "picks_reasons": [1, "kept"],
        }
        payload["events"] = [
            None,
            5,
            "text",
            {"uid": "untitled", "start": arc["start"]},
            {**arc, "uid": "undated", "start": "yesterday"},
            night,
            odd,
            days,
        ]
        payload["feeds"] = [
            {"id": "math", "path": "https://example.org/math.ics"},
            {"id": "unknown", "path": "feeds/unknown.ics"},
            None,
            {"id": "cs", "path": "feeds/cs.ics"},
        ]
        current = session()
        serve_json(current, "events.json", payload)
        current.open("index.html")
        page = current.page
        assert current.shown() == ["days", "odd", "night"]
        whole = page.locator('article[data-uid="days"]')
        assert whole.locator(".event-when").text_content() == "All day"
        assert whole.locator(".event-until").text_content() == "Until Friday, November 13"
        overnight = page.locator('article[data-uid="night"] .event-when').text_content()
        assert overnight == "10:00 PM - Nov 12, 2026, 1:00 AM PST"
        strange = page.locator('article[data-uid="odd"]')
        assert strange.locator(".event-when").text_content() == "9:00 AM"
        assert strange.locator(".badges li").all_text_contents() == ["Pick", "AI/ML"]
        assert strange.locator(".speakers li").all_text_contents() == ["Real Person"]
        assert strange.locator(".reasons li").all_text_contents() == ["kept"]
        feeds = page.eval_on_selector_all("#feeds li", "es => es.map((e) => e.dataset.feed)")
        assert feeds == ["cs"]
        current.assert_clean()

    def test_the_policy_blocks_inline_script_and_remote_loads(
        self, session: Callable[..., Session]
    ) -> None:
        current = session().open("index.html")
        current.page.evaluate(
            """() => {
              const script = document.createElement("script");
              script.textContent = "window.__ran = true;";
              document.body.appendChild(script);
              const image = new Image();
              image.src = "https://example.org/pixel.png";
              document.body.appendChild(image);
            }"""
        )
        current.page.wait_for_function("() => window.__violations.length >= 2")
        assert current.page.evaluate("window.__ran === true") is False
        directives = {violation.split()[0] for violation in current.violations()}
        assert directives == {"script-src-elem", "img-src"}
        assert current.foreign_requests == []
        assert current.console_errors
        assert all("Content Security Policy" in error for error in current.console_errors)


# --- languages ------------------------------------------------------------------------------


def assert_page_in(current: Session, lang: str) -> None:
    """Everything on the page is in ``lang``; no message is missing."""
    page = current.page
    messages = catalog(lang)
    assert page.evaluate("document.documentElement.lang") == lang
    for key, shown in page.eval_on_selector_all(
        "[data-i18n]", "es => es.map((e) => [e.dataset.i18n, e.textContent])"
    ):
        assert shown == messages[key], key
    texts = current.texts()
    assert [text for text in texts if KEY_LIKE.search(text)] == []
    if lang == "en":
        foreign = [text for text in texts if CJK.search(text) and text not in PAYLOAD_TEXT]
    else:
        # Names such as "SLAC" are the same in both languages; the Chinese file has them.
        ascii_messages = {message for message in messages.values() if message.isascii()}
        foreign = [
            text
            for text in texts
            if LATIN.search(text)
            and not CJK.search(text)
            and text not in PAYLOAD_TEXT
            and text not in ascii_messages
        ]
    assert foreign == []


class TestLanguages:
    def test_switching_to_chinese_and_back(self, session: Callable[..., Session]) -> None:
        current = session().open("index.html?field=cs")
        page = current.page
        assert_page_in(current, "en")
        english_day = page.locator('section[data-day="2026-11-12"] .day-heading').text_content()
        assert english_day == "Thursday, November 12"

        page.locator('.lang-switch button[lang="zh-CN"]').click()
        assert current.query() == {"field": ["cs"], "lang": ["zh"]}
        assert_page_in(current, "zh-CN")
        assert page.title() == catalog("zh-CN")["site.page.index_title"]
        chinese_day = page.locator('section[data-day="2026-11-12"] .day-heading').text_content()
        assert chinese_day is not None
        assert "11\u670812\u65e5" in chinese_day
        assert page.locator('.lang-switch button[aria-pressed="true"]').get_attribute("lang") == (
            "zh-CN"
        )

        page.locator('.page-nav a[href*="status.html"]').click()
        page.wait_for_function(READY)
        assert current.query() == {"lang": ["zh"]}
        assert_page_in(current, "zh-CN")

        current.open("index.html")
        assert_page_in(current, "zh-CN")  # remembered without ?lang=
        page.locator('.lang-switch button[lang="en"]').click()
        assert current.query() == {}
        assert_page_in(current, "en")
        current.assert_clean()

    @pytest.mark.parametrize("path", ["index.html?lang=zh", "status.html?lang=zh"])
    def test_chinese_from_the_address(self, session: Callable[..., Session], path: str) -> None:
        current = session().open(path)
        assert_page_in(current, "zh-CN")
        current.assert_clean()

    def test_unknown_language_and_blocked_storage_fall_back_to_english(
        self, session: Callable[..., Session]
    ) -> None:
        current = session()
        current.page.add_init_script(
            """Object.defineProperty(window, "localStorage", {
              get() { throw new DOMException("blocked", "SecurityError"); },
            });"""
        )
        current.open("index.html?lang=fr")
        assert_page_in(current, "en")
        current.page.locator('.lang-switch button[lang="zh-CN"]').click()
        assert_page_in(current, "zh-CN")
        current.assert_clean()


# --- filters and the address bar ------------------------------------------------------------


class TestFilters:
    def test_all_upcoming_talks_are_listed_by_default(
        self, session: Callable[..., Session]
    ) -> None:
        current = session().open("index.html")
        assert current.shown() == UPCOMING
        assert current.page.locator("#count").text_content() == "11 upcoming talks"
        assert current.page.locator("#clear").is_disabled()
        current.assert_clean()

    def test_filter_state_round_trips_through_the_address(
        self, session: Callable[..., Session]
    ) -> None:
        first = session().open("index.html")
        page = first.page
        page.locator('#field-chips button[data-value="ai-ml"]').click()
        assert first.shown() == ["sample-xss", "sample-online", "sample-pick", "sample-arc"]
        page.locator("#picks").check()
        assert first.shown() == ["sample-pick", "sample-arc"]
        page.locator("#search").fill("protein")
        page.locator("#search").press("Enter")  # applies the search without the typing pause
        assert first.shown() == ["sample-pick"]
        assert first.query() == {"q": ["protein"], "field": ["ai-ml"], "picks": ["1"]}
        address = page.url

        second = session()
        second.page.goto(address)
        second.page.wait_for_function(READY)
        restored = second.page
        assert second.shown() == ["sample-pick"]
        assert restored.locator("#search").input_value() == "protein"
        assert restored.locator("#picks").is_checked()
        pressed = restored.eval_on_selector_all(
            "#field-chips button[aria-pressed=true]", "es => es.map((e) => e.dataset.value)"
        )
        assert pressed == ["ai-ml"]

        restored.locator("#clear").click()
        assert second.shown() == UPCOMING
        assert second.query() == {}
        assert restored.locator("#search").input_value() == ""
        first.assert_clean()
        second.assert_clean()

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("inst=slac&range=two_weeks", ["sample-all-day", "sample-tentative-physics"]),
            ("range=week", ["sample-xss", "sample-cancelled", "sample-online"]),
            ("host=CZ+Biohub", ["sample-biohub"]),
            (
                "field=math&field=physics",
                ["sample-cancelled", "sample-all-day", "sample-tentative-physics"],
            ),
            ("q=zoe+muller", ["sample-tba"]),
            ("q=EVIL+corp", ["sample-xss"]),
            ("field=nope&inst=mars&range=forever&host=Nobody&picks=yes", UPCOMING),
        ],
    )
    def test_filters_are_read_from_the_address(
        self, session: Callable[..., Session], query: str, expected: list[str]
    ) -> None:
        current = session().open(f"index.html?{query}")
        assert current.shown() == expected
        current.assert_clean()

    def test_selects_show_the_filters_of_the_address(self, session: Callable[..., Session]) -> None:
        current = session().open("index.html?host=SLAC&range=two_weeks&inst=slac")
        page = current.page
        assert page.locator("#host").input_value() == "SLAC"
        assert page.locator("#range").input_value() == "two_weeks"
        assert current.shown() == ["sample-all-day", "sample-tentative-physics"]
        page.locator("#range").select_option("week")
        assert current.shown() == []
        assert page.locator("#events .notice").text_content() == catalog("en")["site.results.empty"]
        assert current.query()["range"] == ["week"]
        current.assert_clean()

    def test_institution_chips_and_the_organizer_list_change_the_address(
        self, session: Callable[..., Session]
    ) -> None:
        current = session().open("index.html")
        page = current.page
        chips = page.locator("#institution-chips")
        chips.locator('button[data-value="slac"]').click()
        assert current.shown() == ["sample-all-day", "sample-tentative-physics"]
        chips.locator('button[data-value="arc"]').click()
        assert current.shown() == ["sample-all-day", "sample-tentative-physics", "sample-arc"]
        assert current.query() == {"inst": ["slac", "arc"]}
        chips.locator('button[data-value="slac"]').click()
        assert current.shown() == ["sample-arc"]
        assert chips.locator('button[data-value="slac"]').get_attribute("aria-pressed") == "false"
        assert chips.locator('button[data-value="arc"]').get_attribute("aria-pressed") == "true"
        page.locator("#host").select_option("Arc Institute")
        assert current.shown() == ["sample-arc"]
        assert current.query() == {"inst": ["arc"], "host": ["Arc Institute"]}
        page.locator("#host").select_option("SLAC")
        assert current.shown() == []
        page.locator("#clear").click()
        assert current.shown() == UPCOMING
        assert page.locator("#host").input_value() == ""
        assert chips.locator("button[aria-pressed=true]").count() == 0
        current.assert_clean()

    def test_today_without_talks_shows_the_empty_notice(
        self, session: Callable[..., Session]
    ) -> None:
        current = session().open("index.html?range=today")
        assert current.shown() == []
        assert current.page.locator("#count").text_content() == "0 upcoming talks"
        current.assert_clean()

    def test_a_talk_in_progress_is_listed_today_and_an_ended_one_is_not(
        self, session: Callable[..., Session]
    ) -> None:
        during = session(now="2026-11-12T20:30:00Z").open("index.html?range=today")
        assert during.shown() == ["sample-xss"]
        assert during.page.locator("#events section").get_attribute("data-day") == "2026-11-12"
        after = session(now="2026-11-12T21:00:00Z").open("index.html?range=today")
        assert after.shown() == []
        during.assert_clean()
        after.assert_clean()


# --- subscription and notices ---------------------------------------------------------------


class TestPanels:
    def test_every_feed_has_both_links_and_a_working_copy_button(
        self, session: Callable[..., Session]
    ) -> None:
        current = session(permissions=("clipboard-read", "clipboard-write")).open("index.html")
        page = current.page
        feeds = page.eval_on_selector_all("#feeds li", "es => es.map((e) => e.dataset.feed)")
        assert feeds == list(FEED_IDS)
        host = urlsplit(current.base_url).netloc
        item = page.locator('#feeds li[data-feed="math"]')
        hrefs = item.locator("a").evaluate_all("es => es.map((e) => e.href)")
        assert hrefs == [f"webcal://{host}/feeds/math.ics", f"{current.base_url}feeds/math.ics"]
        assert item.locator(".feed-name").text_content() == "Seminars - Math"
        item.locator("button.copy").first.click()
        item.locator(".copy-note", has_text=re.compile(r"\S")).wait_for()
        assert page.evaluate("navigator.clipboard.readText()") == f"webcal://{host}/feeds/math.ics"
        assert (
            item.locator(".copy-note").first.text_content()
            == catalog("en")["site.subscribe.copied"]
        )
        steps = page.locator("#apple-steps li").all_text_contents()
        assert len(steps) == 5
        current.assert_clean()

    def test_without_a_clipboard_the_copy_button_says_so(
        self, session: Callable[..., Session]
    ) -> None:
        current = session()
        current.page.add_init_script(
            'Object.defineProperty(Navigator.prototype, "clipboard", '
            "{ get: () => undefined, configurable: true });"
        )
        current.open("index.html")
        item = current.page.locator('#feeds li[data-feed="all"]')
        item.locator("button.copy").last.click()
        note = item.locator(".copy-note", has_text=re.compile(r"\S"))
        note.wait_for()
        assert note.text_content() == catalog("en")["site.subscribe.copy_failed"]
        current.assert_clean()

    @pytest.mark.parametrize(
        ("path", "name"), [("index.html", "events.json"), ("status.html", "status.json")]
    )
    def test_a_missing_payload_shows_a_notice(
        self, session: Callable[..., Session], path: str, name: str
    ) -> None:
        current = session()
        serve_json(current, name, {"detail": "missing"}, status=404)
        current.open(path)
        notice = current.page.locator(".notice[role=alert]")
        key = "site.results.load_error" if name == "events.json" else "site.status.load_error"
        assert notice.text_content() == catalog("en")[key]
        assert current.page_errors == []
        assert current.dialogs == []
        assert current.foreign_requests == []


# --- layout ---------------------------------------------------------------------------------


OVERFLOW = """() => {
  for (const details of document.querySelectorAll("details")) {
    details.open = true;
  }
  const width = document.documentElement.clientWidth;
  const wide = [...document.querySelectorAll("body *")]
    .filter((element) => element.getBoundingClientRect().right > width + 0.5)
    .map((element) => `${element.tagName}.${element.className}`);
  return { scroll: document.documentElement.scrollWidth, width, wide };
}"""

COLORS = """() => {
  const style = getComputedStyle(document.body);
  return [style.backgroundColor, style.color];
}"""

# The left edges of the labels (dt) and values (dd) of every fact list that is shown.
FACT_COLUMNS = """() => [...document.querySelectorAll(".facts")].map((list) => {
  const lefts = (tag) => [...list.children]
    .filter((element) => element.tagName === tag && element.getClientRects().length > 0)
    .map((element) => Math.round(element.getBoundingClientRect().left));
  return { labels: lefts("DT"), values: lefts("DD") };
})"""

PAGES = ["index.html", "index.html?lang=zh", "status.html", "status.html?lang=zh"]

# Text set wider than the fonts of this machine set it: a machine with other fonts (the
# runner of the CI) lays the pages out with wider text, so the layout must leave room.
WIDER_TEXT = "\n* { letter-spacing: 0.06em !important; }\n"


def luminance(color: str) -> float:
    red, green, blue = (int(part) for part in re.findall(r"\d+", color)[:3])
    return (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255


def widen_text(session: Session, site_dir: Path) -> None:
    """Serve the stylesheet of the site with ``WIDER_TEXT`` appended, from its own address."""
    css = (site_dir / "style.css").read_text(encoding="utf-8") + WIDER_TEXT

    def fulfill(request_route: Any) -> None:
        request_route.fulfill(status=200, content_type="text/css", body=css)

    session.page.route("**/style.css", fulfill)


class TestLayout:
    @pytest.mark.parametrize("scheme", ["light", "dark"])
    @pytest.mark.parametrize("path", PAGES)
    def test_phone_width_has_no_horizontal_scroll(
        self, session: Callable[..., Session], path: str, scheme: str
    ) -> None:
        current = session(viewport=PHONE, color_scheme=scheme).open(path)
        result = current.page.evaluate(OVERFLOW)
        assert result["scroll"] <= result["width"]
        assert result["wide"] == []
        background, text = (luminance(color) for color in current.page.evaluate(COLORS))
        if scheme == "dark":
            assert background < 0.2 < 0.7 < text
        else:
            assert text < 0.3 < 0.9 < background
        current.assert_clean()

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    @pytest.mark.parametrize("path", PAGES)
    def test_phone_width_has_no_horizontal_scroll_with_wider_text(
        self, session: Callable[..., Session], site_dir: Path, path: str, scheme: str
    ) -> None:
        current = session(viewport=PHONE, color_scheme=scheme)
        widen_text(current, site_dir)
        current.open(path)
        assert current.page.evaluate("getComputedStyle(document.body).letterSpacing") != "normal"
        result = current.page.evaluate(OVERFLOW)
        assert result["scroll"] <= result["width"]
        assert result["wide"] == []
        current.assert_clean()

    @pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=["phone", "desktop"])
    @pytest.mark.parametrize("path", PAGES)
    def test_every_value_of_a_fact_list_sits_in_the_value_column(
        self, session: Callable[..., Session], path: str, viewport: dict[str, int]
    ) -> None:
        # A label can have several values (the reasons a source missed events); each one
        # goes beside the labels, never into the column of the labels.
        current = session(viewport=viewport).open(path)
        lists = current.page.evaluate(FACT_COLUMNS)
        assert lists
        for columns in lists:
            assert len(set(columns["labels"])) == 1, columns
            assert len(set(columns["values"])) == 1, columns
            assert columns["values"][0] > columns["labels"][0], columns
        if path.startswith("status.html"):
            assert any(len(columns["values"]) > len(columns["labels"]) for columns in lists)
        current.assert_clean()

    def test_desktop_puts_the_subscription_panel_beside_the_list(
        self, session: Callable[..., Session]
    ) -> None:
        current = session().open("index.html")
        talks = current.page.locator(".talks").bounding_box()
        panel = current.page.locator(".subscribe").bounding_box()
        assert talks is not None
        assert panel is not None
        assert panel["x"] >= talks["x"] + talks["width"]
        result = current.page.evaluate(OVERFLOW)
        assert result["scroll"] <= result["width"]
        current.assert_clean()

    def test_phone_stacks_the_panel_below_the_list(self, session: Callable[..., Session]) -> None:
        current = session(viewport=PHONE).open("index.html")
        talks = current.page.locator(".talks").bounding_box()
        panel = current.page.locator(".subscribe").bounding_box()
        assert talks is not None
        assert panel is not None
        assert panel["y"] >= talks["y"] + talks["height"]
        current.assert_clean()
