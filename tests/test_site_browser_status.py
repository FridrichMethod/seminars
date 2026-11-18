"""Browser tests of the source status page.

Every source of status.json is listed with its state, its group and the facts of its
last run; values that cannot be shown are left out. The server, the browser and the
sessions are those of test_site_browser.py; its fixtures are bound to names of this
module, so pytest finds them here as well. Marked ``browser`` like that module:
``uv run pytest -m browser``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

import test_site_browser as harness
from test_site_browser import Session, catalog, sample, serve_json

PARTIAL = 'article[data-source="sites-mathematics"]'
RESIZED = 'article[data-source="sites-statistics"]'

pytestmark = pytest.mark.browser

# The facts of every source card, as {source id: {term: description}}.
FACTS = """() => Object.fromEntries(
  [...document.querySelectorAll("#sources article")].map((card) => [
    card.dataset.source,
    Object.fromEntries(
      [...card.querySelectorAll("dt")].map((term) => [
        term.textContent,
        term.nextElementSibling.textContent,
      ]),
    ),
  ]),
)"""

site_dir = harness.site_dir
base_url = harness.base_url
browser = harness.browser
session = harness.session


class TestStatusPage:
    def test_status_values_that_cannot_be_shown_are_left_out(
        self, session: Callable[..., Session]
    ) -> None:
        payload = {
            "generated_at": "not a time",
            "last_run_at": None,
            "last_run_ok": False,
            "event_count": -3,
            "llm_provider": None,
            "sources": [
                None,
                "text",
                {"id": "custom-nameless", "state": "ok"},
                {
                    "id": "custom-odd",
                    "name": "Odd source",
                    "group": 5,
                    "url": "javascript:alert(1)",
                    "state": "exploded",
                    "events": 2.5,
                    "last_run_at": "yesterday",
                    "consecutive_failures": -1,
                    "error": "",
                },
                {
                    "id": "custom-off",
                    "name": "Switched off",
                    "group": "campus-radio",
                    "url": "file://example.org/etc/passwd",
                    "state": "disabled",
                },
            ],
        }
        current = session()
        serve_json(current, "status.json", payload)
        current.open("status.html")
        page = current.page
        english = catalog("en")
        assert page.locator("#updated").text_content() == ""
        assert page.locator("#summary li").all_text_contents() == [
            english["site.status.no_run"],
            english["site.status.last_run_failed"],
            "0 events on the site",
            "0 of 1 active sources are healthy.",
            english["site.status.llm_none"],
        ]
        ids = page.eval_on_selector_all("#sources article", "es => es.map((e) => e.dataset.source)")
        assert ids == ["custom-odd", "custom-off"]
        odd = page.locator('article[data-source="custom-odd"]')
        assert odd.locator(".source-name a").count() == 0
        off = page.locator('article[data-source="custom-off"]')
        assert off.locator(".source-name a").count() == 0
        assert off.locator(".source-name").text_content() == "Switched off"
        group = english["site.status.columns.group"]
        assert current.page.evaluate(FACTS)["custom-off"][group] == "campus-radio"
        assert odd.locator(".badge-state").text_content() == english["site.status.states.unknown"]
        assert odd.locator("dt").all_text_contents() == [
            english["site.status.columns.events"],
            english["site.status.columns.last_run"],
            english["site.status.columns.last_success"],
            english["site.status.columns.failures"],
        ]
        never = english["site.status.never"]
        assert odd.locator("dd").all_text_contents() == ["0", never, never, "0"]
        current.assert_clean()

        empty = session()
        serve_json(empty, "status.json", {"generated_at": "2026-11-10T12:30:00Z"})
        empty.open("status.html")
        notice = empty.page.locator("#sources .notice").text_content()
        assert notice == english["site.status.empty"]
        empty.assert_clean()

    def test_status_page_lists_every_source(self, session: Callable[..., Session]) -> None:
        current = session().open("status.html")
        page = current.page
        ids = page.eval_on_selector_all("#sources article", "es => es.map((e) => e.dataset.source)")
        assert ids == [source["id"] for source in sample("status.json")["sources"]]
        states = page.locator("#sources .badge-state").all_text_contents()
        english = catalog("en")
        assert states == [
            english[f"site.status.states.{source['state']}"]
            for source in sample("status.json")["sources"]
        ]
        summary = page.locator("#summary").text_content() or ""
        assert "3 of 7 active sources are healthy." in summary
        assert "claude-cli" in summary
        unlinked = page.locator('article[data-source="rsorg-new-series"] .source-name a')
        assert unlinked.count() == 0
        current.assert_clean()

    @pytest.mark.parametrize(
        ("path", "lang"), [("status.html", "en"), ("status.html?lang=zh", "zh-CN")]
    )
    def test_known_groups_are_named_in_the_language_of_the_page(
        self, session: Callable[..., Session], path: str, lang: str
    ) -> None:
        current = session().open(path)
        messages = catalog(lang)
        facts = current.page.evaluate(FACTS)
        sources = sample("status.json")["sources"]
        assert {source["group"] for source in sources} == {
            key.removeprefix("site.status.groups.")
            for key in messages
            if key.startswith("site.status.groups.")
        }
        for source in sources:
            shown = facts[source["id"]][messages["site.status.columns.group"]]
            assert shown == messages[f"site.status.groups.{source['group']}"], source["id"]
        current.assert_clean()

    @pytest.mark.parametrize(
        ("path", "lang"), [("status.html", "en"), ("status.html?lang=zh", "zh-CN")]
    )
    def test_a_disabled_source_says_why_it_is_off(
        self, session: Callable[..., Session], path: str, lang: str
    ) -> None:
        messages = catalog(lang)
        prefix = "site.status.disabled_reasons."
        first, second = [key.removeprefix(prefix) for key in messages if key.startswith(prefix)][:2]
        payload = {
            "generated_at": "2026-11-16T12:30:00Z",
            "sources": [
                {
                    "id": first,
                    "name": "Off, with a reason",
                    "group": "bayarea",
                    "state": "disabled",
                },
                {
                    "id": "custom-off",
                    "name": "Off, no reason",
                    "group": "bayarea",
                    "state": "disabled",
                },
                {"id": second, "name": "Back on", "group": "bayarea", "state": "ok", "events": 3},
            ],
        }
        current = session()
        serve_json(current, "status.json", payload)
        current.open(path)
        facts = current.page.evaluate(FACTS)
        term = messages["site.status.columns.disabled_reason"]
        assert facts[first][term] == messages[prefix + first]
        assert term not in facts["custom-off"]
        assert term not in facts[second]  # a reason is only shown while the source is off
        terms = current.page.locator(f'article[data-source="{first}"] dt').all_text_contents()
        assert terms[:2] == [messages["site.status.columns.group"], term]
        current.assert_clean()

    def test_the_error_of_a_source_is_marked_as_english_diagnostic_text(
        self, session: Callable[..., Session]
    ) -> None:
        """The message comes from the run (HTTP status, error kind, address), not a locale."""
        current = session().open("status.html?lang=zh")
        error = current.page.locator('article[data-source="custom-nlp"] dd code')
        expected = next(
            source["error"] for source in sample("status.json")["sources"] if source["error"]
        )
        assert error.text_content() == expected
        assert error.get_attribute("lang") == "en"
        assert error.get_attribute("translate") == "no"
        current.assert_clean()

    @pytest.mark.parametrize(
        ("path", "lang"), [("status.html", "en"), ("status.html?lang=zh", "zh-CN")]
    )
    def test_a_source_not_read_in_full_says_so_and_why(
        self, session: Callable[..., Session], path: str, lang: str
    ) -> None:
        current = session().open(path)
        messages = catalog(lang)
        partial = current.page.locator(PARTIAL)
        assert partial.locator(".badge-state").text_content() == messages["site.status.states.ok"]
        assert partial.locator(".badge-partial").text_content() == messages["site.status.partial"]
        missed = messages["site.status.columns.missed"]
        term = partial.locator("dt", has_text=missed)
        assert term.count() == 1
        reasons = partial.locator(f'dt:text-is("{missed}") ~ dd.missed')
        assert reasons.all_text_contents() == [
            messages["site.status.skipped_other"].replace("{count}", "2"),
            messages["site.status.shrank"].replace("{listed}", "5").replace("{usual}", "20"),
        ]
        resized = current.page.locator(RESIZED)
        assert resized.locator(".badge-partial").count() == 0
        note = current.page.evaluate(FACTS)["sites-statistics"][
            messages["site.status.columns.resized"]
        ]
        assert note == messages["site.status.resized"].replace("{listed}", "6").replace(
            "{usual}", "18"
        )
        assert partial.locator("dt", has_text=messages["site.status.columns.resized"]).count() == 0
        current.assert_clean()

    def test_one_page_that_could_not_be_fetched_is_said_in_the_singular(
        self, session: Callable[..., Session]
    ) -> None:
        source = {
            "id": "sites-one",
            "name": "One page short",
            "group": "stanford-sites",
            "state": "ok",
            "read_in_full": False,
            "skipped_pages": 1,
            "listed": None,
            "usual_size": None,
            "resized_from": None,
        }
        current = session()
        serve_json(
            current, "status.json", {"generated_at": "2026-11-10T12:30:00Z", "sources": [source]}
        )
        current.open("status.html")
        english = catalog("en")
        card = current.page.locator('article[data-source="sites-one"]')
        assert card.locator("dd.missed").all_text_contents() == [
            english["site.status.skipped_one"].replace("{count}", "1")
        ]
        summary = current.page.locator("#summary").text_content() or ""
        assert "0 of 1 active sources are healthy." in summary
        current.assert_clean()

    @pytest.mark.parametrize(
        ("path", "lang", "stored", "key"),
        [
            ("status.html", "en", 4, "site.status.llm_stored_other"),
            ("status.html", "en", 1, "site.status.llm_stored_one"),
            ("status.html?lang=zh", "zh-CN", 4, "site.status.llm_stored_other"),
        ],
    )
    def test_a_run_whose_provider_did_not_answer_says_how_many_stored_answers_it_used(
        self, session: Callable[..., Session], path: str, lang: str, stored: int, key: str
    ) -> None:
        payload = {
            "generated_at": "2026-11-10T12:30:00Z",
            "llm_provider": None,
            "llm_stored_answers": stored,
            "sources": [],
        }
        current = session()
        serve_json(current, "status.json", payload)
        current.open(path)
        lines = current.page.locator("#summary li").all_text_contents()
        assert lines[-1] == catalog(lang)[key].replace("{count}", str(stored))
        current.assert_clean()

    @pytest.mark.parametrize("stored", [0, -2, 2.5, "4", None])
    def test_no_stored_answer_or_an_odd_count_is_rules_only(
        self, session: Callable[..., Session], stored: object
    ) -> None:
        payload = {"generated_at": "2026-11-10T12:30:00Z", "llm_stored_answers": stored}
        current = session()
        serve_json(current, "status.json", payload)
        current.open("status.html")
        lines = current.page.locator("#summary li").all_text_contents()
        assert lines[-1] == catalog("en")["site.status.llm_none"]
        current.assert_clean()

    def test_a_provider_that_answered_is_named_whatever_the_count(
        self, session: Callable[..., Session]
    ) -> None:
        payload = {
            "generated_at": "2026-11-10T12:30:00Z",
            "llm_provider": "claude-cli",
            "llm_stored_answers": 4,
        }
        current = session()
        serve_json(current, "status.json", payload)
        current.open("status.html")
        lines = current.page.locator("#summary li").all_text_contents()
        assert lines[-1] == catalog("en")["site.status.llm"].replace("{provider}", "claude-cli")
        current.assert_clean()
