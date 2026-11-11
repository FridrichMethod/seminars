"""The API backends: ``anthropic``, ``openai`` and ``openai-compat``, paid per request.

All three go through the official Python SDKs and send no tools, so the
model can only answer with text:

* ``anthropic``: the Messages API with ``output_config.format`` set to the
  JSON schema of the request.
* ``openai``: the Responses API with ``text.format`` set to the schema in
  strict mode; ``store`` is off, so the request is not kept by the service.
* ``openai-compat``: Chat Completions at ``LLM_BASE_URL``, for any service
  that speaks the OpenAI protocol. Such services differ in what they
  enforce, so only the widely supported ``json_object`` response format is
  requested and the schema is written into the system message.

Whatever a service enforces, the answer is validated here (``parse_answer``).

An SDK error carries the message of the service, which can quote the request
or part of the key. It is read to pick the failure kind and then dropped:
the error raised here has a fixed sentence and the HTTP status, and no
cause. The SDKs retry a failed request a few times on their own (with the
pause the service asks for) before the failure reaches the provider chain.

None of these backends was called for real when this was written: the
machines that run the tests have no API key. tests/test_llm_api.py checks
them against a mock HTTP transport, with the real SDK on both sides of it.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Any, Final

import anthropic
import openai
from pydantic import BaseModel

from seminars.contracts import (
    LLMError,
    LLMInvalidOutputError,
    LLMNetworkError,
    LLMProviderError,
    LLMTimeoutError,
)
from seminars.llm.base import Completion, ProviderBase
from seminars.llm.failures import failure_for_status, provider_failure
from seminars.llm.schema import parse_answer, strict_json_schema

API_MAX_RETRIES: Final = 2
"""How often an SDK repeats a request that failed in a way that may pass (its default)."""

SCHEMA_INSTRUCTION: Final = (
    "\n\nAnswer with one JSON object and nothing else. It must match this JSON schema:\n"
)
"""Appended to the system message for ``openai-compat``, followed by the schema."""

_ANTHROPIC_DONE: Final = frozenset({"end_turn", "stop_sequence"})
_PLAIN_WORD: Final = re.compile(r"[a-z_]{1,40}")
_NAME_CHARACTERS: Final = re.compile(r"[^A-Za-z0-9_-]")


@dataclass(frozen=True)
class _SdkErrors:
    """The exception classes of one SDK that decide the failure kind."""

    timeout: type[Exception]
    connection: type[Exception]
    status: type[Exception]


_ANTHROPIC_ERRORS: Final = _SdkErrors(
    anthropic.APITimeoutError, anthropic.APIConnectionError, anthropic.APIStatusError
)
_OPENAI_ERRORS: Final = _SdkErrors(
    openai.APITimeoutError, openai.APIConnectionError, openai.APIStatusError
)


def _failure(error: Exception, errors: _SdkErrors, provider: str) -> LLMProviderError:
    """The failure an SDK error stands for; its message is read, never passed on."""
    if isinstance(error, errors.timeout):  # a subclass of the connection error: test it first
        return provider_failure(LLMTimeoutError, provider)
    if isinstance(error, errors.connection):
        return provider_failure(LLMNetworkError, provider)
    status = getattr(error, "status_code", None)
    if isinstance(error, errors.status) and isinstance(status, int):
        return provider_failure(failure_for_status(status, str(error)), provider, f"HTTP {status}")
    return provider_failure(LLMProviderError, provider, type(error).__name__)


def _incomplete(reason: object, provider: str) -> LLMInvalidOutputError:
    """The error for an answer that the service did not finish (cut off, refused)."""
    word = reason if isinstance(reason, str) and _PLAIN_WORD.fullmatch(reason) else "unknown"
    return LLMInvalidOutputError(f"the answer is incomplete ({word})", provider=provider)


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class _ApiProvider(ProviderBase):
    """What the three API backends share: the settings and the lazily built SDK client."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        timeout: float,
        http_client: Any = None,
        max_retries: int = API_MAX_RETRIES,
    ) -> None:
        self._model = model
        self._api_key = api_key.strip()
        self._timeout = timeout
        self._http_client = http_client  # an httpx2.Client; tests pass one with a mock transport
        self._max_retries = max_retries
        self._lock = threading.Lock()
        self._sdk: Any = None

    @property
    def model(self) -> str:
        return self._model

    def _build(self) -> Any:
        raise NotImplementedError

    def _client(self) -> Any:
        with self._lock:
            if self._sdk is None:
                self._sdk = self._build()
            return self._sdk


