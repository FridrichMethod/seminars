"""What the README, the source guide and the site's tagline promise about the project.

These texts state the scope of the collection, how the feeds behave for a subscriber,
how the site saves selected talks and what turning a source off does. The English and
the Chinese version say the same, the scope names only institutions that a configured
source collects, and the buttons and the file of the download are named as the site
names them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from seminars.config import load_config
from seminars.i18n import flatten

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(REPO_ROOT, env={})
INSTITUTION_NAMES = {"slac": "SLAC", "arc": "Arc Institute", "biohub": "CZ Biohub"}
PAIRS = [
    ("README.md", "README.zh-CN.md"),
    ("docs/ADDING_A_SOURCE.md", "docs/ADDING_A_SOURCE.zh-CN.md"),
    ("docs/ARCHITECTURE.md", "docs/ARCHITECTURE.zh-CN.md"),
]
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
SELECTED_FILE = re.compile(r'^const SELECTED_FILE = "([^"]+)";$', re.MULTILINE)
DOWNLOAD_SECTIONS = {
    "en": ("README.md", "## Download selected talks"),
    "zh-CN": ("README.zh-CN.md", "## \u4e0b\u8f7d\u9009\u4e2d\u7684\u62a5\u544a"),
}
DOWNLOAD_BUTTONS = ("site.select.select_shown", "site.select.clear", "site.select.download")


def text(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def section(name: str, heading: str) -> str:
    """The text below ``heading`` (a whole heading line) up to the next one, on one line."""
    document = text(name)
    start = document.index(f"\n{heading}\n")
    rest = document[start + len(heading) + 2 :]
    level = heading.index(" ")  # the number of "#"
    following = re.search(rf"\n#{{1,{level}}} ", rest)
    body = rest[: following.start()] if following else rest
    return " ".join(body.split())  # a sentence may wrap at any space


def scope_texts() -> dict[str, str]:
    """Every text that states which institutions the talks come from."""
    texts = {}
    for lang in ("en", "zh-CN"):
        messages = flatten(json.loads(text(f"locales/{lang}.json")))
        texts[f"{lang} tagline"] = messages["site.app.tagline"]
        texts[f"{lang} all feed"] = messages["feeds.all.description"]
    for name in ("README.md", "README.zh-CN.md"):
        document = text(name)
        texts[f"{name} opening"] = document.split("\n## ")[0]
        texts[f"{name} all feed"] = next(
            line for line in document.split("\n") if "| `all` |" in line
        )
    for name in PAIRS[1]:
        texts[f"{name} groups"] = next(
            line for line in text(name).split("\n") if "| `bayarea` |" in line
        )
    return texts


@pytest.mark.parametrize(("institution", "name"), INSTITUTION_NAMES.items())
def test_the_scope_names_an_institute_exactly_when_a_source_collects_it(
    institution: str, name: str
) -> None:
    collected = {source.institution for source in CONFIG.enabled_sources}
    texts = scope_texts()
    naming = sorted(where for where, value in texts.items() if name in value)
    assert naming == (sorted(texts) if institution in collected else [])


def test_subscribers_learn_about_overlapping_feeds_and_the_end_of_a_talk() -> None:
    days = CONFIG.settings.window.days_back
    english = section("README.md", "## Subscribe")
    chinese = section("README.zh-CN.md", "## \u8ba2\u9605")
    assert "once per feed" in english
    assert f"{days} days after" in english
    assert "\u6bcf\u4e2a feed \u4e00\u6b21" in chinese
    assert f"{days} \u5929\u540e" in chinese


def test_the_digest_is_described_as_the_week_it_is_posted_in() -> None:
    english = section("README.md", "### Weekly digest")
    chinese = section("README.zh-CN.md", "### \u6bcf\u5468\u6458\u8981")
    assert "of that week (Monday to Sunday)" in english
    assert "\u672c\u5468\uff08\u5468\u4e00\u5230\u5468\u65e5\uff09" in chinese
    assert "\u4e0b\u4e00\u5468" not in chinese


@pytest.mark.parametrize("lang", DOWNLOAD_SECTIONS)
def test_the_download_of_selected_talks_is_described_as_the_site_offers_it(lang: str) -> None:
    (file_name,) = SELECTED_FILE.findall(text("site/app.js"))
    messages = flatten(json.loads(text(f"locales/{lang}.json")))
    body = section(*DOWNLOAD_SECTIONS[lang])
    assert f"`{file_name}`" in body
    for key in DOWNLOAD_BUTTONS:
        assert messages[key] in body, key


def test_the_architecture_names_the_file_of_selected_talks() -> None:
    (file_name,) = SELECTED_FILE.findall(text("site/app.js"))
    for name in PAIRS[2]:
        assert f"`{file_name}`" in text(name), name


def test_the_guide_says_what_turning_a_source_off_does() -> None:
    english, chinese = (text(name) for name in PAIRS[1])
    assert "removes its future talks at the next run" in english
    assert "\u4e0b\u4e00\u6b21\u8fd0\u884c\u65f6" in chinese
    for document in (english, chinese):
        assert "`enabled: false`" in document


@pytest.mark.parametrize("pair", PAIRS, ids=lambda pair: pair[1])
def test_the_chinese_prose_holds_no_escaped_characters(pair: tuple[str, str]) -> None:
    prose = CODE.sub("", text(pair[1]))
    assert re.findall(r"\\u[0-9a-fA-F]{4}|&#x?[0-9a-fA-F]+;", prose) == []
