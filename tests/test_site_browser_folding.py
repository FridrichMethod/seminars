"""Browser tests: the search folds what typing cannot tell apart.

Scraped text keeps its typographic quotes, dashes and letters (the charset rule treats
them as data), and a reader types the plain ASCII forms. The search therefore folds
both sides the same way: accents go, typographic apostrophes, quotes and dashes become
their ASCII forms, and letters without a decomposition (such as l with a stroke or a
sharp s) become the letters a reader types for them. The server, the browser and the
sessions are those of test_site_browser.py; its fixtures are bound to names of this
module, so pytest finds them here as well. Marked ``browser`` like that module:
``uv run pytest -m browser``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import pytest

import test_site_browser as harness
from seminars.i18n import LOCALES
from seminars.models import TBA_TITLE
from test_site_browser import Session, catalog, sample, serve_json

pytestmark = pytest.mark.browser

site_dir = harness.site_dir
base_url = harness.base_url
browser = harness.browser
session = harness.session

FOLD = "async (text) => (await import('./filters.js')).fold(text)"

TALKS = {
    "apostrophe": "Cell\u2019s Stress Response",
    "dash": "Work with McKeithen\u2013Mead and Kim\u2014Lee on non\u2011linear maps",
    "quotes": "\u201cGenerative\u201d models \u2212 a \u201equick\u201c look",
    "letters": (
        "\u0141ukasz D\u0119bowski, S\u00f8ren \u00d8rsted, Stra\u00dfe, \u00c6sir, \u0110or\u0111e"
    ),
    "plain": "Cell's plain-text talk",
}


def payload() -> dict[str, Any]:
    """The sample events.json with one upcoming talk per title above."""
    data = sample("events.json")
    arc = next(event for event in data["events"] if event["uid"] == "sample-arc")
    data["events"] = [{**arc, "uid": uid, "title": title} for uid, title in TALKS.items()]
    return data


@pytest.fixture
def current(session: Callable[..., Session]) -> Session:
    opened = session()
    serve_json(opened, "events.json", payload())
    return opened.open("index.html")


class TestFold:
    @pytest.mark.parametrize(
        ("text", "folded"),
        [
            ("Zo\u00e9 M\u00fcller", "zoe muller"),
            ("Cell\u2019s \u2018x\u2019 \u201ay\u201b \u2032 \u02bc", "cell's 'x' 'y' ' '"),
            ("\u201cA\u201d \u201eB\u201f \u00abC\u00bb", '"a" "b" "c"'),
            ("a\u2010b\u2011c\u2012d\u2013e\u2014f\u2015g\u2212h", "a-b-c-d-e-f-g-h"),
            ("\u0141\u0142 \u00d8\u00f8 \u0110\u0111 \u00f0 \u00df \u1e9e", "ll oo dd d ss ss"),
            ("\u00c6\u00e6 \u0152\u0153 \u0131 \u00de\u00fe \u0126\u0127", "aeae oeoe i thth hh"),
            ("plain ASCII - 'quoted' \"too\"", "plain ascii - 'quoted' \"too\""),
        ],
    )
    def test_fold(self, current: Session, text: str, folded: str) -> None:
        assert current.page.evaluate(FOLD, text) == folded
        current.assert_clean()


class TestSearch:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("Cell's Stress", ["apostrophe"]),
            ("cell\u2019s", ["apostrophe", "plain"]),
            ("McKeithen-Mead", ["dash"]),
            ("kim-lee non-linear", ["dash"]),
            ('"generative" - a', ["quotes"]),
            ("Lukasz Debowski", ["letters"]),
            ("soren orsted strasse aesir dorde", ["letters"]),
            ("plain\u2010text", ["plain"]),
        ],
    )
    def test_a_query_typed_in_plain_ascii_finds_typographic_text(
        self, current: Session, query: str, expected: list[str]
    ) -> None:
        current.page.locator("#search").fill(query)
        current.page.locator("#search").press("Enter")
        assert current.shown() == expected
        current.assert_clean()


class TestPlaceholderSearch:
    """A talk without a title shows the placeholder of the page's language, and is found
    by it: the search text of such a talk holds the placeholder of every language."""

    @pytest.mark.parametrize("page_lang", ["en", "zh"])
    @pytest.mark.parametrize("word_lang", LOCALES)
    def test_the_placeholder_of_every_language_finds_the_talk(
        self, session: Callable[..., Session], page_lang: str, word_lang: str
    ) -> None:
        word = catalog(word_lang)["site.event.tba"]
        current = session().open(f"index.html?lang={page_lang}&q={quote(word)}")
        assert current.shown() == ["sample-tba"]
        current.assert_clean()

    def test_a_title_that_only_contains_the_word_gets_no_placeholder(
        self, session: Callable[..., Session]
    ) -> None:
        payload = sample("events.json")
        tba = next(event for event in payload["events"] if event["uid"] == "sample-tba")
        tba["title"] = f"{TBA_TITLE}: a talk on knots"
        current = session()
        serve_json(current, "events.json", payload)
        current.open(f"index.html?lang=zh&q={quote(catalog('zh-CN')['site.event.tba'])}")
        assert current.shown() == []
        current.assert_clean()
