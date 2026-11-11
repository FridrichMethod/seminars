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
  "equation". Names and other terms that must match exactly are compiled
  with ``inflect=False``.
* A term that ends with ``*`` matches any word that starts with its last
  word: "superconduct*" matches "superconductivity" and "superconducting".
  The prefix must have at least three letters.
* Spellings that fold to the same words count once; the first one is kept.

The field tagger (``build_field_tagger``) decides the fields of a candidate:

1. Source fields: the hints of the candidate (the sources' ``default_fields``
   and the drafts' ``field_hints``), ``taxonomy.source_defaults`` of every
   source in ``hints.source_ids``, and ``taxonomy.departments`` whose label
   equals one of ``hints.categories`` (same words after folding). These are
   facts stated by the source; method ``"source"``.
2. Rule fields, per field of ``taxonomy.fields``: a name of ``series`` that
   appears in the series or the title implies the field; otherwise the
   weights of the distinct ``keywords`` found in title, series and abstract
   must add up to ``min_score``. A match of ``exclude_keywords`` vetoes the
   rule evidence of that field (never a source field).
3. The fields are the union of both. The method is ``"source"`` when there
   are source fields and the rules add nothing to them, else ``"rules"``.
   ``needs_llm`` is set when there is no field at all, and when an event
   without source fields lost a field to a veto (evidence for and against
   it): the LLM tagger, when there is one, decides those, otherwise the
   rule decision stands.
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

_SEGMENT_BREAK: Final = re.compile(r"(?<![.,;:!?])[.,;:!?]+(?=\s|$)|[\n()\[\]{}<>\"|]")
"""Segment ends. A run of punctuation is only tried from its first character, so that a
long run that is not followed by white space costs linear time, not quadratic."""
_INITIAL: Final = re.compile(r"(?<!\w)([^\W\d_])\.(?=\s)")
"""A single letter and its point, as in "E. coli": the point is dropped before splitting."""
_WORD: Final = re.compile(r"\w+")


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


def _forms(word: str) -> frozenset[str]:
    """Singular and plural forms of ``word`` that a text may use."""
    forms = {word, f"{word}s", f"{word}es"}
    if word.endswith("y") and len(word) > 2:
        forms.add(f"{word[:-1]}ies")
    if word.endswith("s") and len(word) > 3:
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
        return cls(Words.of(event.title), Words.of(event.series), Words.of(event.abstract))


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

    def evaluate(self, texts: EventTexts) -> _Evidence:
        series = self.series.first(texts.series, texts.title)
        keywords = self.keywords.matched(texts.title, texts.series, texts.abstract)
        score = sum(self.weights[term] for term in keywords)
        if series is None and score < self.min_score - SCORE_TOLERANCE:
            return _Evidence(self.field)
        veto = self.vetoes.first(texts.title, texts.series, texts.abstract)
        if veto is not None:
            return _Evidence(self.field, vetoed=True, reason=f'{self.field}: vetoed by "{veto}"')
        parts = [f'series "{series}"'] if series is not None else []
        if keywords:
            parts.append(f"keywords {quoted(keywords)} ({score:g})")
        return _Evidence(self.field, assigned=True, reason=f"{self.field}: {'; '.join(parts)}")


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
        evidence = [rule.evaluate(texts) for rule in self._rules]
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
    "EventTexts",
    "Hit",
    "RuleFieldTagger",
    "TermMatcher",
    "Words",
    "build_field_tagger",
    "label_key",
    "quoted",
]
