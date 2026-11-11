"""What the backends of the LLM layer have in common.

A backend is a ``Provider``: an ``LLMClient`` (``seminars.contracts``) that
also reports what a request used. The provider chain talks to ``complete``
and adds the usage up for the run report; everything outside this package
only sees ``complete_json``.

The backends, by provider id:

==============  =====================================  ============================
id              class                                  credential
==============  =====================================  ============================
claude-cli      seminars.llm.claude_cli                CLAUDE_CODE_OAUTH_TOKEN
codex-cli       seminars.llm.codex_cli                 the local ChatGPT login
anthropic       seminars.llm.api (AnthropicProvider)   ANTHROPIC_API_KEY
openai          seminars.llm.api (OpenAIProvider)      OPENAI_API_KEY
openai-compat   seminars.llm.api (OpenAICompat...)     LLM_API_KEY and LLM_BASE_URL
none            NoneProvider (below)                   none
==============  =====================================  ============================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

from pydantic import BaseModel

from seminars.config import NO_PROVIDER
from seminars.contracts import LLMUnavailableError


@dataclass(frozen=True)
class Completion[T: BaseModel]:
    """One validated answer and what it cost.

    Token counts and cost are what the backend reported, 0 when it reports
    nothing. ``warnings`` are ASCII notes for the run report, such as a CLI
    version that differs from the verified one.
    """

    value: T
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    warnings: tuple[str, ...] = ()


class Provider(Protocol):
    """One backend, as the provider chain uses it."""

    @property
    def name(self) -> str:
        """Provider id, e.g. ``claude-cli``."""
        ...

    @property
    def model(self) -> str | None:
        """The configured model; ``None`` means the default of the backend."""
        ...

    def serves(self, schema: type[BaseModel]) -> bool:
        """Whether this backend may answer requests for ``schema`` at all."""
        ...

    def complete[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> Completion[T]:
        """One answer validated against ``schema``, with its usage; ``LLMError`` otherwise."""
        ...

    def complete_json[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> T:
        """``complete`` without the usage: the ``LLMClient`` method."""
        ...


class ProviderBase:
    """The parts of ``Provider`` that are the same for every backend."""

    def serves(self, schema: type[BaseModel]) -> bool:
        return True

    def complete[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> Completion[T]:
        raise NotImplementedError

    def complete_json[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> T:
        return self.complete(system, user, schema, max_tokens=max_tokens).value


class NoneProvider(ProviderBase):
    """The end of every chain: rules only. It never answers.

    ``complete`` raises ``LLMUnavailableError``, which tells the caller to
    go on without the LLM.
    """

    name: Final = NO_PROVIDER
    model: Final = None

    def complete[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> Completion[T]:
        raise LLMUnavailableError("no LLM provider is available; continuing with the rules only")
