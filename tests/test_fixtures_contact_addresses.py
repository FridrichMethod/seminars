"""Recordings hold no personal e-mail address.

The fixtures are committed to a public repository, so the address of an
individual is replaced before a recording is committed and the README of the
fixture directory says so (tests/fixtures/README.md). An address in a
recording is one of:

* a placeholder: the local part is ``redacted`` (``redacted@stanford.edu``);
* an address on an example domain (``someone@example.edu``);
* the id of a public Google Calendar (``...@group.calendar.google.com``);
* a shared or role address of a department, center or series, listed in
  ``SHARED_ADDRESSES``. A new one is added there by name, never by pattern.

Addresses are looked for after decoding the forms in which pages write the
at sign: JSON escapes (``\\u0040``), HTML character references (``&#64;``)
and percent-encoding (``%40``); the folded lines of an ICS file are joined
first. Image names for dense screens (``logo@2x.png``) are no addresses.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Final
from urllib.parse import unquote

import pytest

FIXTURES: Final = Path(__file__).resolve().parent / "fixtures"
RECORDING_SUFFIXES: Final = frozenset({".html", ".json", ".xml", ".ics"})
RECORDINGS: Final = sorted(
    path
    for path in FIXTURES.rglob("*")
    if path.is_file() and path.suffix in RECORDING_SUFFIXES and path.name != "manifest.json"
)

SHARED_ADDRESSES: Final = frozenset(
    {
        "aimicenter@stanford.edu",
        "datascience@stanford.edu",
        "dbds-admissions@stanford.edu",
        "dbds-operations@stanford.edu",
        "info@ee.stanford.edu",
        "nlp-seminar@lists.stanford.edu",
        "physicsoflife@czbiohub.org",
        "qfarm-contact@stanford.edu",
        "stanford-hai@stanford.edu",
        "stanfordbiochemistry@stanford.edu",
        "syntheticbiology@stanford.edu",
        "thseminar@cs.stanford.edu",
    }
)
"""Shared and role addresses that public pages show; they stay in the recordings."""

PLACEHOLDER_LOCAL_PART: Final = "redacted"
SHARED_CALENDAR_DOMAIN: Final = "group.calendar.google.com"
EXAMPLE_DOMAIN: Final = re.compile(
    r"(?:[a-z0-9-]+\.)*example\.(?:com|edu|net|org)|(?:[a-z0-9-]+\.)*(?:example|invalid|test)"
)

ADDRESS: Final = re.compile(
    r"[A-Za-z0-9._%+-]+@(?![0-9.]+x\.)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)
_JSON_ESCAPE: Final = re.compile(r"\\u([0-9a-fA-F]{4})")
_ICS_FOLD: Final = re.compile(r"\r?\n[ \t]")


def decoded(text: str, *, ics: bool = False) -> str:
    """``text`` with folded ICS lines joined and the encoded forms of characters decoded."""
    if ics:
        text = _ICS_FOLD.sub("", text)
    text = _JSON_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), text)
    return unquote(html.unescape(text))


def addresses(text: str, *, ics: bool = False) -> set[str]:
    """Every e-mail address in ``text``, in lower case."""
    return {match.lower() for match in ADDRESS.findall(decoded(text, ics=ics))}


def may_stay(address: str) -> bool:
    local, _, domain = address.rpartition("@")
    return (
        local == PLACEHOLDER_LOCAL_PART
        or EXAMPLE_DOMAIN.fullmatch(domain) is not None
        or domain == SHARED_CALENDAR_DOMAIN
        or address in SHARED_ADDRESSES
    )


def test_there_are_recordings() -> None:
    assert len(RECORDINGS) > 50
    assert {path.suffix for path in RECORDINGS} == RECORDING_SUFFIXES


@pytest.mark.parametrize("path", RECORDINGS, ids=lambda path: str(path.relative_to(FIXTURES)))
def test_a_recording_holds_no_personal_address(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    personal = sorted(
        found for found in addresses(text, ics=path.suffix == ".ics") if not may_stay(found)
    )
    assert personal == []


class TestTheScan:
    """The scan finds an address in every form a page writes it, and nothing else."""

    @pytest.mark.parametrize(
        "text",
        [
            '<a href="mailto:Jane.Doe@MIT.EDU">Jane</a>',
            "https://meche.mit.edu/people/faculty/jane.doe@mit.edu",
            '{"html": "\\u003cp\\u003ejane.doe\\u0040mit.edu\\u003c/p\\u003e"}',
            "jane.doe&#64;mit.edu",
            "mailto:jane.doe%40mit.edu",
            "ATTENDEE:mailto:jane.doe@mi\n t.edu",
        ],
    )
    def test_a_personal_address_is_found_in_every_form(self, text: str) -> None:
        found = addresses(text, ics=True)
        assert found == {"jane.doe@mit.edu"}
        assert not any(may_stay(address) for address in found)

    @pytest.mark.parametrize(
        "address",
        [
            "redacted@stanford.edu",
            "REDACTED@mit.edu",
            "someone@example.edu",
            "someone@events.example.org",
            "abc123@group.calendar.google.com",
            "syntheticbiology@stanford.edu",
        ],
    )
    def test_placeholders_examples_calendars_and_shared_addresses_may_stay(
        self, address: str
    ) -> None:
        (found,) = addresses(f"<p>{address}</p>")
        assert may_stay(found)

    @pytest.mark.parametrize(
        "address",
        [
            "redacted.jane@mit.edu",
            "jane@example.edu.attacker.org",
            "jane@calendar.google.com",
            "syntheticbiology@stanford.edu.example.net.attacker.org",
        ],
    )
    def test_look_alikes_may_not_stay(self, address: str) -> None:
        (found,) = addresses(address)
        assert not may_stay(found)

    def test_image_names_for_dense_screens_are_no_addresses(self) -> None:
        assert addresses('<img srcset="/logo@2x.png 2x, /logo@1.5x.webp">') == set()

    def test_json_escapes_before_a_placeholder_leave_it_a_placeholder(self) -> None:
        assert addresses('"\\u003eredacted@stanford.edu\\u003c"') == {"redacted@stanford.edu"}


def test_the_synbio_readme_names_the_replaced_address() -> None:
    readme = (FIXTURES / "custom-synbio-events" / "README.md").read_text(encoding="utf-8")
    assert "`redacted@mit.edu`" in readme
