"""Rule-based field tagging, and the word matching that every rule of this package uses.

How a term matches a text (field keywords, series names, filter phrases,
picks keywords and names alike):

* Both are compared word by word. Text is folded first: compatibility
  decomposition (NFKD), accents dropped, case folded, so "Kahler" matches
  "K\\u00e4hler" and "AI" matches "ai". A word is a run of letters, digits
  and underscores; spaces, hyphens, slashes and apostrophes only separate
  words, so "cryo-EM" and "cryo EM" are the same term.
* A term matches whole words only: "bio" never matches "biography" and
  "ai" never matches "Taiwan".
* The words of a phrase must follow each other inside one segment of the
  text. A segment ends at a line break, at a bracket or double quote, and at
  a ``.,;:!?`` that is followed by white space or the end of the text, so
  "the protein. Design" does not contain "protein design" (but "3.5" and
  "Ph.D" stay whole). A point after a single letter is an initial and does
  not end a segment: "E. coli", "C. elegans" and "J. R. Doe" stay whole.
* The last word of a term may appear singular or plural: "enzyme" matches
  "enzymes", "galaxy" matches "galaxies", and "equations" matches
  "equation". A word that ends in "ics" is never made singular: "genetics"
  does not match "genetic", nor "optics" "optic". Names and other terms
  that must match exactly are compiled with ``inflect=False``.
* A term that ends with ``*`` matches any word that starts with its last
  word: "superconduct*" matches "superconductivity" and "superconducting".
  The prefix must have at least three letters.
* Spellings that fold to the same words count once; the first one is kept.

The field tagger (``build_field_tagger``) decides the fields of a candidate:

1. Source fields: the hints of the candidate (the sources' ``default_fields``
   and the drafts' ``field_hints``), ``taxonomy.source_defaults`` of every
   source in ``hints.source_ids``, and ``taxonomy.departments`` whose label
   equals one of ``hints.categories`` (same words after folding). These are
   facts stated by the source; method ``"source"``. A tag (``hints.tags``)
   is never one: whoever posts an event chooses its tags freely, and a talk
   on the history of astronomy tagged "mathematics" is no talk in
   mathematics. The LLM tagger sees the tags.
2. Rule fields, per field of ``taxonomy.fields``: a name of ``series`` that
   appears in the series or the title implies the field; otherwise the
   weights of the distinct ``keywords`` found in title, series and abstract
   must add up to ``min_score``. Each keyword counts once, and a word counts
   for one keyword only: where keywords of one field overlap in a text
   ("drug" and "drug discovery", "health" and "public health"), the
   heaviest counts, then the longest, then the first. A match of
   ``exclude_keywords`` vetoes the rule evidence of that field (never a
   source field).
   The rules read the abstract without its sections on the speaker and the
   series (``talk_text``): a line that is a heading such as "Bio",
   "Speaker Biography", "About the speaker" or "About the ... Series" (alone
   on its line, or followed by a colon and text; an "About ..." heading
   also when its first sentence follows without a break, "About the
   SeriesThe ...") opens such a section, and it runs to the next heading of
   the talk ("Abstract", "Title", "About the seminar", ..., also "Talk:",
   "Topic:", "Abstract - We ..." and "AbstractWe ...") or to the end. A
   line that opens such a section and holds an "Abstract" heading further
   on (a page flattened into one line: "About the SpeakerJane ...
   AbstractWe ...") keeps the text from that heading on. A speaker who
   holds a degree in physics, or a series that "bridges neuroscience and
   data science", says nothing about the field of the talk. A biography
   without a heading is read like the talk, and a talk that follows a
   biography without a heading of its own (a title in quotes, then the
   text) is left out with it.
3. The abstract alone never decides for an event without source fields:
   there, the series name or the keywords of title and series must reach
   ``min_score`` without it. Abstracts on calendar-wide sources (humanities,
   policy and wellness talks) mention "AI", "a physicist" or "vaccines" in
   passing, in a speaker's biography or a list of words, and a decision of
   the rules is final when no LLM is configured, so such a field is only
   reported. For an event that has source fields, and so is known to be a
   talk of the seven fields, the talk part of the abstract counts like the
   title and can add fields.
4. The fields are the union of source and rule fields. The method is
   ``"source"`` when there are source fields and the rules add nothing to
   them, else ``"rules"``. ``needs_llm`` is set when there is no field at
   all (an abstract-only match included), and when an event without source
   fields lost a field to a veto (evidence for and against it): the LLM
   tagger, when there is one, decides those, otherwise the rule decision
   stands and an event without a field is dropped as ``out_of_scope``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from seminars.config import ConfigError, FieldRules, Taxonomy
from seminars.contracts import FieldTagger
from seminars.models import Candidate, Event, EventHints, TagDecision
from seminars.models.base import canonical_fields

PREFIX_MARK: Final = "*"
MIN_PREFIX_LETTERS: Final = 3
SCORE_TOLERANCE: Final = 1e-9
"""Sums of weights are compared with this tolerance (0.1 + 0.2 is not exactly 0.3)."""

NO_FIELD_REASON: Final = "no field matched"
ABSTRACT_ONLY_NOTE: Final = "not enough in title and series (no source field)"
"""Ends the reason of a field that only the abstract supports, on an event without source fields."""

_SEGMENT_BREAK: Final = re.compile(r"(?<![.,;:!?])[.,;:!?]+(?=\s|$)|[\n()\[\]{}<>\"|]")
"""Segment ends. A run of punctuation is only tried from its first character, so that a
long run that is not followed by white space costs linear time, not quadratic."""
_INITIAL: Final = re.compile(r"(?<!\w)([^\W\d_])\.(?=\s)")
"""A single letter and its point, as in "E. coli": the point is dropped before splitting."""
_WORD: Final = re.compile(r"\w+")

_ROLE: Final = r"(?:speaker|presenter|panelist|moderator|lecturer|author|host)s?"
_BIO: Final = (
    r"(?:(?:speaker|presenter|lecturer)(?:'s|s'|s)?\s+|short\s+|brief\s+)?"
    r"(?:bios?|biograph(?:y|ies)|bio\s*sketch|biographical\s+(?:sketch|note))"
)
_ABOUT: Final = (
    rf"about\s+(?:the|our)\s+{_ROLE}(?:\s*(?:,|and|&)\s*(?:the\s+)?{_ROLE})*"
    r"|about\s+(?:\S+\s+){0,8}?series"
)
_TALK: Final = (
    r"(?:(?:talk|seminar|lecture|colloquium|presentation)\s+)?"
    r"(?:abstract|summary|description|title|overview)"
    r"|about\s+(?:the|this)\s+(?:talk|seminar|lecture|colloquium|presentation|event)"
)
_REST: Final = r"\s*(?::.*)?"
"""What follows a heading on its line: nothing, or a colon and the first text of the section.
(Linear: a run of white space can be read one way only.)"""
_RUN_ON: Final = r"(?-i:[A-Z]).*"
"""What may also follow an "About ..." heading: its first sentence without a break, as a page
reads once flattened that sets the heading apart by style alone ("About the SeriesThe ...").
Never after "Bio", which begins words of its own ("BioE", "BioX")."""
_DASH: Final = r"(?:\s*[\u2013\u2014]|\s+-{1,2}|-{1,2}(?=\s))"
"""A dash after a heading, before the first text of the section ("Abstract - We ..."); a
hyphen needs white space on one side, so "Abstract-level" stays a word."""
_ABSTRACT_RUN_ON: Final = r"(?-i:Abstract[A-Z]).*"
""""Abstract" run into its first sentence ("AbstractWe ..."), written as a heading is."""
_ASIDE_HEADING: Final = re.compile(
    rf"\s*(?:(?:{_BIO}){_REST}|(?:{_ABOUT})(?:{_REST}|{_RUN_ON}))", re.IGNORECASE | re.DOTALL
)
"""A line that opens a section on the speaker or the series, which the field rules skip."""
_TALK_HEADING: Final = re.compile(
    rf"\s*(?:(?:{_TALK}){_REST}|(?:talk|topic)\s*:.*|abstract{_DASH}.*|{_ABSTRACT_RUN_ON})",
    re.IGNORECASE | re.DOTALL,
)
"""A line that opens a section on the talk, which ends a section on the speaker or the series:
a heading of ``_TALK``, "Talk:" or "Topic:", or "Abstract" followed by a dash or run into its
first sentence."""
_EMBEDDED_ABSTRACT: Final = re.compile(rf"Abstract(?=[A-Z]|\s*:|{_DASH})")
"""An "Abstract" heading inside a line that opened a section on the speaker, where a page
was flattened into one line ("About the SpeakerJane ... AbstractWe ...")."""


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


@dataclass(frozen=True, slots=True)
class Words:
    """A text cut into segments of folded words (see the module docstring)."""

    segments: tuple[tuple[str, ...], ...] = ()

    @classmethod
    def of(cls, text: str | None) -> Words:
        """The words of ``text``; ``None`` and blank text have none."""
        if not text:
            return cls()
        folded = _INITIAL.sub(r"\1", _fold(text))
        pieces = (_WORD.findall(piece) for piece in _SEGMENT_BREAK.split(folded))
        return cls(tuple(tuple(piece) for piece in pieces if piece))


def talk_text(abstract: str | None) -> str | None:
    """``abstract`` without its sections on the speaker and the series (module docstring)."""
    if not abstract:
        return abstract
    kept: list[str] = []
    aside = False
    for line in abstract.split("\n"):
        if _ASIDE_HEADING.fullmatch(line):
            embedded = _EMBEDDED_ABSTRACT.search(line)
            aside = embedded is None
            if embedded is not None:
                kept.append(line[embedded.start() :])
            continue
        if _TALK_HEADING.fullmatch(line):
            aside = False
        if not aside:
            kept.append(line)
    return "\n".join(kept)


def label_key(text: str) -> str:
    """All words of ``text`` joined by single spaces: equal for labels that differ in form only."""
    return " ".join(word for segment in Words.of(text).segments for word in segment)


@dataclass(frozen=True, slots=True)
class Hit:
    """One match of a term: words ``start`` to ``stop`` (exclusive) of segment ``segment``."""

    term: str
    segment: int
    start: int
    stop: int

    def overlaps(self, other: Hit) -> bool:
        """True when both hits cover a common word (of the same text)."""
        return self.segment == other.segment and self.start < other.stop and other.start < self.stop


def _forms(word: str) -> frozenset[str]:
    """Singular and plural forms of ``word`` that a text may use."""
    forms = {word, f"{word}s", f"{word}es"}
    if word.endswith("y") and len(word) > 2:
        forms.add(f"{word[:-1]}ies")
    if word.endswith("s") and len(word) > 3 and not word.endswith("ics"):
        forms.add(word[:-1])
    if word.endswith("es") and len(word) > 4:
        forms.add(word[:-2])
    return frozenset(forms)


@dataclass(frozen=True, slots=True)
class _Term:
    text: str
    words: tuple[str, ...]
    prefix: bool
    last_forms: frozenset[str]

    def stop_at(self, segment: tuple[str, ...], start: int) -> int | None:
        """End of a match that begins at ``start`` of ``segment``, or ``None``."""
        stop = start + len(self.words)
        if stop > len(segment) or segment[start : stop - 1] != self.words[:-1]:
            return None
        last = segment[stop - 1]
        if self.prefix:
            return stop if last.startswith(self.words[-1]) else None
        return stop if last in self.last_forms else None


def _parse_term(text: str, *, inflect: bool, label: str) -> _Term:
    stripped = text.strip()
    prefix = stripped.endswith(PREFIX_MARK)
    found = tuple(_WORD.findall(_fold(stripped.rstrip(PREFIX_MARK))))
    if not found:
        raise ConfigError(f"{label}: term {text!r} has no letters or digits")
    if prefix and len(found[-1]) < MIN_PREFIX_LETTERS:
        raise ConfigError(
            f"{label}: term {text!r} needs at least {MIN_PREFIX_LETTERS} letters before '*'"
        )
    if prefix:
        forms: frozenset[str] = frozenset()
    elif inflect:
        forms = _forms(found[-1])
    else:
        forms = frozenset({found[-1]})
    return _Term(text=text, words=found, prefix=prefix, last_forms=forms)


class TermMatcher:
    """Finds configured terms in texts (see the module docstring for the rules).

    ``label`` names the configuration entry the terms come from; it is part
    of the message of the ``ConfigError`` raised for a term that can never
    match (no letters or digits, or a prefix shorter than three letters).
    """

    def __init__(self, terms: Iterable[str], *, inflect: bool = True, label: str = "terms"):
        compiled: dict[tuple[tuple[str, ...], bool], _Term] = {}
        for text in terms:
            term = _parse_term(text, inflect=inflect, label=label)
            compiled.setdefault((term.words, term.prefix), term)
        self._terms: tuple[_Term, ...] = tuple(compiled.values())
        index: dict[str, list[_Term]] = {}
        prefixed: list[_Term] = []
        for term in self._terms:
            if len(term.words) == 1 and term.prefix:
                prefixed.append(term)
                continue
            keys = term.last_forms if len(term.words) == 1 else {term.words[0]}
            for key in keys:
                index.setdefault(key, []).append(term)
        self._index: dict[str, tuple[_Term, ...]] = {k: tuple(v) for k, v in index.items()}
        self._prefixed: tuple[_Term, ...] = tuple(prefixed)
        self._order: dict[str, int] = {term.text: n for n, term in enumerate(self._terms)}

    @property
    def terms(self) -> tuple[str, ...]:
        """The distinct terms, in configuration order."""
        return tuple(term.text for term in self._terms)

    def _candidates(self, word: str) -> Iterable[_Term]:
        yield from self._index.get(word, ())
        for term in self._prefixed:
            if word.startswith(term.words[0]):
                yield term

    def hits(self, words: Words) -> tuple[Hit, ...]:
        """Every match in ``words``, by position, then in configuration order."""
        found: list[Hit] = []
        for number, segment in enumerate(words.segments):
            for start, word in enumerate(segment):
                for term in self._candidates(word):
                    stop = term.stop_at(segment, start)
                    if stop is not None:
                        found.append(Hit(term.text, number, start, stop))
        return tuple(sorted(found, key=lambda hit: (hit.segment, hit.start, self._order[hit.term])))

    def matched(self, *texts: Words) -> tuple[str, ...]:
        """The terms found in any of ``texts``, each once, in configuration order."""
        found = {hit.term for words in texts for hit in self.hits(words)}
        return tuple(term for term in self.terms if term in found)

    def first(self, *texts: Words) -> str | None:
        """The first term in configuration order that is found in any of ``texts``."""
        found = self.matched(*texts)
        return found[0] if found else None


def quoted(terms: Iterable[str]) -> str:
    """``"a", "b"``: terms as they are listed in reasons."""
    return ", ".join(f'"{term}"' for term in terms)


# --- field tagging -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventTexts:
    """The words of the texts that rules look at."""

    title: Words
    series: Words
    abstract: Words

    @classmethod
    def of(cls, event: Event) -> EventTexts:
        """Title, series and the talk part of the abstract (``talk_text``)."""
        return cls(
            Words.of(event.title), Words.of(event.series), Words.of(talk_text(event.abstract))
        )


@dataclass(frozen=True, slots=True)
class _Evidence:
    field: str
    assigned: bool = False
    vetoed: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _FieldRule:
    field: str
    keywords: TermMatcher
    weights: Mapping[str, float]
    series: TermMatcher
    vetoes: TermMatcher
    min_score: float

    @classmethod
    def compile(cls, field: str, rules: FieldRules) -> _FieldRule:
        place = f"taxonomy fields.{field}"
        return cls(
            field=field,
            keywords=TermMatcher(rules.keywords, label=f"{place}.keywords"),
            weights=rules.keywords,
            series=TermMatcher(rules.series, label=f"{place}.series"),
            vetoes=TermMatcher(rules.exclude_keywords, label=f"{place}.exclude_keywords"),
            min_score=rules.min_score,
        )

    def _rank(self, hit: Hit) -> tuple[float, int]:
        return (-self.weights[hit.term], hit.start - hit.stop)

    def counted(self, *texts: Words) -> tuple[str, ...]:
        """The keywords that count in ``texts``, in configuration order.

        Within one text, overlapping hits count once: the heaviest keyword,
        then the longest, then the first (hits come by position).
        """
        found: set[str] = set()
        for words in texts:
            taken: list[Hit] = []
            for hit in sorted(self.keywords.hits(words), key=self._rank):
                if not any(hit.overlaps(other) for other in taken):
                    taken.append(hit)
            found.update(hit.term for hit in taken)
        return tuple(term for term in self.keywords.terms if term in found)

    def _reaches(self, keywords: Iterable[str]) -> bool:
        score = sum(self.weights[term] for term in keywords)
        return score >= self.min_score - SCORE_TOLERANCE

    def evaluate(self, texts: EventTexts, *, abstract_decides: bool) -> _Evidence:
        """The evidence for this field; the abstract decides only if ``abstract_decides``."""
        series = self.series.first(texts.series, texts.title)
        keywords = self.counted(texts.title, texts.series, texts.abstract)
        if series is None and not self._reaches(keywords):
            return _Evidence(self.field)
        veto = self.vetoes.first(texts.title, texts.series, texts.abstract)
        if veto is not None:
            return _Evidence(self.field, vetoed=True, reason=f'{self.field}: vetoed by "{veto}"')
        score = sum(self.weights[term] for term in keywords)
        parts = [f'series "{series}"'] if series is not None else []
        if keywords:
            parts.append(f"keywords {quoted(keywords)} ({score:g})")
        found = f"{self.field}: {'; '.join(parts)}"
        headline_only = series is None and not abstract_decides
        if headline_only and not self._reaches(self.counted(texts.title, texts.series)):
            return _Evidence(self.field, reason=f"{found}, {ABSTRACT_ONLY_NOTE}")
        return _Evidence(self.field, assigned=True, reason=found)


class RuleFieldTagger:
    """``FieldTagger`` driven by config/taxonomy.yaml (see the module docstring)."""

    def __init__(self, taxonomy: Taxonomy) -> None:
        # taxonomy.fields always holds all seven fields, in canonical order
        self._rules = tuple(
            _FieldRule.compile(field, rules) for field, rules in taxonomy.fields.items()
        )
        self._source_defaults = dict(taxonomy.source_defaults)
        departments: dict[str, tuple[str, list[str]]] = {}
        for label, fields in taxonomy.departments.items():
            departments.setdefault(label_key(label), (label, fields))
        self._departments = departments

    def _source_fields(self, hints: EventHints) -> tuple[list[str], list[str]]:
        fields: list[str] = [*hints.default_fields, *hints.field_hints]
        reasons: list[str] = []
        if hints.default_fields:
            reasons.append(f"source default: {', '.join(hints.default_fields)}")
        if hints.field_hints:
            reasons.append(f"source hint: {', '.join(hints.field_hints)}")
        for source_id in hints.source_ids:
            mapped = self._source_defaults.get(source_id, [])
            if mapped:
                fields.extend(mapped)
                reasons.append(f"source map {source_id}: {', '.join(mapped)}")
        categories = {label_key(category) for category in hints.categories}
        for key, (label, mapped) in self._departments.items():
            if key in categories and mapped:
                fields.extend(mapped)
                reasons.append(f'department "{label}": {", ".join(mapped)}')
        return canonical_fields(fields), reasons

    def tag(self, candidate: Candidate) -> TagDecision:
        """Decide the fields of ``candidate``; ``needs_llm`` marks what the rules cannot."""
        source_fields, reasons = self._source_fields(candidate.hints)
        texts = EventTexts.of(candidate.event)
        abstract_decides = bool(source_fields)
        evidence = [rule.evaluate(texts, abstract_decides=abstract_decides) for rule in self._rules]
        rule_fields = [item.field for item in evidence if item.assigned]
        reasons.extend(item.reason for item in evidence if item.reason is not None)
        fields = canonical_fields([*source_fields, *rule_fields])
        conflict = any(item.vetoed for item in evidence)
        if not fields:
            reasons.append(NO_FIELD_REASON)
        added = set(rule_fields) - set(source_fields)
        return TagDecision(
            fields=fields,
            method="source" if source_fields and not added else "rules",
            needs_llm=not fields or (conflict and not source_fields),
            reasons=reasons,
        )


def build_field_tagger(taxonomy: Taxonomy) -> FieldTagger:
    """``FieldTaggerFactory``: the rule-based tagger for ``taxonomy``.

    Raises ``ConfigError`` for a term that can never match.
    """
    return RuleFieldTagger(taxonomy)


__all__ = [
    "ABSTRACT_ONLY_NOTE",
    "NO_FIELD_REASON",
    "EventTexts",
    "Hit",
    "RuleFieldTagger",
    "TermMatcher",
    "Words",
    "build_field_tagger",
    "label_key",
    "quoted",
    "talk_text",
]
