"""Grounding: does a value the model wrote occur in the text it was given?

Free text from an LLM is only taken when the source text has it. A text
value must be a part of the source after normalization, or come close (see
``Grounding.text``); dates and times are numbers that must show up in the
source as well. The checks do not depend on the model behaving, so a page
that tells the model to write something else gets nothing past them.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from seminars.textrules import clean_text

MIN_FUZZY_CHARS: Final = 8
"""Values shorter than this must occur in the page exactly."""

MAX_FUZZY_EDITS: Final = 10
"""Most characters a value may differ from the page, however long it is."""

_MONTHS: Final = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)
_HOUR_WORDS: Final = {0: "midnight", 12: "noon"}
_CANCEL_WORDS: Final = ("cancel", "postpone")
_TYPOGRAPHY: Final = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u2032": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u2033": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u2026": "...",
    }
)


def normalize_for_grounding(text: str) -> str:
    """``text`` in the form in which a value and the page are compared.

    Compatibility forms, typographic quotes and dashes, case and the amount
    of whitespace do not count; invisible characters are dropped. Nothing
    else is changed, so a value only matches text that is really there.
    """
    plain = unicodedata.normalize("NFKC", clean_text(text)).translate(_TYPOGRAPHY)
    return " ".join(plain.casefold().split())


class Grounding:
    """Decides whether what the model answered occurs in one piece of page text."""

    def __init__(self, text: str, *, min_similarity: int) -> None:
        self._text = normalize_for_grounding(text)
        self._min_similarity = min_similarity
        self._numbers = frozenset(int(number) for number in re.findall(r"\d{1,4}", self._text))

    def text(self, value: str | None) -> str | None:
        """``value`` tidied when the page has it; ``None`` when it does not.

        The value must be a part of the page text after normalization, or
        come close: at least ``min_similarity`` percent alike and at most
        ``MAX_FUZZY_EDITS`` characters apart. Short values must be exact.
        """
        tidy = " ".join((value or "").split())
        wanted = normalize_for_grounding(tidy)
        if not wanted:
            return None
        if wanted in self._text:
            return tidy
        if len(wanted) < MIN_FUZZY_CHARS:
            return None
        found = fuzz.partial_ratio_alignment(wanted, self._text)
        if found is None or found.score < self._min_similarity:
            return None
        window = self._text[found.dest_start : found.dest_end]
        allowed = min(MAX_FUZZY_EDITS, len(wanted) * (100 - self._min_similarity) // 100)
        return tidy if Levenshtein.distance(wanted, window) <= allowed else None

    def number(self, value: int) -> bool:
        """Whether ``value`` stands in the page as a number of its own."""
        return value in self._numbers

    def day(self, month: int, day: int) -> bool:
        """Whether the page shows this day: the day number, and the month by name or number."""
        if not self.number(day):
            return False
        return _MONTHS[month - 1] in self._text or self.number(month)

    def year(self, year: int) -> bool:
        return self.number(year) or self.number(year % 100)

    def hour(self, hour: int) -> bool:
        """Whether the page shows this hour, on the 12-hour or the 24-hour clock or as a word."""
        if self.number(hour) or self.number(hour % 12 or 12):
            return True
        return _HOUR_WORDS.get(hour, "\x00") in self._text

    def clock(self, hour: int, minute: int | None) -> bool:
        """Whether the page shows this time of day: the hour, and the minutes unless 0."""
        return self.hour(hour) and (not minute or self.number(minute))

    def cancellation(self) -> bool:
        """Whether the page says anywhere that something is cancelled or postponed."""
        return any(word in self._text for word in _CANCEL_WORDS)
