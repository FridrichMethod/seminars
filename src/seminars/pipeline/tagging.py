"""The classification stage as the pipeline runs it: rules first, the LLM for open cases.

The rules and the LLM tagger belong to other packages and are reached
through their contracts (``FieldTagger``, ``LLMFieldTagger``). What happens
here is the order of things:

1. ``FieldTagger.tag`` decides every candidate from source defaults and
   rules.
2. Candidates the rules could not decide (``needs_llm``) and candidates the
   event filter was unsure about (``uncertain``) are sent to the LLM tagger
   in one call, if there is one. It batches and caches by itself.
3. An answer ``is_talk=False`` excludes the event with the reason
   ``not_a_talk``. Otherwise, when the rules had asked for help, the fields
   of the answer are used and the method is ``llm``; an answer without
   fields leaves the decision of the rules in place.
4. Without an answer the decision of the rules stands. One exception keeps
   the data steady while the LLM is away: an event that the rules cannot
   place at all, and that is stored with fields the LLM gave it in an
   earlier run, keeps those fields. A credential that expired would
   otherwise empty the feeds of everything only the LLM had recognised.

The LLM tagger never raises ``LLMError`` by contract. Should it raise one
anyway, or fail with any other error, the stage goes on as if there were
no tagger and says so in a warning: no LLM is no reason for a run to fail.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from seminars.contracts import FieldTagger, LLMFieldTagger
from seminars.models import (
    Candidate,
    Event,
    ExcludedEvent,
    LLMTagResult,
    TagDecision,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tagged:
    """Outcome of the stage: classified events, what the LLM ruled out, and warnings."""

    events: tuple[Event, ...] = ()
    excluded: tuple[ExcludedEvent, ...] = ()
    warnings: tuple[str, ...] = ()


def _ask(
    llm_tagger: LLMFieldTagger | None, open_cases: Sequence[Candidate]
) -> tuple[dict[str, LLMTagResult], list[str]]:
    if llm_tagger is None or not open_cases:
        return {}, []
    try:
        return dict(llm_tagger.tag_batch(open_cases)), []
    except Exception as error:  # the optional LLM must not fail the run
        logger.warning("the LLM tagger failed (%s)", type(error).__name__)
        return {}, [f"the LLM tagger failed ({type(error).__name__}); the rules decided alone"]


def _classified(
    candidate: Candidate,
    decision: TagDecision,
    answer: LLMTagResult | None,
    previous: Event | None,
) -> Event:
    event = candidate.event
    if answer is not None and decision.needs_llm and answer.fields:
        return event.evolve(fields=answer.fields, field_tag_method="llm")
    kept_from_llm = (
        answer is None
        and decision.needs_llm
        and not decision.fields
        and previous is not None
        and previous.field_tag_method == "llm"
    )
    if kept_from_llm and previous is not None:
        return event.evolve(fields=previous.fields, field_tag_method="llm")
    return event.evolve(fields=decision.fields, field_tag_method=decision.method)


def tag_candidates(
    candidates: Sequence[Candidate],
    *,
    tagger: FieldTagger,
    llm_tagger: LLMFieldTagger | None = None,
    uncertain: Collection[str] = (),
    previous: Mapping[str, Event] | None = None,
) -> Tagged:
    """Classify ``candidates`` (see the module docstring); the order is kept.

    ``uncertain`` holds the uids the event filter could not decide and
    ``previous`` the stored events by uid.
    """
    stored = previous or {}
    decisions = [tagger.tag(candidate) for candidate in candidates]
    open_cases = [
        candidate
        for candidate, decision in zip(candidates, decisions, strict=True)
        if decision.needs_llm or candidate.event.uid in uncertain
    ]
    answers, warnings = _ask(llm_tagger, open_cases)
    events: list[Event] = []
    excluded: list[ExcludedEvent] = []
    for candidate, decision in zip(candidates, decisions, strict=True):
        uid = candidate.event.uid
        answer = answers.get(uid)
        if answer is not None and not answer.is_talk:
            excluded.append(ExcludedEvent.from_event(candidate.event, "not_a_talk", "LLM tagger"))
        else:
            events.append(_classified(candidate, decision, answer, stored.get(uid)))
    return Tagged(tuple(events), tuple(excluded), tuple(warnings))