class AnthropicProvider(_ApiProvider):
    """``Provider`` over the Anthropic Messages API; see the module docstring."""

    @property
    def name(self) -> str:
        return "anthropic"

    def _build(self) -> anthropic.Anthropic:
        return anthropic.Anthropic(
            api_key=self._api_key,
            timeout=self._timeout,
            max_retries=self._max_retries,
            http_client=self._http_client,
        )

    def complete[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> Completion[T]:
        strict = anthropic.transform_schema(strict_json_schema(schema))
        try:
            message = self._client().messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": strict}},
            )
        except LLMError:
            raise
        except Exception as error:
            raise _failure(error, _ANTHROPIC_ERRORS, self.name) from None
        if message.stop_reason not in _ANTHROPIC_DONE:
            raise _incomplete(message.stop_reason, self.name)
        text = "".join(block.text for block in message.content if block.type == "text")
        usage = message.usage
        tokens_in = (
            _count(usage.input_tokens)
            + _count(usage.cache_creation_input_tokens)
            + _count(usage.cache_read_input_tokens)
        )
        return Completion(
            value=parse_answer(text, schema, provider=self.name),
            input_tokens=tokens_in,
            output_tokens=_count(usage.output_tokens),
        )


class OpenAIProvider(_ApiProvider):
    """``Provider`` over the OpenAI Responses API; see the module docstring."""

    @property
    def name(self) -> str:
        return "openai"

    def _build(self) -> openai.OpenAI:
        return openai.OpenAI(
            api_key=self._api_key,
            timeout=self._timeout,
            max_retries=self._max_retries,
            http_client=self._http_client,
        )

    def complete[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> Completion[T]:
        strict = strict_json_schema(schema)
        schema_name = _NAME_CHARACTERS.sub("_", schema.__name__)[:64] or "answer"
        try:
            response = self._client().responses.create(
                model=self._model,
                instructions=system,
                input=user,
                max_output_tokens=max_tokens,
                store=False,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": strict,
                        "strict": True,
                    }
                },
            )
        except LLMError:
            raise
        except Exception as error:
            raise _failure(error, _OPENAI_ERRORS, self.name) from None
        if response.status != "completed":
            raise _incomplete(response.status, self.name)
        usage = response.usage
        return Completion(
            value=parse_answer(response.output_text, schema, provider=self.name),
            input_tokens=_count(getattr(usage, "input_tokens", 0)),
            output_tokens=_count(getattr(usage, "output_tokens", 0)),
        )


class OpenAICompatProvider(_ApiProvider):
    """``Provider`` over any service that speaks OpenAI Chat Completions at ``base_url``."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout: float,
        http_client: Any = None,
        max_retries: int = API_MAX_RETRIES,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            timeout=timeout,
            http_client=http_client,
            max_retries=max_retries,
        )
        self._base_url = base_url

    @property
    def name(self) -> str:
        return "openai-compat"

    def _build(self) -> openai.OpenAI:
        return openai.OpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=self._timeout,
            max_retries=self._max_retries,
            http_client=self._http_client,
        )

    def complete[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, max_tokens: int
    ) -> Completion[T]:
        instructions = system + SCHEMA_INSTRUCTION + json.dumps(strict_json_schema(schema))
        try:
            completion = self._client().chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": user},
                ],
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        except LLMError:
            raise
        except Exception as error:
            raise _failure(error, _OPENAI_ERRORS, self.name) from None
        choices = completion.choices or []
        if not choices:
            raise LLMInvalidOutputError("the response has no answer", provider=self.name)
        if choices[0].finish_reason not in ("stop", None):
            raise _incomplete(choices[0].finish_reason, self.name)
        usage = completion.usage
        return Completion(
            value=parse_answer(choices[0].message.content or "", schema, provider=self.name),
            input_tokens=_count(getattr(usage, "prompt_tokens", 0)),
            output_tokens=_count(getattr(usage, "completion_tokens", 0)),
        )
