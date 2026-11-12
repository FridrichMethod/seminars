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
   ``|``, a line break, "and" and ``&`` separate people, as in 3: "Speaker
   1: Jane Doe and John Roe, MIT" names two, and the affiliation goes to
   the last. A part there that does not start with a name belongs to the
   affiliation of the person before it, so "Speaker 1: Jane Doe; Stanford
   University" is Jane Doe of Stanford University.
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

Speakers in a title
-------------------

A draft without any speaker can still name one in its title; the campus
calendar has no speaker field at all. ``speakers_from_title`` reads the
people of a few common forms and nothing else::

    Geophysics Seminar - Shang-Ping Xie, "What powered the growth of ..."
    Stanford Energy Seminar | Electrified metamaterial ... | Jonathan Fan, Stanford University
    PhD Dissertation Defense: Alyssa Cutter, Bando Lab
    CSCS Community Series: "Effects of ..." with Rebecca Buchanan, PhD
    Sunshine - Bryant Lin in conversation with Latha Palaniappan
    Jonathan Kipnis - Navigating turbulent CNS waters
    Jane Doe (MIT): Knots and their invariants

A title is split at its first separator (a colon, "|", or a dash with
spaces around it). A name there must read like a person and hold none of
the words of a series ("Seminar", "Lecture", "Grand Rounds", "Defense",
...), of a placeholder ("Speaker TBA") or of a talk title ("New", "And",
"Beyond"), because "Norton Lecture" and "Quantum Computing" read like
names as well. Then the form must vouch for it:

* series first ("<series>: <name>[, <affiliation>][ - "<title>"]"): the
  part in front is a series, or the name is followed by a degree, a quoted
  title or a further part, or the abstract names the person;
