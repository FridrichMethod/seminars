"""Persistent run history (data/health.json) and the uid map (data/uid_map.json)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, Field

from seminars.models.base import Model, Timestamp, Uid, sorted_dict
from seminars.models.report import Count, LLMFailureKind, LLMProviderStatus, SourceStatus

HEALTH_HISTORY_LIMIT: Final = 30
"""Number of most recent runs kept per source and per LLM provider."""


class SourceHealthEntry(Model):
    """One run of one source."""

    at: Timestamp  # start time of the run
    status: SourceStatus
    events: Count = 0
    error: str | None = None


class LLMHealthEntry(Model):
    """One run of one LLM provider."""

    at: Timestamp
    status: LLMProviderStatus
    calls: Count = 0
    failure_kind: LLMFailureKind | None = None
    error: str | None = None


def _trailing[E: (SourceHealthEntry, LLMHealthEntry)](history: list[E], statuses: set[str]) -> int:
    count = 0
    for entry in reversed(history):
        if entry.status not in statuses:
            break
        count += 1
    return count


class SourceHealth(Model):
    """History of one source, oldest run first, at most HEALTH_HISTORY_LIMIT runs."""

    history: Annotated[list[SourceHealthEntry], Field(max_length=HEALTH_HISTORY_LIMIT)] = []
    last_success_at: Timestamp | None = None  # last run with status "ok"; survives the cap

    def trailing(self, *statuses: SourceStatus) -> int:
        """Number of most recent consecutive runs whose status is one of ``statuses``."""
        return _trailing(self.history, set(statuses))

    @property
    def last(self) -> SourceHealthEntry | None:
        return self.history[-1] if self.history else None


class LLMProviderHealth(Model):
    """History of one LLM provider, oldest run first, at most HEALTH_HISTORY_LIMIT runs."""

    history: Annotated[list[LLMHealthEntry], Field(max_length=HEALTH_HISTORY_LIMIT)] = []
    last_success_at: Timestamp | None = None

    def trailing(self, *statuses: LLMProviderStatus) -> int:
        """Number of most recent consecutive runs whose status is one of ``statuses``."""
        return _trailing(self.history, set(statuses))


class HealthFile(Model):
    """Content of data/health.json: run history per source and per LLM provider."""

    schema_version: Literal[1] = 1
    sources: Annotated[dict[str, SourceHealth], AfterValidator(sorted_dict)] = {}
    llm: Annotated[dict[str, LLMProviderHealth], AfterValidator(sorted_dict)] = {}

    def with_source_entry(self, source_id: str, entry: SourceHealthEntry) -> Self:
        """Return a copy with ``entry`` appended to the capped history of ``source_id``."""
        current = self.sources.get(source_id, SourceHealth())
        updated = SourceHealth(
            history=[*current.history, entry][-HEALTH_HISTORY_LIMIT:],
            last_success_at=entry.at if entry.status == "ok" else current.last_success_at,
        )
        return self.evolve(sources={**self.sources, source_id: updated})

    def with_llm_entry(self, provider: str, entry: LLMHealthEntry) -> Self:
        """Return a copy with ``entry`` appended to the capped history of ``provider``."""
        current = self.llm.get(provider, LLMProviderHealth())
        updated = LLMProviderHealth(
            history=[*current.history, entry][-HEALTH_HISTORY_LIMIT:],
            last_success_at=entry.at if entry.status == "ok" else current.last_success_at,
        )
        return self.evolve(llm={**self.llm, provider: updated})


class UidMap(Model):
    """Content of data/uid_map.json: every source key ever seen, mapped to its uid.

    Keys are ``Event.source_ids`` entries (``<source id>:<native id>`` or a
    derived key). A run looks every incoming key up here first, which is what
    keeps uids stable across title changes, time changes and merges.
    """

    schema_version: Literal[1] = 1
    aliases: Annotated[dict[str, Uid], AfterValidator(sorted_dict)] = {}

    def resolve(self, key: str) -> str | None:
        """The uid ``key`` is bound to, or ``None`` when the key is new."""
        return self.aliases.get(key)

    def with_aliases(self, aliases: Mapping[str, str]) -> Self:
        """Return a copy with ``aliases`` added.

        An existing key is rebound, which is what a merge needs: the keys of
        the absorbed event move to the surviving (earliest seen) uid.
        """
        return self.evolve(aliases={**self.aliases, **aliases})
