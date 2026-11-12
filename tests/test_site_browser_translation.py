"""Browser tests: what the pipeline writes in its own words is said in the page's language.

Three things on the talk list are not scraped text although they look like it: the
reasons of a pick (built from the interests by the picks scorer), the placeholder title
"TBA" (stored for a talk whose title is not announced) and the time zone of the list.
Each of them is shown in the language of the page, while the scraped content and the
terms of the configuration stay as they are. The server, the browser and the sessions
are those of test_site_browser.py; its fixtures are bound to names of this module, so
pytest finds them here as well. Marked ``browser`` like that module:
``uv run pytest -m browser``.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

import test_site_browser as harness
from seminars.models import TBA_TITLE
from site_sample import sample_events
from test_site_browser import Session, catalog, sample, serve_json

pytestmark = pytest.mark.browser

site_dir = harness.site_dir
base_url = harness.base_url
browser = harness.browser
session = harness.session

PICKS = ["sample-pick", "sample-all-day", "sample-arc"]
QUOTED = re.compile(r'"[^"]*"')
LATIN = re.compile(r"[A-Za-z]")
ZONE_NAME = """(lang) => new Intl.DateTimeFormat(lang, {
  timeZone: "America/Los_Angeles", timeZoneName: "longGeneric",
}).formatToParts(new Date()).find((part) => part.type === "timeZoneName").value"""


def stored_reasons(uid: str) -> list[str]:
    """The reason lines of a sample event as data/ and the ICS feeds hold them."""
    return next(event.picks_reasons for event in sample_events() if event.uid == uid)


def reasons_shown(current: Session, uid: str) -> list[str]:
    return current.page.locator(f'article[data-uid="{uid}"] .reasons li').all_text_contents()


class TestPicksReasons:
    def test_english_reasons_read_like_the_calendar_feeds(
        self, session: Callable[..., Session]
    ) -> None:
        current = session().open("index.html")
        for uid in PICKS:
            assert reasons_shown(current, uid) == stored_reasons(uid), uid
        current.assert_clean()

    def test_chinese_reasons_keep_only_the_configured_terms_in_english(
        self, session: Callable[..., Session]
    ) -> None:
        current = session().open("index.html?lang=zh")
        messages = catalog("zh-CN")
        keyword, series = (messages[f"site.event.reasons.{kind}"] for kind in ("keyword", "series"))
        title = messages["site.event.reasons.places.title"]
        assert reasons_shown(current, "sample-pick") == [
            keyword.format(place=title, term="protein design", points="+3"),
            keyword.format(place=title, term="diffusion model", points="+3"),
            series.format(term="Bioengineering Seminar", points="+3"),
        ]
        shown = [reason for uid in PICKS for reason in reasons_shown(current, uid)]
        assert len(shown) == sum(len(stored_reasons(uid)) for uid in PICKS)
        assert [reason for reason in shown if LATIN.search(QUOTED.sub("", reason))] == []
        current.assert_clean()

    def test_the_reasons_follow_the_language_switch(self, session: Callable[..., Session]) -> None:
        current = session().open("index.html")
        current.page.locator('.lang-switch button[lang="zh-CN"]').click()
        penalty = catalog("zh-CN")["site.event.reasons.penalty"]
        expected = penalty.format(term="undergraduate", points="-1.5")
        assert reasons_shown(current, "sample-all-day")[-1] == expected
        current.page.locator('.lang-switch button[lang="en"]').click()
        assert reasons_shown(current, "sample-all-day") == stored_reasons("sample-all-day")
        current.assert_clean()

    def test_a_reason_in_no_known_form_is_shown_as_it_is(
        self, session: Callable[..., Session]
    ) -> None:
        payload = sample("events.json")
        pick = next(event for event in payload["events"] if event["uid"] == "sample-pick")
        pick["picks_reasons"] = [
            {"kind": "other", "term": "listed by hand", "place": None, "points": None},
            "a line from an older payload",
            {"kind": "keyword", "term": "nowhere", "place": "footer", "points": 1},
            {"kind": "series", "term": "Points as text", "place": None, "points": "3"},
            {"kind": "praise", "term": "unknown kind", "place": None, "points": 1},
            {"kind": "series", "term": " ", "place": None, "points": 1},
            {"kind": "series", "term": "Math Colloquium", "place": None, "points": 0.25},
            None,
            7,
        ]
        current = session()
        serve_json(current, "events.json", payload)
        current.open("index.html?lang=zh")
        series = catalog("zh-CN")["site.event.reasons.series"]
        assert reasons_shown(current, "sample-pick") == [
            "listed by hand",
            "a line from an older payload",
            "nowhere",
            "Points as text",
            "unknown kind",
            series.format(term="Math Colloquium", points="+0.25"),
        ]
        current.assert_clean()


class TestPlaceholderTitle:
    @pytest.mark.parametrize("lang", ["en", "zh-CN"])
    def test_an_unannounced_title_is_said_in_the_language_of_the_page(
        self, session: Callable[..., Session], lang: str
    ) -> None:
        current = session().open("index.html" if lang == "en" else "index.html?lang=zh")
        title = current.page.locator('article[data-uid="sample-tba"] .event-title')
        assert title.text_content() == catalog(lang)["site.event.tba"]
        assert title.locator("a").get_attribute("href") is not None
        current.assert_clean()

    def test_the_placeholder_is_the_english_word_and_stays_searchable(
        self, session: Callable[..., Session]
    ) -> None:
        assert catalog("en")["site.event.tba"] == TBA_TITLE
        current = session().open(f"index.html?lang=zh&q={TBA_TITLE}")
        assert current.shown() == ["sample-tba"]
        current.assert_clean()

    def test_a_title_that_only_contains_the_word_is_kept(
        self, session: Callable[..., Session]
    ) -> None:
        payload = sample("events.json")
        tba = next(event for event in payload["events"] if event["uid"] == "sample-tba")
        tba["title"] = "TBA: a talk on knots"
        current = session()
        serve_json(current, "events.json", payload)
        current.open("index.html?lang=zh")
        title = current.page.locator('article[data-uid="sample-tba"] .event-title')
        assert title.text_content() == "TBA: a talk on knots"
        current.assert_clean()


class TestTimeZone:
    @pytest.mark.parametrize("lang", ["en", "zh-CN"])
    def test_the_time_zone_has_its_name_in_the_language_of_the_page(
        self, session: Callable[..., Session], lang: str
    ) -> None:
        current = session().open("index.html" if lang == "en" else "index.html?lang=zh")
        name = current.page.evaluate(ZONE_NAME, lang)
        sentence = current.page.locator("#timezone").text_content() or ""
        assert sentence == catalog(lang)["site.meta.timezone"].format(timezone=name)
        assert "America/Los_Angeles" not in sentence
        if lang == "en":
            assert sentence == "All times are Pacific Time."
        current.assert_clean()

    def test_an_unknown_zone_falls_back_to_utc_and_its_name(
        self, session: Callable[..., Session]
    ) -> None:
        payload = sample("events.json")
        payload["timezone"] = "Mars/Olympus_Mons"
        current = session()
        serve_json(current, "events.json", payload)
        current.open("index.html")
        sentence = current.page.locator("#timezone").text_content() or ""
        timezone = "Coordinated Universal Time"  # not the bare offset "GMT+00:00"
        assert sentence == catalog("en")["site.meta.timezone"].format(timezone=timezone)
        current.assert_clean()
