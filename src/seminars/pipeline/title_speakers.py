"""Reading the speakers that a title names.

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
names as well. Then the form must vouch for it. A mark that goes with the
name is an honorific ("Dr. Jane Doe"), a degree ("Jane Doe PhD", "Jane
Doe, MD"), an organisation in brackets or after a comma, or a quoted title
after it in the same part or at the start of the next; a name suffix is
none ("Knots II"). A part in front of the name
that honours a person ("In Memoriam", "In Honor of", "Obituary",
"Tribute", "Celebration of Life") names the person a memorial is for, so
its title gives no speaker. The parts after the first one that starts with
a name are the talk title, quoted or not, and so are the parts of a quoted
talk title ("Seminar: Dr. Jane Doe - In Memoriam: The Last Glacier",
'Seminar: "Tribute: Songs of the Sea" - Dr. Jane Doe'); an unquoted talk
title in front of the name cannot be told from a memorial head ("Seminar |
In Memoriam: The Last Glacier | Dr. Jane Doe" gives no speaker). After a
head that is only a term, a month or a year ("Spring 2025",
"Jan. 2025", "2025-26") an honorific is no mark in any of the parts:

* series first ("<series>: <name>[, <affiliation>][ - "<title>"]"): the
  part in front is a series, or a mark goes with the name, or the abstract
  names the person. When nothing marks the name and the next part reads
  like a name as well, either one can be the talk title ("Seminar: Public
  Health - Jane Roe"): the speaker is the one that does not read like a
  topic and that the abstract names, else the second when a mark goes
  with it, else the first when the second reads like a topic, else
  nobody;
* series, talk title, then the name ("<series> | <title> | <name>,
  <affiliation>"): the part in front is a series and the last part is a
  person with a mark, or whom the abstract names;
* name first ("<name>: <title>", "<name> (<affiliation>): <title>"): the
  rest does not start with a name as well, and the affiliation in
  brackets names an organisation or the abstract names the person;
* "... with <name>" at the end: a degree follows ("with Jane Doe, PhD")
  or the abstract names the person; "<name> in conversation with <name>"
  gives both people on the same terms.

"The abstract names the person" means that the name occurs in the abstract
outside a copy of the title, next to what goes with a person ("Dr.", ",
PhD", "received", "'s"), so a talk title that the abstract repeats vouches
for nothing. An organisation named after the name on the same line ("the
Jane Doe Lab") counts only for a name that does not read like a topic and
never between two parts, as a group is named after its topic as well ("the
Public Health Department"); an honorific or a label in front of the name
("Dr. Jane Roe Department of ...") counts whatever follows. A title that is
only a name, a series name, and everything else give no speaker.

A name that reads like a topic as well is a speaker only when the abstract
names the person, whatever the form: one that holds a word of a field, a
topic, a place or an event ("Dark Matter", "Deep Learning", "Quantum
Groups", "Monte Carlo", "Coffee Hour") or an adjective of a field
("Topological Insulators"), whose last word ends like a word of a field
("Floer Homology", "Random Matrices", "Causal Inference"), that starts
with "A" or with a verb in "-ing" ("Navigating Heritage"), or whose every
word is a common word of English or of a field ("Heart Failure", "Sea
Level Rise", "Elliptic Curves", "Wound Healing"). The endings of a field
leave out the short words that names end in as well ("Lawrence",
"Clement", "Davies", "de Vries"), and the common words leave out first
names ("Grace", "Mark", "Ray", "Dominic", "Constance", "Clement",
"Crystal"), so most names hold a word outside them ("Emily Fox", "Anthony
Green", "Clement Price"), and the words of a field hold the topics such a
first name starts ("Crystal Growth").

These tests are word lists and endings, not a general rule, and that is
their limit: a topic of words they do not hold ("Salt Marshes", "Bird
Song") still reads like a name after a series, and a name of common words
only ("Coral Banks", "River Stone", "Forest White", "Gene Price") needs its
abstract: "River", "Forest" and "Gene" start topics far more often ("River
Floods", "Forest Fires", "Gene Transfer"). A wrong speaker is worse than
none, so the lists err towards topics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Final

from seminars.pipeline.speakers import (
    CJK_ORGANISATION_WORDS,
    COMMA,
    LABEL,
    ORGANISATION_WORDS,
    WITH_AFFILIATION,
    ParsedSpeaker,
    looks_like_name,
    parse_person,
    split_outside_brackets,
)
from seminars.pipeline.text import fold, is_degree, tidy_name, word_set

_SERIES_WORDS: Final = word_set(
    "seminar seminars lecture lectures lectureship colloquium colloquia rounds defense defence"
    " series symposium symposia workshop workshops forum conference summit chat conversation"
    " conversations talk talks panel panels webinar meeting program programme course session"
    " sessions presentation presentations keynote award prize celebration retreat luncheon"
    " reception club exchange alliance updates showcase festival exhibition hackathon sprint"
    " bootcamp training orientation discussion roundtable gala ceremony"
)
"""Words of the name of a series or an event; no person's name holds one."""
_MEMORIAL: Final = re.compile(
    r"(?:(?:a|an|the) )?(?:in (?:loving )?(?:memoriam|memory)(?: of)?|in remembrance(?: of)?"
    r"|in honou?r of|honou?ring|obituar(?:y|ies)|memorial(?: (?:service|gathering))?|tributes?"
    r"|remembering|remembrance|celebrat(?:ion|ing) of (?:the )?life(?: of)?"
    r"|celebrating (?:the )?life of)"
)
"""A head that honours a person, folded ("In Memoriam", "In Honor of", "Obituary", "Tribute",
"Celebration of Life"): the name after it is the person the memorial is for."""
_TERM: Final = re.compile(
    r"(?:(?:(?:ay|academic year) )?(?:(?:spring|summer|fall|autumn|winter)(?: (?:quarter"
    r"|semester|term))?)?|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r" ?(?:(?:19|20)\d{2}(?: \d{2}(?:\d{2})?)?)?"
)
"""A head that is only a term, a month or a year, folded ("Spring 2025", "Autumn Quarter",
"Jan. 2025", "September", "2025-26")."""
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
_TITLED_BEFORE: Final = re.compile(
    r"\b(?:(?:dr|prof|mr|mrs|ms|mx)\.?|professor|speaker|presenter)\s{0,3}:?\s{0,3}$",
    re.IGNORECASE,
)
"""An honorific or a label in front of a name, which only a person has ("Dr. Jane Roe").
Only an abbreviation takes a dot: "the speaker." ends a sentence and labels nothing."""
_INTRODUCED_BEFORE: Final = re.compile(
    r"\b(?:featuring|features?|welcomes?|by)\s{0,3}:?\s{0,3}$", re.IGNORECASE
)
"""Words in front of a name that introduce a person, and an organisation as well
("Sponsored by the Ocean Mixing Lab")."""
_ORGANISATION_WORD: Final = (
    r"(?:university|college|institutes?|schools?|labs?|laborator(?:y|ies)|cent(?:er|re)s?"
    r"|hospitals?|foundation|department)\b"
)
_PERSON_AFTER: Final = re.compile(
    r"\s{0,3},?\s{0,3}(?:ph\.?\s?d|m\.?\s?d|d\.?\s?phil|sc\.?\s?d|professor|prof|associate"
    r"|assistant|director|chair|dean|principal|lecturer|investigator|scientist|researcher"
    r"|fellow|postdoc|founder|ceo|president)(?:\b|(?-i:(?=[A-Z])))"
    r"|\s{1,3}(?:received|holds|leads|joined|earned|serves|directs|completed|obtained|studies"
    r"|works|is\s{1,3}(?:a|an|the)\s{1,3}(?:\w+\s{1,3}){0,2}(?:professor|director|scientist"
    r"|researcher|fellow|student|chair|dean|lecturer|investigator|postdoc))\b"
    r"|'s\s|\u2019s\s"
    rf"|(?-i:(?=[A-Z])){_ORGANISATION_WORD}",
    re.IGNORECASE,
)
"""What follows a person's name in a biography: a degree, a position, "received" or the
possessive, also run together as text taken out of HTML often is ("Jane Doe, PhDProfessor
of Biology"), and an institution run on to the name ("Jane DoeUniversity of Toronto")."""
_ORGANISATION_AFTER: Final = re.compile(rf"[ \t]{{1,3}}{_ORGANISATION_WORD}", re.IGNORECASE)
"""An organisation named after the name before it, on the same line: after a person ("the
Jane Doe Lab") or after a topic ("the Public Health Department", "the Ocean Mixing Lab")."""
_HONORIFIC: Final = re.compile(r"(?:dr|prof|professor|mr|mrs|ms|mx|sir|dame)\b\.?\s", re.IGNORECASE)
_NAME_SUFFIXES: Final = word_set("jr sr ii iii iv")
"""Suffixes of a name that end a part of a series as well ("Knots II"): no mark of a person."""
_GERUND: Final = re.compile(r"[A-Z][a-z]{3,}ing\b")
"""A first word that is a verb in "-ing" of at least seven letters ("Navigating")."""
_TOPIC_WORDS: Final = word_set(
    "matter theory theories learning inference model models system systems networks holes flow"
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
    " microbiome bacteria viruses virus tumors tumours drugs growth defect defects lattice"
    " lattices phase phases shape shapes size sizes packing melting habit habits optics"
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
    " magic letters hour hours quarter fair fairs salon class classes week weekend"
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
_COMMON_WORDS: Final = word_set(
    # the body, health and care
    "heart kidney liver lung brain bone marrow blood skin eye ear gut cell tissue organ muscle"
    " nerve spine spinal cord joint hip knee breast prostate colon stomach tooth teeth mouth"
    " throat neck chest womb birth child childhood infant baby mother maternal pregnancy care"
    " health healthcare surgery pain sleep stroke injury trauma wound valve disorder syndrome"
    " infection covid flu fever asthma allergy diabetes dementia autism stress apnea sickle"
    " anemia sepsis malaria vaccine drug dose opioid transplant donor patient nurse doctor"
    " diet food hunger hearing vision loss gain risk outcome diagnosis treatment recovery scan"
    " test trial cohort mental chronic acute rare sudden immune viral oral renal vital toxic"
    # the earth, life and the environment
    " ice sheet sea level rise coast coastal coral reef fish forest tree plant soil seed root"
    " leaf flower crop farm water river lake rain wind storm flood drought fire wildfire smoke"
    " air pollution carbon capture storage emission power grid fuel oil gas coal nuclear"
    " hydrogen methane ozone earth moon space galaxy light rays glacier mountain volcano"
    " rock mantle plume plate fault desert island polar tropical urban rural city land habitat"
    " species wildlife bird insect bee animal mammal whale shark frog mouse rat fly worm yeast"
    " microbe fungus algae plankton permafrost thaw retreat melt weather season sediment"
    " flare sail burst cluster atom spin plasma metal iron copper gold silver steel glass"
    " plastic paper cement concrete sand dust salt acid ion ionic liquid solid fluid film"
    " battery engine motor vehicle car truck ship plane drone rocket satellite bridge road"
    " rail transit traffic port dam pipe"
    # computing, media and society
    " network internet web cloud code software hardware language speech text image video game"
    " music media news story mind thought emotion decision choice teaching education teacher"
    " youth family home house housing labor work job market trade money finance bank tax"
    " price cost wealth poverty justice law right policy politics state nation war peace"
    " conflict crime prison police vote election democracy race gender culture religion"
    " ethics privacy trust crisis source search engine smart open free fair safe safety"
    " private public human civil legal moral political federal global local national"
    # mathematics and the sciences
    " number prime curve sphere point line ring field flow map measure mean median average"
    " variable function equation operator form modular elliptic hyperbolic convex minimal"
    " harmonic additive tropical toric lattice mirror moduli packing derived body category"
    " proof logic set sum product rate ratio order chaos noise signal pattern shape scale size"
    " edge frontier limit origin growth decline change death life age era century time"
    # common adjectives
    " big small large little long short high low deep dark early late old young fast slow"
    " hot cold warm cool wet dry clean dirty hard soft heavy weak strong strange new real true"
    " false full empty open closed simple complex basic general special common single double"
    " total partial whole main primary direct indirect inner outer upper lower central red"
    " blue green white black brown gray yellow ultrafast solar lunar heat radio flight stone"
    " optic optics dispersal removal survival arrival renewal voting"
    # general nouns of titles
    " access agent alert analog answer area array aspect asset attack band barrier base basis"
    " battle beam benefit bias block board border boundary box branch budget bug burden cable"
    " capacity capital cause chain channel charge chart check circuit claim class clock color"
    " copy core corner count country court cover crack cross crowd cure current cut cycle"
    " damage danger date deal debt decay defect degree delay delivery demand depth design"
    " detail device disaster discovery distance drift drive drop duty economy effect egg"
    " element end entry error event exit face fact factor fat fear fee fight firm fit floor"
    " focus force frame fruit fund gap garden gate goal grain graph grass ground guide habit"
    " hair half hand harm help hole horizon host idea income index input interest item"
    " journey key kind layer lead lift link list load loan lock margin mass match matter"
    " meal meat milk mine mix mode model monitor move mystery net node norm north south east"
    " west note object office output owner oxygen pace part particle partner party pass path"
    " peak people phase phone photon piece pitch place plan plot pole pool position post"
    " powder practice print profit property pulse pump range rank reach record reserve"
    " resource rest result return reward role room rule sample scene screen seat sector self"
    " sense service sex share shell shift shock side sign site skill sound speed sport spread"
    " spring square stage station step stock store stream street strike string style subject"
    " sugar supply survey switch symbol system table tail talent target task term theme thing"
    " threat tool top touch tower town trace track trail train trait transfer trap travel"
    " trend truth tube turn type unit universe use user value vapor vector view voice volume"
    " wage wall waste watch wave way weight welfare wheel window wing wire word world year"
    " yield zone history story memory philosophy astronomy anatomy autonomy industry strategy"
    " society"
)
"""Common words of English that topics are made of. A name read from a title whose every
word is one of them, or a word of a field, reads like a topic ("Heart Failure", "Sea Level
Rise", "Elliptic Curves"). Words that are also first names ("Grace", "Mark", "Rose",
"Ray", "Will") or common surnames in Chinese ("Sun") are not in it."""
_FIRST_NAMES: Final = word_set(
    "dominic cedric frederic clarence constance florence laurence lawrence terence terrence"
    " clement prudence patience charity felicity verity crystal"
)
"""First names that end like a common word ("Dominic", "Constance", "Clement") or that are
the singular of a word of a field ("Crystal"), which are not counted as common words."""
_COMMON_ENDINGS: Final = re.compile(
    r"\w{2,}(?:tions?|sions?)|\w{3,}(?:ments?|ness|ity|ities|ism|isms|ist|ists|osis|itis)"
    r"|\w{4,}(?:ures?|ances?|ences?|ive|ives|ous|ings?)|\w{3,}(?:ency|ancy)"
    r"|\w{4,}(?<!ov)(?<!ev)ics?|\w{2,}(?:ogy|ogies)"
)
"""How a common word of English ends ("Failure", "Capture", "Healing", "Cosmic"); "-ovic"
and "-evic" end surnames ("Popovic")."""
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
    marks: bool  # a degree, a quoted title or an organisation goes with the name
    honorific: bool  # an honorific is in front of the name: "Dr. Jane Doe"

    @property
    def marked(self) -> bool:
        return self.marks or self.honorific


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


def _is_term(text: str) -> bool:
    folded = fold(text)
    return bool(folded) and _TERM.fullmatch(folded) is not None


def _names_organisation(text: str | None) -> bool:
    """Whether an affiliation names an organisation ("MIT", "Bando Lab", "UCSF")."""
    if not text:
        return False
    if any(word in text for word in CJK_ORGANISATION_WORDS):
        return True
    return any(word in ORGANISATION_WORDS for word in fold(text).split())


def _mentions(
    name: str, abstract: str | None, title: str, *, organisation: bool = True
) -> _Mentions:
    """How ``abstract`` speaks of ``name``.

    The name is looked for as it is written, without regard to case. Copies
    of the title are taken out first, and a name followed by a separator, a
    quote or the end of its line repeats a part of the title: neither says
    what the name stands for. An honorific or a label in front of the name
    ("Dr.", "Speaker:") speaks of a person whatever follows it. An
    organisation named after the name ("the Jane Doe Lab") speaks of a
    person only when ``organisation`` is set and the name does not read
    like a topic ("the Public Health Department"), and of something else
    otherwise.
    """
    person = tidy_name(name)
    if not abstract or not person:
        return _Mentions(as_person=False, as_other=False)
    copy = r"\s+".join(re.escape(word) for word in title.split())
    text = re.sub(copy, "\n", abstract, flags=re.IGNORECASE) if copy else abstract
    organisation = organisation and not _reads_like_topic(person)
    other = False
    for match in re.finditer(re.escape(person), text, re.IGNORECASE):
        before, end = text[max(match.start() - 30, 0) : match.start()], match.end()
        if _TITLED_BEFORE.search(before):
            return _Mentions(as_person=True, as_other=False)
        if _ORGANISATION_AFTER.match(text, end) is not None:
            if organisation:
                return _Mentions(as_person=True, as_other=False)
            other = True
        elif _INTRODUCED_BEFORE.search(before) or _PERSON_AFTER.match(text, end):
            return _Mentions(as_person=True, as_other=False)
        other = other or _COPY_AFTER.match(text, end) is None
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
    if _TOPIC_ENDINGS.fullmatch(words[-1]) is not None:
        return True
    return all(_is_common(word) for word in words)


def _singular(word: str) -> str:
    """``word`` without a plural ending: "curves" -> "curve", "bodies" -> "body"."""
    if word.endswith("ies") and len(word) > 4:
        return f"{word[:-3]}y"
    if word.endswith(("ches", "shes", "sses", "xes")):
        return word[:-2]
    return word[:-1] if word.endswith("s") and len(word) > 3 else word


def _is_common(word: str) -> bool:
    """Whether a folded word is a common word of English or of a field (``_COMMON_WORDS``)."""
    if word in _FIRST_NAMES:
        return False
    if word in _COMMON_WORDS or _singular(word) in _COMMON_WORDS:
        return True
    if f"{word}s" in _TOPIC_WORDS:  # "planet" of "planets"
        return True
    return any(
        pattern.fullmatch(word) for pattern in (_COMMON_ENDINGS, _TOPIC_ENDINGS, _ADJECTIVE_ENDINGS)
    )


def _taken(person: ParsedSpeaker, vouched: bool, abstract: str | None, title: str) -> bool:
    """Whether a name read from ``title`` is a speaker.

    It is when the abstract names the person, or when the form of the title
    vouches for it (``vouched``), the abstract does not speak of it as
    something else and it does not read like a topic ("Navigating
    Heritage"; "Channing Dungey" needs the abstract too).
    """
    said = _mentions(person.name, abstract, title)
    if said.as_person:
        return True
    return vouched and not said.as_other and not _reads_like_topic(person.name)


def _bare(text: str) -> str:
    """``text`` without an affiliation in brackets at its end."""
    match = WITH_AFFILIATION.match(text)
    return match.group("name") if match else text


def _has_degree(text: str) -> bool:
    return any(is_degree(part) for part in split_outside_brackets(text, COMMA)[1:])


def _title_person(text: str) -> ParsedSpeaker | None:
    """``"Jane Doe, PhD, MIT"`` of a title as a person whose name reads like one.

    Of the parts after the name, only degrees and the parts up to the first
    one that names no organisation or starts in lower case are kept: "Jane
    Doe, Princeton University, on her book ..." is Jane Doe of Princeton
    University.
    """
    first, *rest = split_outside_brackets(text.strip(" ,-\u2013\u2014"), COMMA) or [""]
    person = parse_person(LABEL.sub("", first))
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


def _after_honorific(text: str) -> bool:
    """Whether a name comes after an honorific: "Dr. Jane Doe"."""
    return _HONORIFIC.match(" ".join(_bare(LABEL.sub("", text)).split()) + " ") is not None


def _before_degree(text: str) -> bool:
    """Whether a degree follows a name without a comma: "Jane Doe PhD", not "Knots II"."""
    words = _bare(LABEL.sub("", text)).split()
    last = words[-1] if len(words) > 2 else ""
    return is_degree(last) and last.rstrip(".").casefold() not in _NAME_SUFFIXES


def _leading_person(text: str) -> _Candidate | None:
    """The person a part of a title starts with: a name, an affiliation, then a title.

    Text after a comma must be a degree or name an organisation; otherwise
    the comma separates the words of a title ("Media Art, AI and the Self").
    A quoted title marks the name in the same part or when it opens the
    next one ('Jane Roe - "Coastal Futures"'), never across a further part:
    in 'Bird Song | Jane Roe, "Coastal Futures"' it goes with Jane Roe, not
    with Bird Song.
    """
    quote = _QUOTE.search(text)
    head = text[: quote.start()] if quote else text
    separator = _TITLE_SEPARATOR.search(head)
    head = (head[: separator.start()] if separator else head).strip(" ,-\u2013\u2014")
    person = _title_person(head) if head else None
    if person is None:
        return None
    first, *parts = split_outside_brackets(head, COMMA)
    if any(not is_degree(part) and not _names_organisation(part) for part in parts):
        return None
    quoted = quote is not None and (separator is None or separator.end() == quote.start())
    marks = quoted or bool(parts) or _before_degree(first)
    return _Candidate(
        person=person,
        marks=marks or _names_organisation(person.affiliation),
        honorific=_after_honorific(first),
    )


def _rival(tail: str) -> _Candidate | None:
    """The person a further part of ``tail`` starts with: "Jane Roe" of "Public Health - Jane
    Roe"; ``None`` when ``tail`` has one part or the next does not start with a name."""
    separator = _TITLE_SEPARATOR.search(tail)
    return None if separator is None else _leading_person(tail[separator.end() :])


def _after_term(candidate: _Candidate | None, term: bool) -> _Candidate | None:
    """``candidate`` of a part after a head that is only a term (``term``), where an
    honorific is no mark: "Spring 2025: Dr. Jane Doe", "Spring 2025: Ocean Mixing - Dr. Jane
    Roe"."""
    return replace(candidate, honorific=False) if term and candidate is not None else candidate


def _either(
    first: _Candidate, second: _Candidate, vouched: bool, abstract: str | None, title: str
) -> list[ParsedSpeaker]:
    """The speaker of "<series>: <name> - <name>", where the first name is not marked.

    One of the two parts is a talk title, and both read like a name; the
    module docstring says which is the speaker. An organisation named after
    a part ("the Ocean Mixing Lab") shows that it is no speaker, but not
    that the other part is one.
    """
    parts = (first, second)
    said = [_mentions(part.person.name, abstract, title, organisation=False) for part in parts]
    topic = [_reads_like_topic(part.person.name) for part in parts]
    for part, mention, is_topic in zip(parts, said, topic, strict=True):
        if mention.as_person and not is_topic:
            return [part.person]
    if second.marked and _taken(second.person, True, abstract, title):
        return [second.person]
    if topic[1] and _taken(first.person, vouched, abstract, title):
        return [first.person]
    return []


def _after_separator(title: str, abstract: str | None) -> list[ParsedSpeaker]:
    """People of the series-first and the name-first forms."""
    separator = _TITLE_SEPARATOR.search(title)
    if separator is None:
        return []
    head, tail = title[: separator.start()].strip(), title[separator.end() :].strip()
    term = _is_term(head)  # "Spring 2025: Dr. Jane Doe"
    following = _after_term(_leading_person(tail), term)
    if _is_series(head) or (following is not None and not _title_name(_bare(head))):
        if following is None:
            return _last_part(title, abstract) if _is_series(head) else []
        vouched = _is_series(head) or following.marked
        rival = None if following.marked else _after_term(_rival(tail), term)
        if rival is not None:
            return _either(following, rival, vouched, abstract, title)
        return [following.person] if _taken(following.person, vouched, abstract, title) else []
    first = parse_person(head)
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


def _heads(title: str) -> list[str]:
    """The parts of ``title`` in front of its last one.

    The first separator ends a part wherever it is, as everywhere in this
    module. A later one inside a quoted talk title separates no parts: the
    heads of 'Seminar: "Tribute: Songs of the Sea" - Dr. Jane Doe' are
    "Seminar" and the quoted title as a whole.
    """
    heads: list[str] = []
    start = 0
    for index, separator in enumerate(_TITLE_SEPARATOR.finditer(title)):
        if index == 0 or len(_QUOTE.findall(title, 0, separator.start())) % 2 == 0:
            heads.append(title[start : separator.start()])
            start = separator.end()
    return heads


def _honours_a_person(title: str) -> bool:
    """Whether a part of ``title`` in front of the name is a memorial ("In Memoriam: Dr. Jane
    Doe", "Physics Colloquium | In Memoriam | Dr. Jane Doe").

    The parts after the first one that starts with a name are the talk title,
    so a memorial there keeps the speaker: "Seminar: Dr. Jane Doe - In
    Memoriam: The Last Glacier".
    """
    for head in _heads(title):
        if _MEMORIAL.fullmatch(fold(head)) is not None:
            return True
        if _leading_person(head) is not None:
            return False
    return False


def speakers_from_title(title: str, abstract: str | None = None) -> list[ParsedSpeaker]:
    """The people a title names in one of the common forms (see the module docstring).

    ``abstract`` is the abstract of the same event; it vouches for a name
    that the form alone does not, and it rules out one that it speaks of
    as something other than a person. Empty when the title names nobody.
    """
    text = " ".join(title.split())
    if not text or _honours_a_person(text):
        return []
    return (
        _conversation(text, abstract) or _after_separator(text, abstract) or _with(text, abstract)
    )
