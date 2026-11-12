"""Splitting a raw speaker line into people.

Sources write speakers in many ways::

    Speaker: Jane Doe (MIT)
    Jane Doe, Stanford University
    Prof. Jane Doe, PhD, Department of Chemistry, MIT
    Jane Doe (MIT) and John Roe (Stanford University)
    Jane Doe, John Roe, and Mary Major

A comma can separate two people or a person from an affiliation, and rules
cannot always tell which. The choice made here is conservative:

0. A line that starts with a numbered label ("Speaker 1 (1:30 PM - 2:05
   PM) - ", "Speaker 2:") names the people of each label in the text up
   to the next label: the name, then after the first comma the
   affiliation. So the schedule of a session, whose paragraphs often run
   together ("...Cardiology Speaker 2 (2:10 PM) - Emily Fox, Professor,
   Statistics and Computer Science"), gives one speaker per label and
   never a "Computer Science". Only before the first comma do ``;``,
   ``|``, a line break, "and" and ``&`` separate people, and only when
   every part reads like a person: "Speaker 1: Jane Doe and John Roe,
   MIT" names two, and the affiliation goes to the last, as in 3.
1. A leading label ("Speaker:", "Presented by:") is dropped, and so is a
   part that is nothing but a label ("Contact:").
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
_NUMBERED_LABEL: Final = re.compile(
    r"(?:\b|(?-i:(?<=[a-z0-9])(?=[A-Z][a-z])))"
    r"(?:speaker|presenter|panelist|lecturer)\s{0,3}#?\d{1,2}\s{0,3}(?:\([^()\n]{0,60}\))?"
    r"\s{0,3}[:\-\u2013\u2014]\s*",
    re.IGNORECASE,
)
"""A numbered label in front of the speakers of one slot.

It may follow a bracket or a stop without a space ("(OTL)Speaker 2"), and
a word when it starts with a capital ("GeophysicsSpeaker 2", where two
paragraphs ran together), but it is never the lower-case end of a word
("loudspeaker 2").
"""
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
    "centre chemistry clinic cnrs college columbia company computer cornell corp corporation "
    "csail data deepmind department dept division ecole engineering epfl eth faculty flatiron "
    "foundation genentech google group harvard hhmi hopkins hospital ibm inc inria institut "
    "institute institution intel janelia kaist lab laboratories laboratory labs llc ltd "
    "mathematics medicine mellon merck meta microsoft mit national nih novartis nvidia openai "
    "oxford pfizer physics polytechnic polytechnique princeton program research riken salk "
    "school science sciences scripps simons slac society stanford state statistics team tech "
    "technion uc ucb ucla ucsd ucsf universidad universitat universite university weizmann "
    "yale"
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


def _numbered(text: str) -> list[str] | None:
    """The parts after each numbered label, when ``text`` starts with one; else ``None``."""
    labels = list(_NUMBERED_LABEL.finditer(text))
    if not labels or text[: labels[0].start()].strip():
        return None
    ends = [label.start() for label in labels[1:]] + [len(text)]
    pairs = zip(labels, ends, strict=True)
    parts = (text[label.end() : end].strip(" \n,;") for label, end in pairs)
    return [part for part in parts if part]


def _numbered_people(part: str) -> list[ParsedSpeaker]:
    """The people after one numbered label (see rule 0 of the module docstring)."""
    comma = _mask_brackets(part).find(",")
    head, rest = (part, "") if comma < 0 else (part[:comma], part[comma:])
    names = _split(head, _HARD_SEPARATOR)
    if len(names) < 2 or not all(_is_person(name) for name in names):
        names = [part]
    else:
        names = [*names[:-1], names[-1] + rest]
    people = (_person_with_commas(name) for name in names)
    return [person for person in people if person is not None]


def split_speakers(text: str) -> list[ParsedSpeaker]:
    """The people named in a raw speaker line, in order; empty when there is no name."""
    numbered = _numbered(text.strip())
    if numbered is not None:
        return [person for part in numbered for person in _numbered_people(part)]
    line = _LABEL.sub("", text.strip())
    parts = [part for part in _split(line, _ANY_SEPARATOR) if not is_degree(part)]
    if len(parts) > 1 and all(_is_person(part) for part in parts):
        people = [_person(part) for part in parts]
        return [person for person in people if person is not None]
    speakers: list[ParsedSpeaker] = []
    for chunk in _split(line, _HARD_SEPARATOR):
        person = None if chunk.endswith(":") else _person_with_commas(chunk)
        if person is None:
            continue
        if speakers and not looks_like_name(person.name):
            previous = speakers[-1]
            joined = f"{previous.affiliation} and {chunk}" if previous.affiliation else chunk
            speakers[-1] = ParsedSpeaker(name=previous.name, affiliation=joined)
        else:
            speakers.append(person)
    return speakers
