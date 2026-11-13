"""Tests for the locale files and the Python locale loader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from seminars.i18n import LOCALES, Locale, bilingual, flatten, load_locales, placeholders
from seminars.models import FEED_IDS, FIELD_IDS

LOCALES_DIR = Path(__file__).resolve().parents[1] / "locales"

DIGEST_KEYS = {
    "digest.issue_title",
    "digest.heading",
    "digest.intro",
    "digest.summary",
    "digest.sections.picks",
    "digest.sections.new",
    "digest.sections.health",
    "digest.table.date",
    "digest.table.time",
    "digest.table.title",
    "digest.table.speaker",
    "digest.table.location",
    "digest.table.link",
    "digest.table.field",
    "digest.table.reasons",
    "digest.labels.all_day",
    "digest.labels.online",
    "digest.labels.cancelled",
    "digest.labels.tentative",
    "digest.labels.tba",
    "digest.labels.details",
    "digest.labels.more",
    "digest.reasons.keyword",
    "digest.reasons.series",
    "digest.reasons.speaker",
    "digest.reasons.penalty",
    "digest.reasons.places.title",
    "digest.reasons.places.series",
    "digest.reasons.places.abstract",
    "digest.empty.picks",
    "digest.empty.field",
    "digest.empty.new",
    "digest.empty.first_issue",
    "digest.health.all_ok",
    "digest.health.summary",
    "digest.health.source",
    "digest.health.status",
    "digest.health.events",
    "digest.health.last_success",
    "digest.health.error",
    "digest.health.status_ok",
    "digest.health.status_empty",
    "digest.health.status_failed",
    "digest.health.status_partial",
    "digest.health.never",
    "digest.health.skipped",
    "digest.health.shrank",
    "digest.health.resized",
    "digest.health.llm",
    "digest.health.llm_none",
    "digest.health.llm_stored",
    "digest.footer.site",
    "digest.footer.notify",
}


def raw(lang: str) -> dict[str, Any]:
    return json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def locales() -> dict[str, Locale]:
    return load_locales(LOCALES_DIR)


class TestLocaleFiles:
    def test_both_languages_exist(self, locales: dict[str, Locale]) -> None:
        assert LOCALES == ("en", "zh-CN")
        assert set(locales) == {"en", "zh-CN"}

    def test_key_sets_are_identical(self, locales: dict[str, Locale]) -> None:
        assert locales["en"].keys() == locales["zh-CN"].keys()

    def test_placeholders_are_identical(self, locales: dict[str, Locale]) -> None:
        english, chinese = flatten(raw("en")), flatten(raw("zh-CN"))
        different = [
            key for key in english if placeholders(english[key]) != placeholders(chinese[key])
        ]
        assert different == []

    @pytest.mark.parametrize("lang", LOCALES)
    def test_namespaces(self, lang: str) -> None:
        tree = raw(lang)
        assert list(tree) == ["fields", "feeds", "digest", "site"]
        assert isinstance(tree["site"], dict)
        assert list(tree["fields"]) == list(FIELD_IDS)
        assert list(tree["feeds"]) == list(FEED_IDS)
        assert all(set(feed) == {"name", "description"} for feed in tree["feeds"].values())

    @pytest.mark.parametrize("lang", LOCALES)
    def test_digest_keys_are_complete(self, lang: str) -> None:
        keys = {key for key in flatten(raw(lang)) if key.startswith("digest.")}
        assert keys == DIGEST_KEYS

    @pytest.mark.parametrize("lang", LOCALES)
    def test_messages_are_non_empty_and_trimmed(self, lang: str) -> None:
        for key, message in flatten(raw(lang)).items():
            assert message, key
            assert message == message.strip(), key

    def test_english_is_ascii(self) -> None:
        assert (LOCALES_DIR / "en.json").read_bytes().isascii()

    def test_chinese_is_translated(self) -> None:
        chinese = flatten(raw("zh-CN"))
        owned = [key for key in chinese if key.split(".")[0] in ("fields", "feeds", "digest")]
        assert [key for key in owned if chinese[key].isascii()] == []

    def test_english_names(self, locales: dict[str, Locale]) -> None:
        english = locales["en"]
        assert english.t("fields.math") == "Math"
        assert english.t("feeds.math.name") == "Seminars - Math"
        assert english.t("feeds.picks.name") == "Seminars - Picks"
        assert english.t("feeds.all.name") == "Seminars - All"
        assert english.t("digest.issue_title", date="2026-11-16") == "Seminars - week of 2026-11-16"
        assert english.t("digest.sections.picks") == "Picks"
        assert english.t("digest.sections.new") == "New since last issue"
        assert english.t("digest.sections.health") == "Source health"
        for field in FIELD_IDS:
            assert english.t(f"feeds.{field}.name") == f"Seminars - {english.t(f'fields.{field}')}"

    @pytest.mark.parametrize("lang", LOCALES)
    def test_files_use_two_space_indentation_and_end_with_newline(self, lang: str) -> None:
        text = (LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8")
        assert text == json.dumps(raw(lang), ensure_ascii=False, indent=2) + "\n"


class TestLocale:
    def test_placeholders_are_filled(self) -> None:
        locale = Locale("en", {"a.b": "{ok} of {total} sources", "plain": "Hello"})
        assert locale.t("a.b", ok=3, total=5) == "3 of 5 sources"
        assert locale.t("plain", unused="x") == "Hello"
        assert "a.b" in locale
        assert "a.c" not in locale
        assert locale.keys() == ["a.b", "plain"]

    def test_missing_key_or_value(self) -> None:
        locale = Locale("en", {"a": "{count} more"})
        with pytest.raises(KeyError, match="has no key 'b'"):
            locale.t("b")
        with pytest.raises(KeyError, match=r"needs a value for \{count\}"):
            locale.t("a")

    def test_other_braces_are_left_alone(self) -> None:
        locale = Locale("en", {"a": "{} {Not_A_Placeholder} {x}"})
        assert locale.t("a", x=1) == "{} {Not_A_Placeholder} 1"

    def test_flatten(self) -> None:
        assert flatten({"a": {"b": "x", "c": {"d": "y"}}, "site": {}}) == {"a.b": "x", "a.c.d": "y"}
        with pytest.raises(ValueError, match="must be a string or an object"):
            flatten({"a": 1})

    def test_load_rejects_non_objects(self, tmp_path: Path) -> None:
        (tmp_path / "en.json").write_text("[]\n", encoding="utf-8")
        with pytest.raises(ValueError, match="must contain a JSON object"):
            Locale.load("en", tmp_path)

    def test_bilingual(self, locales: dict[str, Locale]) -> None:
        text = bilingual("digest.sections.picks", locales["en"], locales["zh-CN"])
        english, _, chinese = text.partition(" / ")
        assert english == "Picks"
        assert chinese == locales["zh-CN"].t("digest.sections.picks")
        same = Locale("en", {"k": "OK"})
        assert bilingual("k", same, same) == "OK"
