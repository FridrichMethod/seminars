"""Properties of the privacy rules: idempotence, arbitrary input and linear time.

The rules themselves are tested in tests/test_pipeline_privacy.py.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Final

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


HOSTILE_TEXTS: Final[list[Callable[[int], str]]] = [
    lambda k: "a" * 50_000 * k,
    lambda k: "a." * 25_000 * k,
    lambda k: "1 " * 25_000 * k,
    lambda k: "+1" * 25_000 * k,
    lambda k: "+" + "1" * 50_000 * k,
    lambda k: "x@" * 25_000 * k,
    lambda k: "@a." * 17_500 * k,
    lambda k: "passcode " * 6_250 * k,
    lambda k: "pwd:" + "1" * 50_000 * k + "a",
    lambda k: "(" * 50_000 * k,
    lambda k: "https://" + "a." * 25_000 * k,
    lambda k: "a [at] b [dot] " * 3_750 * k,
    lambda k: "Meeting ID: " + "1 " * 25_000 * k,
    lambda k: "call " * 10_000 * k,
    lambda k: "passcode" + " " * 50_000 * k + "y",
    lambda k: "password is" + " " * 50_000 * k + "y",
    lambda k: "pin" + " " * 25_000 * k + "(" + " " * 25_000 * k + "y",
    lambda k: "phone" + " " * 50_000 * k + "y",
    lambda k: "meeting id" + " " * 50_000 * k + "1",
    lambda k: "650 723 4321" + "," * 50_000 * k,
    lambda k: "650 723 4321 ext " * 3_000 * k,
    lambda k: "a at b dot " * 5_000 * k,
    lambda k: "x at " + "b dot " * 10_000 * k,
    lambda k: "passcode: " + '"' * 50_000 * k,
    lambda k: "passcode: (" + "1" * 50_000 * k,
    lambda k: "pin: " + "1 " * 25_000 * k,
    lambda k: "passcode for the meeting is" + " " * 50_000 * k + "1",
    lambda k: "6507234321" * 5_000 * k,
    lambda k: "cell: " * 7_500 * k,
    lambda k: "https://urldefense.com/v2/url?u=" + "-3F_" * 12_500 * k,
    lambda k: "https://www.google.com/url?q=" + "%25" * 15_000 * k,
    lambda k: "Phone: [phone removed], " * 5_000 * k,
    lambda k: "(" * 25_000 * k + "[removed]" + ")" * 25_000 * k,
    lambda k: ", ;" * 15_000 * k + "[email removed]",
    lambda k: "password: " + "a" * 50_000 * k + " b",
    lambda k: "password: a " * 5_000 * k,
    lambda k: "use password " * 5_000 * k,
    lambda k: "passcode (a b " * 3_750 * k,
    lambda k: "contact " + "x " * 25_000 * k + "at 723-4321",
    lambda k: "contact " * 7_500 * k,
    lambda k: "e-mail " * 7_500 * k,
    lambda k: "a.b at " * 7_500 * k,
    lambda k: "x @ " * 12_500 * k,
    lambda k: "@group.calendar.google.com" * 2_000 * k,
    lambda k: "contact jdoe at " + "a." * 25_000 * k,
]
"""Texts that make a careless rule backtrack; ``k`` = 4 gives about 200,000 characters."""

LENGTH_FACTOR: Final = 4
"""How many times longer the long text is than the short one."""
ROUNDS: Final = 5
"""How often the two lengths are timed at most; the best time of each counts."""
MAX_RATIO: Final = 8.0
"""Four times the text takes four times the time when scrubbing is linear, 16 when quadratic."""
CEILING_SECONDS: Final = 30.0
"""For the longer text: far above what a loaded machine needs, so it never fails by load."""


def best_times(
    work: Callable[[str], object],
    build: Callable[[int], str],
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[float, float]:
    """The best time of ``work`` on ``build(1)`` and on ``build(LENGTH_FACTOR)``.

    The two lengths take turns, so that a burst of load slows both, and the best of the
    rounds is what the machine can do. Timing stops after the first round that is clear:
    the longer text takes less than ``MAX_RATIO`` times the shorter one, or is over the
    ceiling.
    """
    texts = (build(1), build(LENGTH_FACTOR))
    best = [math.inf, math.inf]
    for _ in range(ROUNDS):
        for index, text in enumerate(texts):
            started = clock()
            work(text)
            best[index] = min(best[index], clock() - started)
        if best[1] < MAX_RATIO * best[0] or best[1] >= CEILING_SECONDS:
            break
    return best[0], best[1]


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

    @pytest.mark.parametrize("build", HOSTILE_TEXTS, ids=lambda build: repr(build(1)[:12]))
    def test_large_hostile_text_is_scrubbed_in_linear_time(
        self, build: Callable[[int], str]
    ) -> None:
        """Four times the text takes about four times the time, idle or busy.

        A budget in seconds depends on the load of the machine: the slowest of these
        texts takes about 0.5 s at a load average of 6 and up to 1.5 s at one of 21. The
        ratio of the times for two lengths does not depend on it, and it tells a linear
        scrubber (4) from a quadratic one (16) with a wide margin on both sides of the
        threshold (8). When a share ``f`` of the time on the shorter text grows with the
        square of the length, the ratio is 4 + 12 f, so the test fails a scrubber that
        is quadratic in a third of that time or more. The ceiling only stops a scrubber
        that is far too slow at any load.
        """
        short, long = best_times(lambda text: strip_markers(scrub(text)), build)
        assert long < CEILING_SECONDS
        assert long < MAX_RATIO * short


class TestTheTiming:
    """``best_times`` with a clock that only the work moves, by a cost of the length."""

    def times(self, cost: Callable[[int, int], float]) -> tuple[tuple[float, float], int]:
        """The best times for the cost ``cost(length, call)``, and the number of calls."""
        clock = [0.0]
        calls: list[int] = []

        def work(text: str) -> None:
            calls.append(len(text))
            clock[0] += cost(len(text), len(calls))

        return best_times(work, lambda k: "x" * 1_000 * k, clock=lambda: clock[0]), len(calls)

    def test_a_linear_scrubber_passes_after_one_round(self) -> None:
        (short, long), calls = self.times(lambda length, _call: length * 1e-6)
        assert (short, long, calls) == (pytest.approx(1e-3), pytest.approx(4e-3), 2)
        assert long < MAX_RATIO * short

    def test_a_quadratic_scrubber_fails_in_every_round(self) -> None:
        (short, long), calls = self.times(lambda length, _call: length**2 * 1e-9)
        assert long >= MAX_RATIO * short
        assert calls == 2 * ROUNDS

    def test_a_scrubber_quadratic_in_half_of_its_time_fails_in_every_round(self) -> None:
        (short, long), calls = self.times(lambda length, _call: length * 1e-6 + length**2 * 1e-9)
        assert (short, long) == (pytest.approx(2e-3), pytest.approx(20e-3))
        assert long >= MAX_RATIO * short
        assert calls == 2 * ROUNDS

    def test_a_burst_of_load_on_the_longer_text_is_timed_again(self) -> None:
        def cost(length: int, call: int) -> float:
            return length * 1e-6 * (10 if call == 2 else 1)

        (short, long), calls = self.times(cost)
        assert long < MAX_RATIO * short
        assert calls == 4

    def test_a_run_over_the_ceiling_is_not_repeated(self) -> None:
        (_short, long), calls = self.times(lambda length, _call: length**2 * 1e-4)
        assert long >= CEILING_SECONDS
        assert calls == 2
