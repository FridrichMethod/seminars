"""Grounding: does a value the model wrote occur in the text it was given?

Free text from an LLM is only taken when the source text has it. A text
value must be a part of the source after normalization, or come close (see
``Grounding.text``), and what is kept is the text of the source, never the
words of the model; dates and times are numbers that must show up in the
source as well. The checks do not depend on the model behaving, so a page
that tells the model to write something else gets nothing past them.

Link markers (``[[n]]``, see ``seminars.sources.generic.llm_page``) are not
text of the page: they are left out of the comparison and out of every
value that is kept.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from seminars.textrules import clean_text

MIN_FUZZY_CHARS: Final = 8
"""Values shorter than this must occur in the page exactly."""

MAX_FUZZY_EDITS: Final = 10
"""Most characters a value may differ from the page, however long it is."""

MAX_EXACT_PLACES: Final = 50
"""Places of the page compared when a value occurs there more than once."""

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
_LINK_MARKER: Final = re.compile(r"\[\[[0-9]+\]\]")
"""A link marker in page text; a page cannot write one itself."""

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


@dataclass(frozen=True)
class _Normalized:
    """Normalized text and where each of its characters comes from.

    ``text[i]`` stems from ``source[starts[i]:ends[i]]``: one character of
    the source with the combining marks that follow it, which together can
    give one character or several (a ligature, a letter that folds to two).
    """

    text: str
    starts: tuple[int, ...]
    ends: tuple[int, ...]


def _fold(piece: str) -> str:
    """One character of the source, with its combining marks, as it is compared."""
    if len(piece) == 1 and " " <= piece <= "~":
        return piece.lower()
    plain = unicodedata.normalize("NFKC", clean_text(piece)).translate(_TYPOGRAPHY)
    return plain.casefold()


def _pieces(text: str) -> Iterator[tuple[int, int]]:
    """Spans of ``text``: every character with the combining marks after it; markers whole."""
    index = 0
    while index < len(text):
        marker = _LINK_MARKER.match(text, index) if text[index] == "[" else None
        end = marker.end() if marker else index + 1
        while marker is None and end < len(text) and unicodedata.combining(text[end]):
            end += 1
        yield index, end
        index = end


def _normalized(text: str) -> _Normalized:
    """``text`` normalized as ``normalize_for_grounding`` says, with the origin of each character.

    Normalizing piece by piece gives the same result for the value and for
    the page, so both are compared on equal terms.
    """
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    space = False
    for start, end in _pieces(text):
        piece = text[start:end]
        folded = " " if _LINK_MARKER.fullmatch(piece) else _fold(piece)
        for char in folded:
            if char.isspace():
                space = bool(chars)
                continue
            if space:
                chars.append(" ")
                starts.append(start)
                ends.append(start)
                space = False
            chars.append(char)
            starts.append(start)
            ends.append(end)
    return _Normalized("".join(chars), tuple(starts), tuple(ends))


def normalize_for_grounding(text: str) -> str:
    """``text`` in the form in which a value and the page are compared.

    Compatibility forms, typographic quotes and dashes, case and the amount
    of whitespace do not count; invisible characters and link markers are
    dropped. Nothing else is changed, so a value only matches text that is
    really there.
    """
    return _normalized(text).text


def _cuts_word(text: str, index: int) -> bool:
    """Whether a span that starts or ends at ``index`` cuts a word of ``text`` in two."""
    return 0 < index < len(text) and text[index - 1].isalnum() and text[index].isalnum()


class Grounding:
    """Decides whether what the model answered occurs in one piece of page text."""

    def __init__(self, text: str, *, min_similarity: int) -> None:
        self._source = text
        self._page = _normalized(text)
        self._text = self._page.text
        self._min_similarity = min_similarity
        self._numbers = frozenset(int(number) for number in re.findall(r"\d{1,4}", self._text))

    def text(self, value: str | None) -> str | None:
        """The text of the page that ``value`` stands for; ``None`` when the page lacks it.

        The value must be a part of the page text after normalization, or
        come close: at least ``min_similarity`` percent alike and at most
        ``MAX_FUZZY_EDITS`` characters apart. Short values must be exact.
        What comes back is the matching part of the page, whitespace tidied
        and link markers removed, so the model can choose which text of the
        page is taken but cannot add a single character of its own.
        """
        wanted = normalize_for_grounding(value or "")
        if not wanted:
            return None
        exact = self._exact(wanted, " ".join((value or "").split()))
        if exact is not None:
            return exact
        if len(wanted) < MIN_FUZZY_CHARS:
            return None
        alignment = fuzz.partial_ratio_alignment(wanted, self._text)
        if alignment is None or alignment.score < self._min_similarity:
            return None
        allowed = min(MAX_FUZZY_EDITS, len(wanted) * (100 - self._min_similarity) // 100)
        span = self._closest_span(wanted, alignment.dest_start, alignment.dest_end, allowed)
        return None if span is None else self._original(*span)

    def _exact(self, wanted: str, tidy: str) -> str | None:
        """The page text of the best place where ``wanted`` occurs; ``None`` when it does not.

        The text can occur several times, in other case or as part of a
        longer word. A place written exactly like the value wins, then one
        that does not cut a word, then the first; at most
        ``MAX_EXACT_PLACES`` places are looked at.
        """
        best: tuple[bool, int, int, str] | None = None
        found = self._text.find(wanted)
        for _ in range(MAX_EXACT_PLACES):
            if found < 0:
                break
            end = found + len(wanted)
            original = self._original(found, end)
            if original is not None:
                cuts = _cuts_word(self._text, found) + _cuts_word(self._text, end)
                key = (original != tidy, cuts, found, original)
                best = key if best is None or key < best else best
            found = self._text.find(wanted, found + 1)
        return None if best is None else best[3]

    def _closest_span(
        self, wanted: str, start: int, end: int, allowed: int
    ) -> tuple[int, int] | None:
        """The part of the normalized page near ``start:end`` that is closest to ``wanted``.

        The window of the fuzzy match has the length of ``wanted``; the part
        of the page that the value stands for can begin and end up to
        ``allowed`` characters away from it. Of the parts within ``allowed``
        edits, the closest wins, then one that does not cut a word, then
        the one nearest to the window. ``None`` when no part is close enough.
        """
        text = self._text
        best: tuple[int, int, int, int, int] | None = None
        for first in range(max(0, start - allowed), min(len(text), start + allowed) + 1):
            for last in range(max(first + 1, end - allowed), min(len(text), end + allowed) + 1):
                if text[first] == " " or text[last - 1] == " ":
                    continue
                distance = Levenshtein.distance(wanted, text[first:last], score_cutoff=allowed)
                if distance > allowed:
                    continue
                cuts = _cuts_word(text, first) + _cuts_word(text, last)
                key = (distance, cuts, abs(first - start) + abs(last - end), first, last)
                best = key if best is None or key < best else best
        return None if best is None else (best[3], best[4])

    def _original(self, start: int, end: int) -> str | None:
        """The page text behind ``self._text[start:end]``, tidied; ``None`` when it is empty."""
        piece = self._source[self._page.starts[start] : self._page.ends[end - 1]]
        tidy = " ".join(_LINK_MARKER.sub(" ", piece).split())
        return tidy or None

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
