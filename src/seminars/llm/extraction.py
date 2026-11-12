"""The answer to a page extraction request, and what of it may be cached.

The page extractor (``seminars.sources.generic.llm_page``) asks the model for
one item per talk and caches the answer by the piece of page text it sent.
data/llm_cache/ is committed to a public repository, so every text value of
an answer (title, speaker name and affiliation, series, location, abstract)
is checked before the answer is stored. The checks compare a value with the
piece as normalization reads text before it scrubs it
(``seminars.llm.privacy_filter.readable``), with its meeting codes, e-mail
addresses and phone numbers scrubbed in the context of the whole page
(``PieceForms``). Values are taken from that text, which is also the
``source_text`` of the drafts.

A new answer (``grounded_answer``). A value is read and scrubbed the way
normalization does it (``PrivacyFilter.clean``) and must be grounded in the
scrubbed piece; otherwise it is dropped. So a value that is private only in
the words around it (an address spelled "jdoe at stanford.edu" after an
"E-mail:" that the value leaves out) is not grounded at all, and what a
hostile page made the model write beyond the page text never reaches the
cache.

An entry that is read (``filtered_answer``). Grounding depends on the
configuration (``grounding_min_similarity``) as well as on the page, so it
is applied to a new answer and to every draft, but it never drops a value
of a hit. A value of a hit is scrubbed as it is, and that is the only way it
is ever rewritten; it is dropped only when it shows a secret or hides
private data. A hit is therefore stored again only when the scrubber or the
secret filter changed it (an entry written before they knew a case), and a
change of the configuration never rewrites the cache: lowering a threshold
that was raised gives back what the stricter grounding left out.

Both kinds of values are dropped when some form of them that normalization
may read still shows private data (``PrivacyFilter.hides``), or when they,
or the part of the page they stand for, show a secret
(``seminars.llm.secret_filter``). Applied to their own result, both
functions change nothing, which is how a clean hit is told from one that
needs cleaning.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Final

from seminars.llm.grounding import Grounding
from seminars.llm.privacy_filter import PrivacyFilter, readable
from seminars.llm.secret_filter import SecretFilter
from seminars.models import Model

MAX_EVENTS_PER_CHUNK: Final = 100
"""Items of one answer that are read and cached; the rest is left out."""


class ExtractedSpeaker(Model):
    name: str
    affiliation: str | None = None


class ExtractedEvent(Model):
    """One talk as the model read it. Text is copied from the page; the rest is numbers."""

    title: str | None = None
    year: int | None = None
    month: int
    day: int
    start_hour: int | None = None
    start_minute: int | None = None
    end_hour: int | None = None
    end_minute: int | None = None
    end_month: int | None = None
    end_day: int | None = None
    speakers: list[ExtractedSpeaker] = []
    series: str | None = None
    location: str | None = None
    abstract: str | None = None
    link: int | None = None
    stream_link: int | None = None
    cancelled: bool = False


class ExtractionAnswer(Model):
    """The answer to one extraction request."""

    events: list[ExtractedEvent]


class CachedExtraction(Model):
    """What the cache holds for one piece of page text: the answer and when it was first read."""

    reference: date
    answer: ExtractionAnswer


@dataclass(frozen=True)
class PieceForms:
    """One piece of page text in the forms the values of its answer are checked against."""

    readable: str
    """The piece as normalization reads text before it scrubs it; private data still in."""

    scrubbed: str
    """``readable`` without private data: the text values are taken from."""

    grounding: Grounding
    """Over ``scrubbed``, with the configured similarity."""


def piece_forms(chunk: str, privacy: PrivacyFilter, *, min_similarity: int) -> PieceForms:
    """``chunk`` in the forms of ``PieceForms``; grounding with ``min_similarity``."""
    text = readable(chunk)
    scrubbed = privacy.scrub(text)
    grounding = Grounding(scrubbed, min_similarity=min_similarity)
    return PieceForms(readable=text, scrubbed=scrubbed, grounding=grounding)


@dataclass(frozen=True)
class AnswerFilter:
    """What every text value of an answer goes through before it is cached."""

    privacy: PrivacyFilter
    secrets: SecretFilter

    def _safe(self, value: str, part: str | None) -> bool:
        """Whether ``value``, whose part of the page is ``part``, may be cached.

        It must hide no private data, and neither it nor ``part`` may show a
        secret.
        """
        if self.privacy.hides(value) or self.secrets.shows(value):
            return False
        return part is None or part == value or not self.secrets.shows(part)

    def new_value(self, value: str | None, piece: PieceForms) -> str | None:
        """A value of a new answer as it is cached; ``None`` when it may not be cached.

        See the module docstring: read and scrubbed as normalization does
        it, and grounded in the scrubbed piece.
        """
        if value is None:
            return None
        cleaned = self.privacy.clean(value)
        found = piece.grounding.text(cleaned)
        if found is None:
            return None
        return cleaned if self._safe(cleaned, found) else None

    def kept_value(self, value: str | None, piece: PieceForms) -> str | None:
        """A value of a cached answer as it may stay in the cache; ``None`` when it may not.

        It is scrubbed as it is. A value that was nothing but private data
        goes; one that was empty already stays as it is, so that it is not
        stored again.
        """
        if value is None:
            return None
        scrubbed = self.privacy.scrub(value)
        if scrubbed != value and not scrubbed.strip():
            return None
        part = piece.grounding.text(scrubbed)
        return scrubbed if self._safe(scrubbed, part) else None


type _Check = Callable[[str | None], str | None]
"""A text value in, the value as it may be cached out (``None``: not at all)."""


def _cleaned_item(item: ExtractedEvent, kept: _Check) -> ExtractedEvent:
    """``item`` with every text value as ``kept`` gives it; a speaker without a name goes."""
    speakers = [
        speaker.evolve(name=name, affiliation=kept(speaker.affiliation))
        for speaker in item.speakers
        if (name := kept(speaker.name)) is not None
    ]
    return item.evolve(
        title=kept(item.title),
        speakers=speakers,
        series=kept(item.series),
        location=kept(item.location),
        abstract=kept(item.abstract),
    )


def grounded_answer(
    answer: ExtractionAnswer, piece: PieceForms, filters: AnswerFilter
) -> ExtractionAnswer:
    """A new ``answer`` for ``piece`` as it is cached: the items that are read, values checked."""

    def kept(value: str | None) -> str | None:
        return filters.new_value(value, piece)

    events = answer.events[:MAX_EVENTS_PER_CHUNK]
    return answer.evolve(events=[_cleaned_item(item, kept) for item in events])


def filtered_answer(
    answer: ExtractionAnswer, piece: PieceForms, filters: AnswerFilter
) -> ExtractionAnswer:
    """A cached ``answer`` for ``piece`` as it may stay in the cache (see the module docstring)."""

    def kept(value: str | None) -> str | None:
        return filters.kept_value(value, piece)

    return answer.evolve(events=[_cleaned_item(item, kept) for item in answer.events])
