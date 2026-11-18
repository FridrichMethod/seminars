"""The README points readers of conference deadlines to a calendar that is not collected here."""

from __future__ import annotations

import pytest

from test_docs_scope import section

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
