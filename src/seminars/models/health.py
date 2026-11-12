"""Persistent run history (data/health.json) and the uid map (data/uid_map.json)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, Field

from seminars.models.base import Model, Timestamp, Uid, sorted_dict
from seminars.models.report import Count, LLMFailureKind, LLMProviderStatus, SourceStatus

HEALTH_HISTORY_LIMIT: Final = 30
"""Number of most recent runs kept per source and per LLM provider."""


class SourceHealthEntry(Model):
    """One run of one source.

    ``accepted`` is ``None`` in entries of failed runs and of runs recorded
    before it was kept. ``skipped_pages``, ``shrank_from`` and
    ``resized_from`` are those of ``SourceRunResult``.
    """

    at: Timestamp  # start time of the run
    status: SourceStatus
    events: Count = 0
    accepted: Count | None = None  # drafts that normalization accepted, before the window
    skipped_pages: Count = 0
    shrank_from: Count | None = None
    resized_from: Count | None = None
    error: str | None = None

    @property
    def read_in_full(self) -> bool:
        """Whether nothing was known to be missing from what the run delivered."""
        return self.skipped_pages == 0 and self.shrank_from is None


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


def _kept_until[E: (SourceHealthEntry, LLMHealthEntry)](
    history: list[E], last_success_at: datetime | None, moment: datetime
) -> tuple[list[E], datetime | None]:
    """The entries of runs that started at ``moment`` or before, and the last success among them.

    A last success after ``moment`` goes back to the last kept entry with
    status "ok", or to ``None`` when the capped history has none.
    """
    kept = [entry for entry in history if entry.at <= moment]
    if last_success_at is None or last_success_at <= moment:
        return kept, last_success_at
    successes = [entry.at for entry in kept if entry.status == "ok"]
    return kept, max(successes, default=None)


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

    def until(self, moment: datetime) -> Self:
        """The history without the runs that started after ``moment``."""
        history, success = _kept_until(self.history, self.last_success_at, moment)
        return self.evolve(history=history, last_success_at=success)


class LLMProviderHealth(Model):
    """History of one LLM provider, oldest run first, at most HEALTH_HISTORY_LIMIT runs."""

    history: Annotated[list[LLMHealthEntry], Field(max_length=HEALTH_HISTORY_LIMIT)] = []
    last_success_at: Timestamp | None = None

    def trailing(self, *statuses: LLMProviderStatus) -> int:
        """Number of most recent consecutive runs whose status is one of ``statuses``."""
        return _trailing(self.history, set(statuses))

    def until(self, moment: datetime) -> Self:
        """The history without the runs that started after ``moment``."""
        history, success = _kept_until(self.history, self.last_success_at, moment)
        return self.evolve(history=history, last_success_at=success)


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

    def until(self, moment: datetime) -> Self:
        """The history as the run that started at ``moment`` left it.

        Every entry of a run that started later is dropped: such a run
        wrote data/health.json but did not finish (see
        ``seminars.pipeline.store``).
        """
        return self.evolve(
            sources={key: value.until(moment) for key, value in self.sources.items()},
            llm={key: value.until(moment) for key, value in self.llm.items()},
        )

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

    Build every key with ``seminars.models.source_key``, for lookups as well
    as for new aliases: it escapes the native id to plain ASCII, so the keys
    in this file pass the charset rule of data/ and a key always finds the
    alias that was stored for it.
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
