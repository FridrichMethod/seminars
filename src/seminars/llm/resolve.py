"""From configuration and environment to the provider chain, and the probe behind llm-smoke.

``build_llm_client`` and ``probe_providers`` are the implementations of
``seminars.contracts.LLMClientFactory`` and ``LLMProber``.

Which providers join the chain. ``config.providers`` is the chain in order
of preference with the environment overrides applied
(``seminars.config.apply_llm_env``): LLM_PROVIDERS reorders and selects,
``codex-cli`` is only there when it was named, and inside GitHub Actions it
is never there. Of that list a provider joins when it is configured:

* ``claude-cli``: CLAUDE_CODE_OAUTH_TOKEN is set. Without a configured model
  the default of the CLI is used.
* ``codex-cli``: nothing to configure; it uses the ChatGPT login of the
  machine, which is checked when the first request is made. Dropped inside
  GitHub Actions here as well, whatever the configuration says.
* ``anthropic`` / ``openai``: the API key is set and a model is configured.
* ``openai-compat``: LLM_API_KEY, the address (LLM_BASE_URL or ``base_url``)
  and a model (LLM_MODEL_OPENAI_COMPAT) are all there.

Credentials are read from the given environment, stripped, and handed to
the backend that needs them. They are never logged, and building a chain
runs no command and sends no request. A chain without any provider is
``None``: rules-only mode.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from seminars.config import (
    ENV_GITHUB_ACTIONS,
    ENV_LLM_BASE_URL,
    LOCAL_ONLY_PROVIDERS,
    PROVIDER_CREDENTIAL_ENV,
    LLMConfig,
    ProviderId,
    model_env_var,
)
from seminars.contracts import LLMChainClient, LLMError
from seminars.llm.api import AnthropicProvider, OpenAICompatProvider, OpenAIProvider
from seminars.llm.base import Provider
from seminars.llm.chain import ProviderChain
from seminars.llm.claude_cli import ClaudeCliProvider
from seminars.llm.cli import CliRunner, run_cli
from seminars.llm.codex_cli import CodexCliProvider
from seminars.models import LLMProviderResult, Model

logger: Final = logging.getLogger(__name__)

PROBE_SYSTEM: Final = 'This is a connection check. Answer with the JSON object {"ok": true}.'
PROBE_USER: Final = "ping"
PROBE_MAX_TOKENS: Final = 512
"""Room for the probe answer; models that think before they answer need more than the answer."""


class ProbeAnswer(Model):
    """The answer to the probe request: a closed vocabulary, so every backend may give it."""

    ok: bool


@dataclass(frozen=True)
class _Resolution:
    """The backend of one provider id, or the reason why it is not configured."""

    provider: Provider | None = None
    reason: str | None = None


def _in_github_actions(env: Mapping[str, str]) -> bool:
    return env.get(ENV_GITHUB_ACTIONS, "").strip().lower() == "true"


def _resolve(
    provider_id: ProviderId, config: LLMConfig, env: Mapping[str, str], runner: CliRunner
) -> _Resolution:
    """Build the backend for ``provider_id`` when everything it needs is there."""
    if provider_id in LOCAL_ONLY_PROVIDERS and _in_github_actions(env):
        return _Resolution(reason="never used inside GitHub Actions")
    credential_env = PROVIDER_CREDENTIAL_ENV[provider_id]
    credential = env.get(credential_env, "").strip() if credential_env else ""
    if credential_env and not credential:
        return _Resolution(reason=f"{credential_env} is not set")
    model = config.model_for(provider_id)
    timeout = config.timeout_seconds
    if provider_id == "claude-cli":
        version = config.cli_versions.get("claude-cli")
        return _Resolution(
            ClaudeCliProvider(
                model=model,
                token=credential,
                timeout=timeout,
                env=env,
                verified_version=version,
                runner=runner,
            )
        )
    if provider_id == "codex-cli":
        version = config.cli_versions.get("codex-cli")
        return _Resolution(
            CodexCliProvider(
                model=model, timeout=timeout, env=env, verified_version=version, runner=runner
            )
        )
    if not model:
        variable = model_env_var(provider_id)
        return _Resolution(reason=f"no model is set (config/llm.yaml or {variable})")
    if provider_id == "anthropic":
        return _Resolution(AnthropicProvider(model=model, api_key=credential, timeout=timeout))
    if provider_id == "openai":
        return _Resolution(OpenAIProvider(model=model, api_key=credential, timeout=timeout))
    if not config.base_url:
        return _Resolution(reason=f"{ENV_LLM_BASE_URL} is not set")
    return _Resolution(
        OpenAICompatProvider(
            model=model, api_key=credential, base_url=config.base_url, timeout=timeout
        )
    )


def _safely_resolved(
    provider_id: ProviderId, config: LLMConfig, env: Mapping[str, str], runner: CliRunner
) -> _Resolution:
    """``_resolve`` that reports a backend which cannot even be built instead of raising."""
    try:
        return _resolve(provider_id, config, env, runner)
    except Exception as error:  # a broken provider must not take the run down
        logger.warning("LLM provider %s cannot be set up (%s)", provider_id, type(error).__name__)
        return _Resolution(reason=f"cannot be set up ({type(error).__name__})")


def build_providers(
    config: LLMConfig, env: Mapping[str, str], *, runner: CliRunner = run_cli
) -> list[Provider]:
    """The configured backends of ``config.providers``, in order, each at most once.

    ``runner`` runs the command line clients; tests pass a fake.
    """
    providers: list[Provider] = []
    for provider_id in dict.fromkeys(config.providers):
        resolution = _safely_resolved(provider_id, config, env, runner)
        if resolution.provider is not None:
            providers.append(resolution.provider)
    return providers


def build_llm_client(config: LLMConfig, env: Mapping[str, str]) -> LLMChainClient | None:
    """The provider chain for one run; ``None`` when no provider is configured.

    ``config`` has the environment overrides applied already. Never raises
    for a missing or broken provider: such a provider is left out, or fails
    when it is first asked and the chain moves on.
    """
    providers = build_providers(config, env)
    if not providers:
        return None
    return ProviderChain(
        providers, max_calls=config.max_calls_per_run, max_input_chars=config.max_input_chars
    )


def _probe(provider: Provider) -> LLMProviderResult:
    """Send the probe request to one backend and describe the outcome."""
    try:
        completion = provider.complete(
            PROBE_SYSTEM, PROBE_USER, ProbeAnswer, max_tokens=PROBE_MAX_TOKENS
        )
    except LLMError as error:
        kind, message = error.failure_kind, str(error)
    except Exception as error:  # the report must come out whatever a backend does
        kind, message = "other", f"{provider.name}: unexpected {type(error).__name__}"
    else:
        return LLMProviderResult(
            provider=provider.name,
            model=provider.model,
            status="ok",
            calls=1,
            warnings=list(completion.warnings),
        )
    return LLMProviderResult(
        provider=provider.name,
        model=provider.model,
        status="failed",
        calls=1,
        failures=1,
        failure_kind=kind,
        error=message,
    )


def probe_providers(
    config: LLMConfig, env: Mapping[str, str], *, runner: CliRunner = run_cli
) -> list[LLMProviderResult]:
    """One cheap request to every provider of ``config.providers``; one result each.

    Unlike the chain, the probe does not stop at the first provider that
    answers: ``seminars llm-smoke`` wants to know the state of all of them.
    A provider that is not configured is reported as ``unused`` with the
    reason as a warning, and nothing is sent to it.
    """
    results: list[LLMProviderResult] = []
    for provider_id in dict.fromkeys(config.providers):
        resolution = _safely_resolved(provider_id, config, env, runner)
        if resolution.provider is None:
            results.append(
                LLMProviderResult(
                    provider=provider_id,
                    model=config.model_for(provider_id),
                    status="unused",
                    warnings=[f"not configured: {resolution.reason}"],
                )
            )
        else:
            results.append(_probe(resolution.provider))
    return results
