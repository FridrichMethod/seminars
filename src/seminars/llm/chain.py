"""The provider chain: several backends behind one ``LLMChainClient``.

A request goes to the first provider that is still usable. A provider
failure (credential, quota, rate limit, timeout, network; any
``LLMProviderError``) marks that provider unavailable for the rest of the
run and the request moves on to the next one. When none is left the chain
has reached its end, ``none``: the request raises ``LLMUnavailableError``
and the caller goes on with the rules only.

The probe. A run learns whether its first provider works from its first
request. A run that sends no request (every answer came from the cache)
would never learn it, and an expired credential would go unnoticed for as
long as the cache serves everything. ``probe`` covers that case: when the
run has sent nothing yet, it sends one cheap request (``ProbeAnswer``)
through the chain like any other request, so it counts against the cap of
the run, a provider that fails is marked unavailable and the next one is
asked, and the outcome is in ``usage``. With ``probe_when_idle`` (how
``seminars.llm.resolve.build_llm_client`` builds the chain) ``usage`` calls
``probe`` itself, so the run report never lacks the state of the chain.
``seminars llm-smoke`` probes every provider instead (``seminars.llm.resolve``).

An answer that does not validate (``LLMInvalidOutputError``) is discarded
and reported to the caller. It is not a reason to switch: the provider did
its part, and another provider would see the same input.

Two caps of the run are enforced here, before anything is sent: the number
of requests (every attempt at a provider counts) and the length of the user
prompt. A request over a cap raises ``LLMBudgetExceededError`` and is
counted for the run report. Callers that leave input out for its length
before they make a request report it with ``record_skipped_too_long``
(``seminars.llm.base.SkipRecorder``), so the report has those as well.

``restricted`` gives a view of the same chain without some providers. The
view shares everything else with the chain it came from: caps, failures
and usage. A provider can also decline a kind of request by itself
(``Provider.serves``; the codex CLI only takes closed vocabularies); it is
then passed over for that request without counting as failed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Final

from pydantic import BaseModel

from seminars.contracts import (
    LLMBudgetExceededError,
    LLMError,
    LLMInvalidOutputError,
)
from seminars.llm.base import Completion, NoneProvider, Provider
from seminars.llm.probe import PROBE_MAX_TOKENS, PROBE_SYSTEM, PROBE_USER, ProbeAnswer
from seminars.llm.schema import strict_json_schema
from seminars.models import LLMFailureKind, LLMProviderResult, LLMProviderStatus, LLMUsage
from seminars.textrules import ascii_printable

logger: Final = logging.getLogger(__name__)


@dataclass
class _ProviderRecord:
    """What happened to one provider so far in this run."""

    calls: int = 0
    failures: int = 0
    successes: int = 0
    unavailable: bool = False
    failure_kind: LLMFailureKind | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def status(self) -> LLMProviderStatus:
        if self.calls == 0:
            return "unused"
        return "ok" if self.successes and not self.unavailable else "failed"


@dataclass
class _RunState:
    """The state of one run, shared by a chain and all of its restricted views."""

    max_calls: int
    probe_when_idle: bool = False
    probed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    records: dict[str, _ProviderRecord] = field(default_factory=dict)
    calls: int = 0
    skipped_call_cap: int = 0
    skipped_too_long: int = 0
    last_provider: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class ProviderChain:
    """``LLMChainClient`` over ``providers`` in order of preference; see the module docstring.

    ``max_calls`` is the cap of requests for the run and ``max_input_chars``
    the longest user prompt that is sent (``LLMConfig.max_calls_per_run`` and
    ``max_input_chars``). With ``probe_when_idle``, ``usage`` probes first
    when the run has sent no request (see the module). Safe to use from
    several threads.
    """

    def __init__(
        self,
        providers: Sequence[Provider],
        *,
        max_calls: int,
        max_input_chars: int,
        probe_when_idle: bool = False,
        _state: _RunState | None = None,
        _excluded: frozenset[str] = frozenset(),
    ) -> None:
        self._providers = tuple(providers)
        self._max_input_chars = max_input_chars
        self._state = _state or _RunState(max_calls=max_calls, probe_when_idle=probe_when_idle)
        self._excluded = _excluded
        self._end = NoneProvider()
        for provider in self._providers:
            self._state.records.setdefault(provider.name, _ProviderRecord())

    @property
    def name(self) -> str:
        """The provider that would be asked next; ``none`` when no provider is left."""
        with self._state.lock:
            for provider in self._providers:
                usable = not self._state.records[provider.name].unavailable
                if usable and provider.name not in self._excluded:
                    return provider.name
        return self._end.name

    def restricted(self, exclude: Collection[str]) -> ProviderChain:
        """A view of this chain that never sends a request to the providers in ``exclude``."""
        return ProviderChain(
            self._providers,
            max_calls=self._state.max_calls,
            max_input_chars=self._max_input_chars,
            probe_when_idle=self._state.probe_when_idle,
            _state=self._state,
            _excluded=self._excluded | frozenset(exclude),
        )

    def _claim(self, provider: Provider) -> bool:
        """Reserve one request for ``provider``; False when it is no longer usable."""
        state = self._state
        with state.lock:
            record = state.records[provider.name]
            if record.unavailable:
                return False
            if state.calls >= state.max_calls:
                state.skipped_call_cap += 1
                raise LLMBudgetExceededError(
                    f"the call cap of the run ({state.max_calls}) is reached"
                )
            state.calls += 1
            record.calls += 1
            return True

    def _succeeded(self, provider: Provider, completion: Completion[BaseModel]) -> None:
        state = self._state
        with state.lock:
            record = state.records[provider.name]
            record.successes += 1
            for warning in completion.warnings:
                if warning not in record.warnings:
                    record.warnings.append(warning)
            state.last_provider = provider.name
            state.input_tokens += completion.input_tokens
            state.output_tokens += completion.output_tokens
            state.cost_usd += completion.cost_usd

    def _failed(
        self, provider: Provider, kind: LLMFailureKind, error: str, *, unavailable: bool
    ) -> None:
        with self._state.lock:
            record = self._state.records[provider.name]
            record.failures += 1
            record.failure_kind = kind
            record.error = ascii_printable(error)
            record.unavailable = record.unavailable or unavailable
        if unavailable:
            logger.warning(
                "LLM provider %s failed (%s) and is not used again in this run",
                provider.name,
                kind,
            )

    def complete_json[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> T:
        """One validated answer from the first provider that can give it.

        Raises ``LLMBudgetExceededError``, ``LLMInvalidOutputError`` or
        ``LLMUnavailableError`` as the contract says, and ``ValueError`` for
        a schema that cannot be sent to any backend (a mistake of the
        caller, raised before anything is sent).
        """
        strict_json_schema(schema)
        if len(user) > self._max_input_chars:
            with self._state.lock:
                self._state.skipped_too_long += 1
            raise LLMBudgetExceededError(
                f"an input of {len(user)} characters exceeds max_input_chars "
                f"({self._max_input_chars})"
            )
        for provider in self._providers:
            if provider.name in self._excluded or not provider.serves(schema):
                continue
            if not self._claim(provider):
                continue
            try:
                completion = provider.complete(system, user, schema, max_tokens=max_tokens)
            except LLMInvalidOutputError as error:
                self._failed(provider, error.failure_kind, str(error), unavailable=False)
                raise
            except LLMError as error:
                self._failed(provider, error.failure_kind, str(error), unavailable=True)
            except Exception as error:
                message = f"{provider.name}: unexpected {type(error).__name__}"
                self._failed(provider, "other", message, unavailable=True)
            else:
                self._succeeded(provider, completion)
                return completion.value
        return self._end.complete_json(system, user, schema, max_tokens=max_tokens)

    def probe(self) -> None:
        """Send the probe request when the run has sent no request yet; only the first time.

        A run that has sent a request has had its probe. The probe request
        goes through the chain like any other (see the module) and never
        raises: its outcome is in ``usage``.
        """
        state = self._state
        with state.lock:
            idle = not state.probed and state.calls == 0 and state.max_calls > 0
            state.probed = True
        if not idle:
            return
        try:
            self.complete_json(PROBE_SYSTEM, PROBE_USER, ProbeAnswer, max_tokens=PROBE_MAX_TOKENS)
        except LLMError as error:
            logger.info("LLM probe: no provider answered (%s)", type(error).__name__)

    def record_skipped_too_long(self, count: int) -> None:
        """Count requests a caller did not make because the input is too long (``SkipRecorder``)."""
        if count < 0:
            raise ValueError("a count of skipped requests cannot be negative")
        with self._state.lock:
            self._state.skipped_too_long += count

    def usage(self) -> LLMUsage:
        """The LLM activity of the run so far, for the run report and health.

        With ``probe_when_idle`` a run that has sent no request yet sends the
        probe request first (``probe``), once.
        """
        if self._state.probe_when_idle:
            self.probe()
        state = self._state
        with state.lock:
            results = [
                self._result(provider, state.records[provider.name]) for provider in self._providers
            ]
            return LLMUsage(
                enabled=True,
                provider=state.last_provider,
                calls=state.calls,
                skipped_call_cap=state.skipped_call_cap,
                skipped_too_long=state.skipped_too_long,
                input_tokens=state.input_tokens,
                output_tokens=state.output_tokens,
                cost_usd=state.cost_usd,
                providers=results,
            )

    @staticmethod
    def _result(provider: Provider, record: _ProviderRecord) -> LLMProviderResult:
        failed = record.status == "failed"
        return LLMProviderResult(
            provider=provider.name,
            model=provider.model,
            status=record.status,
            calls=record.calls,
            failures=record.failures,
            failure_kind=record.failure_kind if failed else None,
            error=record.error if failed else None,
            warnings=list(record.warnings),
        )