* series, talk title, then the name ("<series> | <title> | <name>,
  <affiliation>"): the part in front is a series and the last part is a
  person followed by a degree or an organisation, or whom the abstract
  names;
* name first ("<name>: <title>", "<name> (<affiliation>): <title>"): the
  rest does not start with a name as well, and the affiliation in
  brackets names an organisation or the abstract names the person;
* "... with <name>" at the end: a degree follows ("with Jane Doe, PhD")
  or the abstract names the person; "<name> in conversation with <name>"
  gives both people on the same terms.

"The abstract names the person" means that the name occurs in the abstract
outside a copy of the title, so a talk title that the abstract repeats
vouches for nothing. A title that is only a name, a series name, and
everything else give no speaker.

A name that reads like a topic as well is a speaker only when the abstract
names the person, whatever the form: one that holds a word of a field, a
topic, a place or an event ("Dark Matter", "Deep Learning", "Quantum
Groups", "Monte Carlo", "Coffee Hour") or an adjective of a field
("Topological Insulators"), whose last word ends like a word of a field
("Floer Homology", "Random Matrices", "Causal Inference"), or that starts
with "A" or with a verb in "-ing" ("Navigating Heritage"). The endings
leave out the short words that names end in as well ("Lawrence",
"Clement", "Davies", "de Vries").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from seminars.pipeline.text import fold, is_degree, tidy_name, word_set

MAX_NAME_LENGTH: Final = 80

_LABEL: Final = re.compile(
    r"^\s*(?:(?:(?:guest|invited|keynote|featured)\s+)?"
    r"(?:speakers?|presenters?|presented\s+by|lecturers?|panelists?|featuring|with|by)"
    r"\s*[:\-\uff1a]"
    "|(?:\u8bb2\u8005|\u8b1b\u8005|\u4e3b\u8bb2\u4eba|\u4e3b\u8b1b\u4eba|\u62a5\u544a\u4eba"
    "|\u5831\u544a\u4eba|\u6f14\u8bb2\u8005|\u6f14\u8b1b\u8005|\u8bb2\u5e08|\u8b1b\u5e2b"
    "|\u8b1b\u6f14\u8005|\ubc1c\ud45c\uc790|\uc5f0\uc0ac)"
    r"\s*[:\uff1a])\s*",
    re.IGNORECASE,
)
"""A label in front of the speakers, in English or in Chinese, Japanese or Korean (for
"speaker", "lecturer" and "presenter"); one in CJK needs a colon."""
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
_HARD_SEPARATOR: Final = re.compile(
    r"[;|\n\uff1b\u3001]|\s+(?:and|&)\s+|,\s+(?:and|&)\s+", re.IGNORECASE
)
_ANY_SEPARATOR: Final = re.compile(r"[;|\n,\uff0c\uff1b\u3001]|\s+(?:and|&)\s+", re.IGNORECASE)
_COMMA: Final = re.compile("[,\uff0c]")
"""The comma, also the full-width one; the full-width semicolon and the ideographic comma of
CJK text are the semicolon."""
_OPENING: Final = "([\uff08"
_CLOSING: Final = ")]\uff09"
"""Brackets around an affiliation: round, square and the full-width round ones of CJK text."""
_WITH_AFFILIATION: Final = re.compile(
    r"^(?P<name>[^()\[\]\uff08\uff09]*?)\s*[(\[\uff08](?P<affiliation>.+)[)\]\uff09]\s*$"
)
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

_CJK_ORGANISATION_WORDS: Final = (
    "\u5927\u5b66",  # university (simplified Chinese, Japanese)
    "\u5927\u5b78",  # university (traditional Chinese)
    "\u5b66\u9662",  # college, school
    "\u5b78\u9662",
    "\u7814\u7a76",  # research (institute, centre)
    "\u5b9e\u9a8c\u5ba4",  # laboratory
    "\u5be6\u9a57\u5ba4",
    "\u4e2d\u5fc3",  # centre
    "\u533b\u9662",  # hospital
    "\u91ab\u9662",
    "\u516c\u53f8",  # company
    "\ub300\ud559",  # university (Korean)
    "\uc5f0\uad6c",  # research (Korean)
)
"""Words that mark an organisation in CJK text, which has no spaces between its words."""


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
        if char in _OPENING:
            depth += 1
            masked.append(char)
        elif char in _CLOSING:
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
    name when it is short, has no digit and holds none of the CJK words
    for a university, an institute or a company.
    """
    name = tidy_name(text)
    if not name or len(name) > MAX_NAME_LENGTH or any(char.isdigit() for char in name):
        return False
    if "@" in name or "://" in name:
        return False
    if not any(char.isascii() and char.isalpha() for char in name):
        return len(name) <= 20 and not any(word in name for word in _CJK_ORGANISATION_WORDS)
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


def _people_or_affiliations(chunks: list[str]) -> list[ParsedSpeaker]:
    """Rule 3 of the module docstring for parts that are already split at hard separators.

    A part that starts with a name is a person, the text after its first
    comma the affiliation; a part that does not belongs to the affiliation
    of the person before it.
    """
    speakers: list[ParsedSpeaker] = []
    for chunk in chunks:
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


def _numbered_people(part: str) -> list[ParsedSpeaker]:
    """The people after one numbered label (see rule 0 of the module docstring)."""
    comma = _COMMA.search(_mask_brackets(part))
    head, rest = (part, "") if comma is None else (part[: comma.start()], part[comma.start() :])
    pieces = _split(head, _HARD_SEPARATOR) or [""]
    return _people_or_affiliations([*pieces[:-1], pieces[-1] + rest])


_SERIES_WORDS: Final = word_set(
    "seminar seminars lecture lectures lectureship colloquium colloquia rounds defense defence"
    " series symposium symposia workshop workshops forum conference summit chat conversation"
    " conversations talk talks panel panels webinar meeting program programme course session"
    " sessions presentation presentations keynote award prize celebration retreat luncheon"
    " reception club exchange alliance updates showcase festival exhibition hackathon sprint"
    " bootcamp training orientation discussion roundtable gala ceremony"
)
"""Words of the name of a series or an event; no person's name holds one."""
_NOT_IN_A_NAME: Final = _SERIES_WORDS | word_set(
    "speaker speakers presenter tba tbd tbc title an the of and or in on for with to at from by"
    " vs via into about beyond toward towards through new how why what when is are initiative"
    " project studio circle concert recital launch book film screening movie challenge visiting"
    " student students fellowship fellowships deadline application applications info network"
    " networking mixer social we you they them our your his her its their us me shall will can"
    " may must should would could not no all more most than after before under over between"
    " against without within across around behind"
)
"""Words that rule out a name read from a title: series, placeholders, words of a title."""
_TITLE_SEPARATOR: Final = re.compile(r"\s*[:\uff1a|]\s*|\s+[-\u2013\u2014]{1,2}\s+")
_QUOTE: Final = re.compile('["\u201c\u201d]')
_WITH: Final = re.compile(r"\bwith\s+(?P<person>[^\"\u201c\u201d:|]+?)\s*$", re.IGNORECASE)
_CONVERSATION: Final = re.compile(
    r"(?P<first>[^\"\u201c\u201d]+?)\s+in\s+conversation\s+with\s+(?P<second>[^\"\u201c\u201d]+)",
    re.IGNORECASE,
)
_POSSESSIVE: Final = re.compile("[^\\W\\d_]+['\u2019]s")
"""A word in the possessive ("Nuremberg's"), which a name never holds."""
_SHOUTED: Final = re.compile(r"[^\W\d_]*(?:[A-Z]{2}|[a-z][A-Z]{2})[^\W\d_]*")
"""A word in capitals ("BOOK", "TNT") or with a run of them inside ("StEMI"): no name."""
_PERSON_BEFORE: Final = re.compile(
    r"(?:\b(?:dr|prof|professor|mr|mrs|ms|mx|speaker|presenter|featuring|features?|welcomes?"
    r"|by)\.?\s{0,3}:?\s{0,3})$",
    re.IGNORECASE,
)
_PERSON_AFTER: Final = re.compile(
    r"\s{0,3},?\s{0,3}(?:ph\.?\s?d|m\.?\s?d|d\.?\s?phil|sc\.?\s?d|professor|prof|associate"
    r"|assistant|director|chair|dean|principal|lecturer|investigator|scientist|researcher"
    r"|fellow|postdoc|founder|ceo|president)(?:\b|(?-i:(?=[A-Z])))"
    r"|\s{1,3}(?:received|holds|leads|joined|earned|serves|directs|completed|obtained|studies"
    r"|works|is\s{1,3}(?:a|an|the)\s{1,3}(?:\w+\s{1,3}){0,2}(?:professor|director|scientist"
    r"|researcher|fellow|student|chair|dean|lecturer|investigator|postdoc))\b"
    r"|'s\s|\u2019s\s"
    r"|\s{0,3}(?:university|institute|college|school|laborator|lab\b|center|centre|hospital"
    r"|foundation|department)",
    re.IGNORECASE,
)
"""What follows a person's name in a biography: a degree, a position, "received", the
possessive, an institution (also run together, as text taken out of HTML
often is: "Jane DoeStanford University", "Jane Doe, PhDProfessor of Biology")."""
_GERUND: Final = re.compile(r"[A-Z][a-z]{3,}ing\b")
"""A first word that is a verb in "-ing" of at least seven letters ("Navigating")."""
_TOPIC_WORDS: Final = word_set(
    "matter theory theories learning inference model models system systems networks holes"
    " chains groups processes process transport matrix graphs graph algorithm algorithms"
    " structure structures materials proteins protein genes genome genomes molecules cells"
    " neurons circuits sensors robots waves particles spaces manifolds surfaces operators"
    " numbers invariants symmetry flows fluids turbulence geometry algebra algebras calculus"
    " analysis synthesis catalysis design designs energy climate disease diseases therapy"
    " therapies vaccines cancer cancers memory behavior behaviour oceans atmosphere"
    " earthquakes pathways receptors enzymes catalysts polymers crystals insulators"
    " semiconductors superconductors lasers magnets galaxies planets exoplanets stars boson"
    " bosons quarks neutrinos photons electrons fermions qubits computers conjecture"
    " conjectures hypothesis problem problems method methods approach approaches framework"
    " frameworks datasets knots loops modules sheaves ecosystem ecosystems microbes"
    " microbiome bacteria viruses virus tumors tumours drugs"
    " computing sensing imaging modeling modelling sequencing signaling signalling folding"
    " processing programming reasoning mining editing scattering tracking planning matching"
    " sampling clustering testing screening printing manufacturing forecasting monitoring"
    " mapping coding rendering scheduling routing banking aging ageing warming"
    " quantum neural causal random dark optimal spectral nonlinear linear bayesian gaussian"
    " riemannian hamiltonian lagrangian euclidean hermitian abelian laplacian boolean"
    " markovian newtonian darwinian eulerian cartesian artificial digital global"
    " africa america americas asia europe antarctica arctic pacific atlantic mediterranean"
    " caribbean california american african asian european korean chinese japanese russian"
    " ukrainian mexican indian italian spanish british latin arab arabic jewish indigenous"
    " electric galactic healthy uncommon unreliable authoritarian documentary"
    " telescope medium markets machines effects pictures knowledge minerals century worlds"
    " magic letters hour hours quarter tour tours fair fairs salon class classes week weekend"
    " evening edition exhibit exhibits sale basics debrief makeover makeovers cancelled"
    " canceled postponed holiday holidays checks practice presents sense"
)
"""Words of a field, a topic, a place or an event that no person's name holds ("Dark
Matter", "Coffee Hour")."""
_TOPIC_PHRASES: Final = frozenset(
    {
        "monte carlo",
        "san francisco",
        "san diego",
        "san jose",
        "santa cruz",
        "santa barbara",
        "los angeles",
        "las vegas",
        "costa rica",
        "puerto rico",
        "hong kong",
        "sri lanka",
        "el nino",
        "la nina",
    }
)
"""Names of methods and places that read like a person's name as a whole."""
_TOPIC_ENDINGS: Final = re.compile(
    r"\w{2,}(?:tions?|sions?|ogy|ogies|ity|ities|isms?|ices|aries|ories|eries|tries|gies)"
    r"|\w{3,}(?:[tmnshrpdl]ics|ties|ures)|\w{4,}(?:ness|ments?)|\w{5,}(?:ences?|ances?)"
)
"""How the last word of a field ends ("Homology", "Matrices", "Inference").

The shortest such words are left out because names end so as well: "Sion",
"Ness", "Clement", "Lawrence", "Vance", and so is "-vics" (Markovics) and
"-ries" after a "v" (de Vries).
"""
_ADJECTIVE_ENDINGS: Final = re.compile(
    r"\w{3,}(?:ical|ular|etic|atic|omic)|\w{3,}(?:ional|istic|astic|ative)"
)
"""How an adjective of a field ends ("Topological", "Molecular", "Stochastic")."""
_COPY_AFTER: Final = re.compile(r"[ \t]{0,3}(?:[:|\"\u201c\u201d]|[-\u2013\u2014]{1,2}\s|$)", re.M)
"""What follows a name where the abstract repeats a part of the title: a separator, a
quote or the end of the line."""


@dataclass(frozen=True)
class _Mentions:
    """How the abstract of an event speaks of a name read from its title."""

    as_person: bool  # once at least as a person: "Dr. Jane Doe", "Jane Doe, PhD"
    as_other: bool  # never as a person, once at least as something else: "in Solid Pictures,"


@dataclass(frozen=True)
class _Candidate:
    """A person at the start of a part of a title, and whether the form vouches for it."""

    person: ParsedSpeaker
    marked: bool  # a degree, a quoted title or an organisation follows the name


def _title_name(name: str) -> bool:
    """Whether ``name``, read from a title, reads like a person and like nothing else."""
    if not looks_like_name(name):
        return False
    words = tidy_name(name).split()
    if any(_SHOUTED.fullmatch(word) or _POSSESSIVE.fullmatch(word) for word in words):
        return False
    if len(words[-1].rstrip(".")) < 2:
        return False
    return not any(word in _NOT_IN_A_NAME for word in fold(" ".join(words)).split())


def _is_series(text: str) -> bool:
    return any(word in _SERIES_WORDS for word in fold(text).split())


def _names_organisation(text: str | None) -> bool:
    """Whether an affiliation names an organisation ("MIT", "Bando Lab", "UCSF")."""
    if not text:
        return False
    if any(word in text for word in _CJK_ORGANISATION_WORDS):
        return True
    return any(word in _ORGANISATION_WORDS for word in fold(text).split())


def _mentions(name: str, abstract: str | None, title: str) -> _Mentions:
    """How ``abstract`` speaks of ``name``.

    The name is looked for as it is written, without regard to case. Copies
    of the title are taken out first, and a name followed by a separator, a
    quote or the end of its line repeats a part of the title: neither says
    what the name stands for.
    """
    person = tidy_name(name)
    if not abstract or not person:
        return _Mentions(as_person=False, as_other=False)
    copy = r"\s+".join(re.escape(word) for word in title.split())
    text = re.sub(copy, "\n", abstract, flags=re.IGNORECASE) if copy else abstract
    other = False
    for match in re.finditer(re.escape(person), text, re.IGNORECASE):
        before = text[max(match.start() - 30, 0) : match.start()]
        if _PERSON_BEFORE.search(before) or _PERSON_AFTER.match(text, match.end()):
            return _Mentions(as_person=True, as_other=False)
        other = other or _COPY_AFTER.match(text, match.end()) is None
    return _Mentions(as_person=False, as_other=other)


def _reads_like_topic(name: str) -> bool:
    """Whether a name read from a title reads like a topic as well (see the module docstring)."""
    if _GERUND.match(name) or tidy_name(name).startswith("A "):
        return True
    words = fold(name).split()
    if not words or " ".join(words) in _TOPIC_PHRASES:
        return True
    if any(word in _TOPIC_WORDS or _ADJECTIVE_ENDINGS.fullmatch(word) for word in words):
        return True
    return _TOPIC_ENDINGS.fullmatch(words[-1]) is not None


def _taken(person: ParsedSpeaker, vouched: bool, abstract: str | None, title: str) -> bool:
    """Whether a name read from ``title`` is a speaker.

    It is when the abstract names the person, or when the form of the title
    vouches for it (``vouched``) and the abstract does not speak of it as
    something else. A name that reads like a topic as well (see the module
    docstring) is far more often a talk title than a person, so for it only
    the abstract counts. That includes a name that starts with a verb in
    "-ing": "Navigating Heritage" and "Building Bridges" are titles, and
    "Channing Dungey" needs the abstract too.
    """
    said = _mentions(person.name, abstract, title)
    if said.as_person:
        return True
    return vouched and not said.as_other and not _reads_like_topic(person.name)


def _bare(text: str) -> str:
    """``text`` without an affiliation in brackets at its end."""
    match = _WITH_AFFILIATION.match(text)
    return match.group("name") if match else text


def _has_degree(text: str) -> bool:
    return any(is_degree(part) for part in _split(text, _COMMA)[1:])


def _title_person(text: str) -> ParsedSpeaker | None:
    """``"Jane Doe, PhD, MIT"`` of a title as a person whose name reads like one.

    Of the parts after the name, only degrees and the parts up to the first
    one that names no organisation or starts in lower case are kept: "Jane
    Doe, Princeton University, on her book ..." is Jane Doe of Princeton
    University.
    """
    first, *rest = _split(text.strip(" ,-\u2013\u2014"), _COMMA) or [""]
    person = _person(_LABEL.sub("", first))
    if person is None or not _title_name(person.name):
        return None
    kept: list[str] = [] if person.affiliation is None else [person.affiliation]
    for part in rest:
        if is_degree(part):
            continue
        if part[:1].islower() or not _names_organisation(part):
            break
        kept.append(part)
    return ParsedSpeaker(name=person.name, affiliation=", ".join(kept) or None)


def _leading_person(text: str) -> _Candidate | None:
    """The person a part of a title starts with: a name, an affiliation, then a title.

    Text after a comma must be a degree or name an organisation; otherwise
    the comma separates the words of a title ("Media Art, AI and the Self").
    """
    quote = _QUOTE.search(text)
    head = text[: quote.start()] if quote else text
    separator = _TITLE_SEPARATOR.search(head)
    head = (head[: separator.start()] if separator else head).strip(" ,-\u2013\u2014")
    person = _title_person(head) if head else None
    if person is None:
        return None
    parts = _split(head, _COMMA)[1:]
    if any(not is_degree(part) and not _names_organisation(part) for part in parts):
        return None
    marked = quote is not None or bool(parts)
    return _Candidate(person=person, marked=marked)


def _after_separator(title: str, abstract: str | None) -> list[ParsedSpeaker]:
    """People of the series-first and the name-first forms."""
    separator = _TITLE_SEPARATOR.search(title)
    if separator is None:
        return []
    head, tail = title[: separator.start()].strip(), title[separator.end() :].strip()
    following = _leading_person(tail)
    if _is_series(head) or (following is not None and not _title_name(_bare(head))):
        if following is None:
            return _last_part(title, abstract) if _is_series(head) else []
        vouched = _is_series(head) or following.marked
        return [following.person] if _taken(following.person, vouched, abstract, title) else []
    first = _person(head)
    if first is None or following is not None or not _title_name(first.name):
        return []
    return [first] if _taken(first, _names_organisation(first.affiliation), abstract, title) else []


def _last_part(title: str, abstract: str | None) -> list[ParsedSpeaker]:
    """The person of "<series> | <title> | <name>, <affiliation>", which is the last part."""
    parts = _TITLE_SEPARATOR.split(title)
    last = _leading_person(parts[-1]) if len(parts) >= 3 else None
    if last is None:
        return []
    return [last.person] if _taken(last.person, last.marked, abstract, title) else []


def _conversation(title: str, abstract: str | None) -> list[ParsedSpeaker]:
    """The two people of "<name> in conversation with <name>", when the abstract names both."""
    last = _TITLE_SEPARATOR.split(title)[-1]
    match = _CONVERSATION.fullmatch(last)
    if match is None:
        return []
    people = [_title_person(match.group(key)) for key in ("first", "second")]
    found = [person for person in people if person is not None]
    if len(found) == 2 and all(
        _mentions(person.name, abstract, title).as_person for person in found
    ):
        return found
    return []


def _with(title: str, abstract: str | None) -> list[ParsedSpeaker]:
    """The person of "... with <name>[, <degree>]" at the end of a title."""
    match = _WITH.search(title)
    if match is None:
        return []
    text = match.group("person")
    person = _title_person(text)
    if person is None:
        return []
    return [person] if _taken(person, _has_degree(text), abstract, title) else []


def speakers_from_title(title: str, abstract: str | None = None) -> list[ParsedSpeaker]:
    """The people a title names in one of the common forms (see the module docstring).

    ``abstract`` is the abstract of the same event; it vouches for a name
    that the form alone does not, and it rules out one that it speaks of
    as something other than a person. Empty when the title names nobody.
    """
    text = " ".join(title.split())
    if not text:
        return []
    return (
        _conversation(text, abstract) or _after_separator(text, abstract) or _with(text, abstract)
    )


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
    return _people_or_affiliations(_split(line, _HARD_SEPARATOR))
