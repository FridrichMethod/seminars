"""Fakes for the LLM-side contracts: client, cache, LLM tagger and page extractor."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self

from pydantic import BaseModel, ValidationError

from seminars.contracts import LLMError, LLMInvalidOutputError
from seminars.models import (
    Candidate,
    EventDraft,
    LLMFailureKind,
    LLMProviderResult,
    LLMTagResult,
    LLMUsage,
    RawPayload,
    SourceConfig,
)


@dataclass(frozen=True)
class LLMCall:
    """One recorded call of ``FakeLLMClient.complete_json``."""

    system: str
    user: str
    schema: type[BaseModel]
    max_tokens: int


Answer = BaseModel | Mapping[str, Any]
Scripted = Answer | Exception | Callable[[LLMCall], Answer]
"""A scripted reply: a model or a plain mapping (validated against the requested
schema), an exception to raise, or a function computing the reply from the call."""


class FakeLLMClient:
    """Scripted stand-in for an LLM client or the whole provider chain.

    Replies are consumed in order; ``always`` sets the reply used once the
    script is empty; ``fail`` makes the next calls raise. A mapping that does
    not validate raises ``LLMInvalidOutputError``, exactly like a real
    client. An unscripted call is a test bug and raises ``AssertionError``.

        llm = FakeLLMClient({"fields": ["bio"]}).fail(LLMRateLimitError("slow down"))
    """

    def __init__(self, *replies: Scripted, name: str = "fake-llm") -> None:
        self.name = name
        self.calls: list[LLMCall] = []
        self._script: deque[Scripted] = deque(replies)
        self._fallback: Scripted | None = None
        self._failures: dict[LLMFailureKind, int] = {}

    def script(self, *replies: Scripted) -> Self:
        """Queue more replies behind the ones already scripted."""
        self._script.extend(replies)
        return self

    def always(self, reply: Scripted) -> Self:
        """Reply used for every call once the script is exhausted."""
        self._fallback = reply
        return self

    def fail(self, error: Exception, *, times: int = 1) -> Self:
        """Make the next ``times`` calls raise ``error`` before the script continues."""
        self._script.extendleft([error] * times)
        return self

    def complete_json[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> T:
        call = LLMCall(system=system, user=user, schema=schema, max_tokens=max_tokens)
        self.calls.append(call)
        if self._script:
            reply = self._script.popleft()
        elif self._fallback is not None:
            reply = self._fallback
        else:
            raise AssertionError(f"FakeLLMClient: call {len(self.calls)} has no scripted reply")
        try:
            return self._resolve(reply, call, schema)
        except LLMError as error:
            kind = error.failure_kind
            self._failures[kind] = self._failures.get(kind, 0) + 1
            raise

    def _resolve[T: BaseModel](self, reply: Scripted, call: LLMCall, schema: type[T]) -> T:
        if isinstance(reply, Exception):
            raise reply
        if callable(reply) and not isinstance(reply, BaseModel | Mapping):
            reply = reply(call)
        if isinstance(reply, schema):
            return reply
        data = reply.model_dump() if isinstance(reply, BaseModel) else dict(reply)
        try:
            return schema.model_validate(data)
        except ValidationError as error:
            raise LLMInvalidOutputError(
                f"reply does not match {schema.__name__}", provider=self.name
            ) from error

    def usage(self) -> LLMUsage:
        """Usage in the shape the real provider chain reports it."""
        failures = sum(self._failures.values())
        succeeded = len(self.calls) - failures
        status = "unused" if not self.calls else ("ok" if succeeded else "failed")
        result = LLMProviderResult(
            provider=self.name,
            status=status,
            calls=len(self.calls),
            failures=failures,
            failure_kind=next(iter(self._failures), None) if status == "failed" else None,
        )
        return LLMUsage(
            enabled=True,
            provider=self.name if succeeded else None,
            calls=len(self.calls),
            providers=[result],
        )


class FakeLLMCache:
    """Dictionary-backed ``LLMCache`` that counts hits, misses and writes."""

    def __init__(self, entries: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self.entries: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (entries or {}).items()}
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.prunes = 0

    def get(self, key: str) -> dict[str, Any] | None:
        value = self.entries.get(key)
        if value is None:
            self.misses += 1
            return None
        self.hits += 1
        return dict(value)

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        self.writes += 1
        self.entries[key] = dict(value)

    def prune(self) -> int:
        self.prunes += 1
        return 0


class FakeLLMFieldTagger:
    """``LLMFieldTagger`` answering from a fixed table keyed by event uid.

    Uids without an entry get no answer, which is what the real tagger does
    when the LLM is unavailable. ``batches`` records the uids of each call.
    """

    def __init__(self, answers: Mapping[str, LLMTagResult] | None = None) -> None:
        self.answers = dict(answers or {})
        self.batches: list[list[str]] = []

    def tag_batch(self, candidates: Sequence[Candidate]) -> dict[str, LLMTagResult]:
        uids = [candidate.event.uid for candidate in candidates]
        self.batches.append(uids)
        return {uid: self.answers[uid] for uid in uids if uid in self.answers}


class FakePageExtractor:
    """``PageExtractor`` returning preset drafts; records the source ids it was asked for."""

    def __init__(self, drafts: Sequence[EventDraft] = ()) -> None:
        self.drafts = list(drafts)
        self.requested: list[str] = []

    def extract(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        self.requested.append(source.id)
        return list(self.drafts)
