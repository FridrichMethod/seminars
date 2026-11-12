"""The answer to a page extraction request, and what of it may be cached.

The page extractor (``seminars.sources.generic.llm_page``) asks the model for
one item per talk and caches the answer by the piece of page text it sent.
data/llm_cache/ is committed to a public repository, so every text value of
an answer (title, speaker name and affiliation, series, location, abstract)
is cleaned before the answer is stored:

* it goes through the scrubber of normalization (``AnswerFilter.scrub``,
  see ``seminars.llm.privacy_filter``), so no meeting code, e-mail address
  or telephone number is stored, and the rest of the value stays;
* it is dropped when it shows a secret, as it is or in the part of the page
  it stands for (``AnswerFilter.secrets``, see ``seminars.llm.secret_filter``);
* in a new answer, it is dropped when the page lacks it. The page here is
  the piece of text with its private data scrubbed the same way, so a
  scrubbed value still matches the page, and a value that is private only
  in the words around it does not (an address spelled "jdoe at
  stanford.edu" after an "E-mail:" that the value leaves out). What a
  hostile page made the model write beyond the page text never reaches the
  cache.

Grounding depends on the configuration (``grounding_min_similarity``) as well
as on the page, so it is applied to a new answer and again to every draft
made from an answer, but never to an entry that is read: ``filtered_answer``,
which cleans a hit, only scrubs and drops secrets. A hit is therefore stored
again only when the scrubber or the secret filter changed it (an entry
written before they knew a case), and a change of the configuration never
rewrites the cache: lowering a threshold that was raised gives back what the
stricter grounding left out. Applied to their own result, both functions
change nothing, which is how a clean hit is told from one that needs
cleaning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

from seminars.llm.grounding import Grounding
from seminars.llm.privacy_filter import Scrubber
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
class AnswerFilter:
    """What every text value of an answer goes through before it is cached."""

    scrub: Scrubber
    secrets: SecretFilter

    def value(self, value: str | None, grounding: Grounding, *, grounded: bool) -> str | None:
        """``value`` as it may be cached; ``None`` when it may not be cached at all.

        ``grounding`` is over the scrubbed piece of text. With ``grounded``,
        a value that the page lacks is dropped as well.
        """
        if value is None:
            return None
        scrubbed = self.scrub(value)
        if not scrubbed.strip():
            return None
        found = grounding.text(scrubbed)
        if found is None and grounded:
            return None
        if self.secrets.shows(scrubbed) or (found is not None and self.secrets.shows(found)):
            return None
        return scrubbed


def _cleaned_item(
    item: ExtractedEvent, grounding: Grounding, filters: AnswerFilter, *, grounded: bool
) -> ExtractedEvent:
    """``item`` with every text value as ``AnswerFilter.value`` gives it.

    A speaker whose name goes is left out.
    """

    def kept(value: str | None) -> str | None:
        return filters.value(value, grounding, grounded=grounded)

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
    answer: ExtractionAnswer, grounding: Grounding, filters: AnswerFilter
) -> ExtractionAnswer:
    """A new ``answer`` as it is cached: the items that are read, cleaned and grounded.

    ``grounding`` is over the piece of text the answer is for, scrubbed by
    ``filters.scrub``. Values are kept scrubbed but otherwise as the model
    wrote them, so that a draft grounds them again when it is made.
    """
    events = answer.events[:MAX_EVENTS_PER_CHUNK]
    items = [_cleaned_item(item, grounding, filters, grounded=True) for item in events]
    return answer.evolve(events=items)


def filtered_answer(
    answer: ExtractionAnswer, grounding: Grounding, filters: AnswerFilter
) -> ExtractionAnswer:
    """A cached ``answer`` as it may stay in the cache: scrubbed and without secrets.

    Nothing is dropped for grounding (see the module docstring);
    ``grounding`` only gives the part of the page a value stands for, which
    must not show a secret either.
    """
    items = [_cleaned_item(item, grounding, filters, grounded=False) for item in answer.events]
    return answer.evolve(events=items)
