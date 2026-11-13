"""Recorded pages hold no session-like value that the server computed for the request.

A WordPress nonce (``"nonce":"a1b2c3d4e5"`` in the script data of a page) and a
Drupal contextual token (``data-contextual-token="..."``) are replaced by
``REDACTED`` before a recording is committed (tests/fixtures/README.md), and the
README of the fixture directory says so.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SESSION_VALUES = (
    re.compile(r'"nonce"\s*:\s*"(?!REDACTED")[0-9a-f]{6,}"'),
    re.compile(r'data-contextual-token="(?!REDACTED")[^"]+"'),
)
RECORDINGS = sorted(
    path
    for path in FIXTURES.rglob("*")
    if path.is_file() and path.suffix in {".html", ".json", ".xml"} and path.name != "manifest.json"
)


def test_there_are_recordings() -> None:
    assert len(RECORDINGS) > 50


@pytest.mark.parametrize("path", RECORDINGS, ids=lambda path: str(path.relative_to(FIXTURES)))
def test_a_recording_holds_no_session_value(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    found = [match[0][:40] for pattern in SESSION_VALUES for match in pattern.finditer(text)]
    assert found == []


def test_the_redacted_biohub_nonce_is_named_in_its_readme() -> None:
    readme = (FIXTURES / "bayarea-biohub" / "README.md").read_text(encoding="utf-8")
    assert '`"nonce":"..."`' in readme
    assert "`REDACTED`" in readme
