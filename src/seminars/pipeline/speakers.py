"""Splitting a raw speaker line into people.

Sources write speakers in many ways::

    Speaker: Jane Doe (MIT)
    Jane Doe, Stanford University
    Prof. Jane Doe, PhD, Department of Chemistry, MIT
    Jane Doe (MIT) and John Roe (Stanford University)
    Jane Doe, John Roe, and Mary Major

A comma can separate two people or a person from an affiliation, and rules
cannot always tell which. The choice made here is conservative:

1. A leading label ("Speaker:", "Presented by:") is dropped.
2. When every part between separators (``;``, ``|``, a line break, "and",
   ``&`` and the comma) reads like a person, optionally followed by an
   affiliation in parentheses, the line is a list of people.
3. Otherwise only ``;``, ``|``, a line break, "and" and ``&`` separate
   people, and within each part the text after the first comma is the
   affiliation. A part that does not start with a person's name (the second
   half of "Department of Chemistry and Chemical Biology") belongs to the
   affiliation before it.

"Reads like a person" is decided by ``looks_like_name``: two to six words
that start with a capital letter (name particles such as "van" and "de"
excepted), no digits, and none of the words that mark an organisation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from seminars.pipeline.text import fold, is_degree, tidy_name, word_set

MAX_NAME_LENGTH: Final = 80

_LABEL: Final = re.compile(
    r"^\s*(?:(?:guest|invited|keynote|featured)\s+)?"
    r"(?:speakers?|presenters?|presented\s+by|lecturers?|panelists?|featuring|with|by)"
    r"\s*[:\-]\s*",
    re.IGNORECASE,
)
_HARD_SEPARATOR: Final = re.compile(r"[;|\n]|\s+(?:and|&)\s+|,\s+(?:and|&)\s+", re.IGNORECASE)
_ANY_SEPARATOR: Final = re.compile(r"[;|\n,]|\s+(?:and|&)\s+", re.IGNORECASE)
_COMMA: Final = re.compile(",")
_WITH_AFFILIATION: Final = re.compile(r"^(?P<name>[^()\[\]]*?)\s*[(\[](?P<affiliation>.+)[)\]]\s*$")
_NAME_WORD: Final = re.compile(r"[^\W\d_](?:[^\W\d_]|['\u2019.\-])*")
_PARTICLES: Final = word_set(
    "af al bin da das de del della den der di do dos du e el ibn la le ten ter van von y zu"
)
_ORGANISATION_WORDS: Final = word_set(
    "academy amazon anthropic apple berkeley biohub biology broad caltech cambridge center "
    "centre chemistry clinic cnrs college columbia company cornell corp corporation csail "
    "deepmind department dept division ecole engineering epfl eth faculty flatiron foundation "
    "genentech google group harvard hhmi hopkins hospital ibm inc inria institut institute "
    "institution intel janelia kaist lab laboratories laboratory labs llc ltd mathematics "
    "medicine mellon merck meta microsoft mit national nih novartis nvidia openai oxford pfizer "
    "physics polytechnic polytechnique princeton program research riken salk school sciences "
    "scripps simons slac society stanford state statistics team tech technion uc ucb ucla ucsd "
    "ucsf universidad universitat universite university weizmann yale"
)


@dataclass(frozen=True)
class ParsedSpeaker:
    """One person found in a speaker line."""

    name: str
    affiliation: str | None = None


def _mask_brackets(text: str) -> str:
    """``text`` with everything inside brackets replaced, so separators there do not count."""
    masked: list[str] = []
    depth = 0
    for char in text:
        if char in "([":
            depth += 1
            masked.append(char)
        elif char in ")]":
            depth = max(depth - 1, 0)
            masked.append(char)
        else:
            masked.append("x" if depth else char)
    return "".join(masked)


def _split(text: str, separator: re.Pattern[str]) -> list[str]:
    """Split ``text`` at ``separator`` outside brackets; empty parts are dropped."""
    parts: list[str] = []
    position = 0
    for match in separator.finditer(_mask_brackets(text)):
        parts.append(text[position : match.start()])
        position = match.end()
    parts.append(text[position:])
    return [part.strip() for part in parts if part.strip()]


def looks_like_name(text: str) -> bool:
    """Whether ``text`` reads like the name of a person (see the module docstring).

    Text without any Latin letter (a name in another script) counts as a
    name when it is short and has no digit.
    """
    name = tidy_name(text)
    if not name or len(name) > MAX_NAME_LENGTH or any(char.isdigit() for char in name):
        return False
    if "@" in name or "://" in name:
        return False
    if not any(char.isascii() and char.isalpha() for char in name):
        return len(name) <= 20
    words = name.split()
    if not 2 <= len(words) <= 6:
        return False
    if any(word in _ORGANISATION_WORDS for word in fold(name).split()):
        return False
    return all(_is_name_word(word) for word in words)


def _is_name_word(word: str) -> bool:
    if word.casefold().rstrip(".") in _PARTICLES:
        return True
    return _NAME_WORD.fullmatch(word) is not None and word[0].isupper()


def _person(text: str) -> ParsedSpeaker | None:
    """``"Jane Doe (MIT)"`` or ``"Jane Doe"`` as a speaker; ``None`` without a name."""
    match = _WITH_AFFILIATION.match(text)
    name = tidy_name(match.group("name") if match else text)
    affiliation = match.group("affiliation").strip() if match else None
    return ParsedSpeaker(name=name, affiliation=affiliation or None) if name else None


def _person_with_commas(text: str) -> ParsedSpeaker | None:
    """``"Jane Doe, PhD, Department of Chemistry, MIT"``: name first, then the affiliation."""
    first, *rest = _split(text, _COMMA) or [""]
    person = _person(first)
    if person is None:
        return None
    extra = ", ".join(part for part in rest if not is_degree(part))
    affiliation = ", ".join(part for part in (person.affiliation, extra) if part)
    return ParsedSpeaker(name=person.name, affiliation=affiliation or None)


def _is_person(text: str) -> bool:
    match = _WITH_AFFILIATION.match(text)
    return looks_like_name(match.group("name") if match else text)


def split_speakers(text: str) -> list[ParsedSpeaker]:
    """The people named in a raw speaker line, in order; empty when there is no name."""
    line = _LABEL.sub("", text.strip())
    parts = [part for part in _split(line, _ANY_SEPARATOR) if not is_degree(part)]
    if len(parts) > 1 and all(_is_person(part) for part in parts):
        people = [_person(part) for part in parts]
        return [person for person in people if person is not None]
    speakers: list[ParsedSpeaker] = []
    for chunk in _split(line, _HARD_SEPARATOR):
        person = _person_with_commas(chunk)
        if person is None:
            continue
        if speakers and not looks_like_name(person.name):
            previous = speakers[-1]
            joined = f"{previous.affiliation} and {chunk}" if previous.affiliation else chunk
            speakers[-1] = ParsedSpeaker(name=previous.name, affiliation=joined)
        else:
            speakers.append(person)
    return speakers
