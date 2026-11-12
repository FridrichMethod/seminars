"""Properties of the privacy rules: idempotence, arbitrary input and linear time.

The rules themselves are tested in tests/test_pipeline_privacy.py.
"""

from __future__ import annotations

import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from seminars.pipeline.privacy import (
    EMAIL_REMOVED,
    LINK_REMOVED,
    PHONE_REMOVED,
    VALUE_REMOVED,
    scrub_private,
    strip_markers,
)


def scrub(text: str) -> str:
    return scrub_private(text)


FRAGMENTS = [
    "Passcode",
    "password",
    "pwd",
    "PIN",
    "pass",
    ":",
    "=",
    " ",
    "\n",
    "is",
    "(",
    ")",
    "[",
    "]",
    "@",
    ".",
    "edu",
    "jdoe",
    "[at]",
    "[dot]",
    "mailto:",
    "https://zoom.us/j/1",
    "?",
    "&",
    "pwd=",
    "tk=",
    "#",
    "+1",
    "650",
    "723",
    "4321",
    "-",
    ",,",
    "*",
    "Meeting ID",
    "x",
    "ext.",
    "Tel.",
    "call",
    "0",
    "00",
    "\u00e9",
    "[removed]",
    "[email removed]",
    "http://[",
    "::1",
    "%40",
    "user:pw@",
    "at",
    "dot",
    "Cell",
    "Contact:",
    "E-mail:",
    "'",
    '"',
    "\u2013",
    "\u201c",
    "%3D",
    "-3D",
    "urldefense",
    "q=https://",
    "6507234321",
    "[link removed]",
    "[phone removed]",
    "Password: ",
    "seminar",
    "Use",
    " at ",
    "stanford.edu",
    "\uff20",
    "2x.png",
    "contact",
    "Join",
    "723-4321",
    "(case sensitive)",
    "the",
    "group.calendar.google.com",
]


class TestProperties:
    @given(st.lists(st.sampled_from(FRAGMENTS), max_size=40).map("".join))
    @settings(max_examples=400, deadline=None)
    def test_scrubbing_twice_changes_nothing(self, text: str) -> None:
        once = scrub(text)
        assert scrub(once) == once

    @given(st.text(max_size=200))
    @settings(max_examples=300, deadline=None)
    def test_arbitrary_text_is_handled(self, text: str) -> None:
        once = scrub(text)
        assert scrub(once) == once

    @given(st.lists(st.sampled_from(FRAGMENTS), max_size=40).map("".join))
    @settings(max_examples=300, deadline=None)
    def test_no_marker_is_left_in_a_name_or_an_affiliation(self, text: str) -> None:
        stripped = strip_markers(scrub(text))
        for marker in (VALUE_REMOVED, EMAIL_REMOVED, PHONE_REMOVED, LINK_REMOVED):
            assert marker not in stripped

    @pytest.mark.parametrize(
        "text",
        [
            "a" * 200_000,
            "a." * 100_000,
            "1 " * 100_000,
            "+1" * 100_000,
            "+" + "1" * 200_000,
            "x@" * 100_000,
            "@a." * 70_000,
            "passcode " * 25_000,
            "pwd:" + "1" * 200_000 + "a",
            "(" * 200_000,
            "https://" + "a." * 100_000,
            "a [at] b [dot] " * 15_000,
            "Meeting ID: " + "1 " * 100_000,
            "call " * 40_000,
            "passcode" + " " * 200_000 + "y",
            "password is" + " " * 200_000 + "y",
            "pin" + " " * 100_000 + "(" + " " * 100_000 + "y",
            "phone" + " " * 200_000 + "y",
            "meeting id" + " " * 200_000 + "1",
            "650 723 4321" + "," * 200_000,
            "650 723 4321 ext " * 12_000,
            "a at b dot " * 20_000,
            "x at " + "b dot " * 40_000,
            "passcode: " + '"' * 200_000,
            "passcode: (" + "1" * 200_000,
            "pin: " + "1 " * 100_000,
            "passcode for the meeting is" + " " * 200_000 + "1",
            "6507234321" * 20_000,
            "cell: " * 30_000,
            "https://urldefense.com/v2/url?u=" + "-3F_" * 50_000,
            "https://www.google.com/url?q=" + "%25" * 60_000,
            "Phone: [phone removed], " * 20_000,
            "(" * 100_000 + "[removed]" + ")" * 100_000,
            ", ;" * 60_000 + "[email removed]",
            "password: " + "a" * 200_000 + " b",
            "password: a " * 20_000,
            "use password " * 20_000,
            "passcode (a b " * 15_000,
            "contact " + "x " * 100_000 + "at 723-4321",
            "contact " * 30_000,
            "e-mail " * 30_000,
            "a.b at " * 30_000,
            "x @ " * 50_000,
            "@group.calendar.google.com" * 8_000,
            "contact jdoe at " + "a." * 100_000,
        ],
        ids=lambda text: repr(text[:12]),
    )
    def test_large_hostile_text_is_scrubbed_in_linear_time(self, text: str) -> None:
        started = time.perf_counter()
        strip_markers(scrub(text))
        assert time.perf_counter() - started < 1.0
