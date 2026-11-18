"""What the documents say about the deadlines of conferences and workshops.

The README describes the feed deadlines.ics at its published address and points readers of
the submission deadlines of the main tracks of machine-learning and computer-science
conferences to a calendar that is not collected here; it and the architecture quote the
days and the rows of the digest as the code has them; the guide to the registry names the
key and the rules of curation of a milestone.
"""

from __future__ import annotations

import pytest

from seminars.config import load_config
from seminars.outputs.digest import DEADLINE_DAYS, DEADLINE_ROWS
from seminars.sources.custom.registry import MAX_SHORT_TITLE
from test_docs_scope import REPO_ROOT, section, text

CCFDDL_FEED = "https://ccfddl.com/conference/deadlines/deadlines_en_AI.ics"
CCFDDL_PROJECT = "https://github.com/ccfddl/ccf-deadlines"
SUBSCRIBE = {
    "en": ("README.md", "## Subscribe"),
    "zh-CN": ("README.zh-CN.md", "## \u8ba2\u9605"),
}


@pytest.mark.parametrize("lang", SUBSCRIBE)
def test_the_subscribe_section_names_the_ccfddl_calendar_and_its_license(lang: str) -> None:
    body = section(*SUBSCRIBE[lang])
    assert f"`{CCFDDL_FEED}`" in body
    assert f"({CCFDDL_PROJECT})" in body
    assert "MIT" in body


BASE_URL = str(load_config(REPO_ROOT, env={}).settings.site.base_url)
FEEDS = {
    "en": ("README.md", "### Calendar feeds"),
    "zh-CN": ("README.zh-CN.md", "### \u65e5\u5386\u8ba2\u9605"),
}
DIGEST = {
    "en": ("README.md", "### Weekly digest", f"the deadlines of the {DEADLINE_DAYS} days"),
    "zh-CN": (
        "README.zh-CN.md",
        "### \u6bcf\u5468\u6458\u8981",
        f"{DEADLINE_DAYS} \u5929\u5185\u7684\u622a\u6b62\u65e5\u671f",
    ),
}
MAIN_TRACKS = {
    "en": "main tracks of machine-learning and computer-science conferences",
    "zh-CN": "\u673a\u5668\u5b66\u4e60\u548c\u8ba1\u7b97\u673a\u79d1\u5b66\u4f1a\u8bae\u4e3b\u4f1a",
}
"""The policy of the README: no main track of a machine-learning or computer-science
conference, in the words the README had before deadlines.ics (ML and CS alike)."""
ARCHITECTURE = {
    "docs/ARCHITECTURE.md": (
        f"of the {DEADLINE_DAYS} days from the Monday",
        f"a fixed limit of {DEADLINE_ROWS} rows",
    ),
    "docs/ARCHITECTURE.zh-CN.md": (
        f"{DEADLINE_DAYS} \u5929\u5185\u7684\u622a\u6b62\u65e5\u671f",
        f"\u6700\u591a {DEADLINE_ROWS} \u884c",
    ),
}
GUIDES = {
    "docs/ADDING_A_SOURCE.md": f"at most {MAX_SHORT_TITLE} characters",
    "docs/ADDING_A_SOURCE.zh-CN.md": f"\u6700\u591a {MAX_SHORT_TITLE} \u4e2a\u5b57\u7b26",
}


@pytest.mark.parametrize("lang", FEEDS)
def test_the_readme_links_the_feed_of_the_deadlines_at_its_published_address(lang: str) -> None:
    name = FEEDS[lang][0]
    assert f"[deadlines.ics]({BASE_URL}feeds/deadlines.ics)" in section(*FEEDS[lang])
    webcal = f"\nwebcal://{BASE_URL.removeprefix('https://')}feeds/deadlines.ics\n"
    assert webcal in text(name)


@pytest.mark.parametrize("lang", SUBSCRIBE)
def test_the_main_tracks_of_machine_learning_conferences_stay_with_ccfddl(lang: str) -> None:
    body = section(*SUBSCRIBE[lang])
    assert "`deadlines.ics`" in body
    assert MAIN_TRACKS[lang] in body


@pytest.mark.parametrize("lang", DIGEST)
def test_the_readme_quotes_the_days_of_the_deadlines_in_the_digest(lang: str) -> None:
    name, heading, phrase = DIGEST[lang]
    assert phrase in section(name, heading)


@pytest.mark.parametrize("path", ARCHITECTURE)
def test_the_architecture_quotes_the_days_and_the_rows_of_the_digest(path: str) -> None:
    prose = " ".join(text(path).split())
    assert [phrase for phrase in ARCHITECTURE[path] if phrase not in prose] == []


@pytest.mark.parametrize("path", GUIDES)
def test_the_guide_names_the_key_and_the_rules_of_a_milestone(path: str) -> None:
    guide = text(path)
    assert GUIDES[path] in " ".join(guide.split())
    for code in ("`short_title`", "`deadlines.ics`", "`tests/test_custom_registry_deadlines.py`"):
        assert code in guide, code
