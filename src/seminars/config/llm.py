"""Schema of config/llm.yaml and the environment overrides of the LLM layer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Final, Literal, get_args

from pydantic import Field

from seminars.models import Model
from seminars.models.base import HttpUrlStr

ProviderId = Literal["claude-cli", "codex-cli", "anthropic", "openai", "openai-compat"]
CliProviderId = Literal["claude-cli", "codex-cli"]

PROVIDER_IDS: Final[tuple[ProviderId, ...]] = get_args(ProviderId)
"""All providers in the default chain order: subscription CLIs first, then API keys."""

NO_PROVIDER: Final = "none"
"""Pseudo provider: rules only. Ends the chain when it appears in LLM_PROVIDERS."""

PROVIDER_CREDENTIAL_ENV: Final[dict[ProviderId, str | None]] = {
    "claude-cli": "CLAUDE_CODE_OAUTH_TOKEN",
    "codex-cli": None,  # uses the local ChatGPT login of the codex CLI, never a secret
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-compat": "LLM_API_KEY",
}
"""Environment variable holding the credential of each provider."""

OPT_IN_PROVIDERS: Final[frozenset[ProviderId]] = frozenset({"codex-cli"})
"""Providers that join the chain only when LLM_PROVIDERS (or --llm) names them."""

LOCAL_ONLY_PROVIDERS: Final[frozenset[ProviderId]] = frozenset({"codex-cli"})
"""Providers that are never used inside GitHub Actions."""

TAG_ONLY_PROVIDERS: Final[frozenset[ProviderId]] = frozenset({"codex-cli"})
"""Providers that may tag fields (closed vocabulary) but never extract free text.

The codex CLI cannot run without tools, so its output is only trusted when it
is an enumeration value.
"""

ENV_LLM_PROVIDERS: Final = "LLM_PROVIDERS"
ENV_LLM_BASE_URL: Final = "LLM_BASE_URL"
ENV_LLM_MODEL_PREFIX: Final = "LLM_MODEL_"
ENV_GITHUB_ACTIONS: Final = "GITHUB_ACTIONS"


def model_env_var(provider: str) -> str:
    """Name of the variable that overrides a provider's model, e.g. LLM_MODEL_CLAUDE_CLI."""
    return ENV_LLM_MODEL_PREFIX + provider.upper().replace("-", "_")


class LLMConfig(Model):
    """Content of config/llm.yaml (values are owned by the LLM layer).

    * ``providers``: the provider chain in order of preference.
    * ``models``: model name per provider; ``null`` or a missing key means "no
      model configured" (CLI providers then use the CLI default, API
      providers are skipped). Model names never live in code.
    * ``cli_versions``: the CLI version each CLI provider was verified with;
      Claude Code is installed at exactly this version in CI.
    * ``base_url``: endpoint of the ``openai-compat`` provider.
    * ``batch_size``: events per tagging request.
    * ``max_calls_per_run``: hard cap of provider requests in one run.
    * ``max_input_chars``: longest user prompt that is sent; longer inputs are
      skipped and reported.
    * ``timeout_seconds``: per request.
    * ``cache_ttl_days``: cache entries not hit for this long are pruned.
    * ``grounding_min_similarity``: fuzzy ratio (0-100) an extracted text
      field needs against the page text when it is not a plain substring.

    Environment overrides (see ``apply_llm_env``): LLM_PROVIDERS,
    LLM_MODEL_<PROVIDER> and LLM_BASE_URL.
    """

    providers: list[ProviderId] = list(PROVIDER_IDS)
    models: dict[ProviderId, str | None] = {}
    cli_versions: dict[CliProviderId, str | None] = {}
    base_url: HttpUrlStr | None = None
    batch_size: Annotated[int, Field(ge=1)] = 20
    max_calls_per_run: Annotated[int, Field(ge=0)] = 60
    max_input_chars: Annotated[int, Field(ge=1)] = 12000
    timeout_seconds: Annotated[float, Field(gt=0)] = 120.0
    cache_ttl_days: Annotated[int, Field(ge=1)] = 180
    grounding_min_similarity: Annotated[int, Field(ge=0, le=100)] = 90

    def model_for(self, provider: ProviderId) -> str | None:
        """Configured model of ``provider`` or ``None``."""
        return self.models.get(provider)


def parse_provider_list(text: str) -> list[ProviderId]:
    """Parse a comma separated provider list such as ``codex-cli,claude-cli``.

    Names are case-insensitive, duplicates are dropped, and ``none`` ends the
    list (``LLM_PROVIDERS=none`` therefore disables the LLM). Unknown names
    raise ``ValueError``.
    """
    known: dict[str, ProviderId] = {provider: provider for provider in PROVIDER_IDS}
    chain: list[ProviderId] = []
    for raw in text.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if name == NO_PROVIDER:
            break
        if name not in known:
            allowed = ", ".join([*PROVIDER_IDS, NO_PROVIDER])
            raise ValueError(f"unknown LLM provider {name!r} (allowed: {allowed})")
        if known[name] not in chain:
            chain.append(known[name])
    return chain


def resolve_providers(configured: Sequence[ProviderId], env: Mapping[str, str]) -> list[ProviderId]:
    """The provider chain that is actually used.

    * LLM_PROVIDERS, when set and not blank, replaces the configured order and
      may name opt-in providers.
    * Otherwise the configured order is used without the opt-in providers.
    * Inside GitHub Actions (GITHUB_ACTIONS=true) local-only providers are
      removed in both cases.
    """
    requested = env.get(ENV_LLM_PROVIDERS, "").strip()
    if requested:
        candidates: Sequence[ProviderId] = parse_provider_list(requested)
    else:
        candidates = [provider for provider in configured if provider not in OPT_IN_PROVIDERS]
    in_github_actions = env.get(ENV_GITHUB_ACTIONS, "").strip().lower() == "true"
    chain: list[ProviderId] = []
    for provider in candidates:
        if in_github_actions and provider in LOCAL_ONLY_PROVIDERS:
            continue
        if provider not in chain:
            chain.append(provider)
    return chain


def apply_llm_env(config: LLMConfig, env: Mapping[str, str]) -> LLMConfig:
    """Apply the environment overrides to a loaded config.

    The result's ``providers`` is the effective chain of ``resolve_providers``.
    Blank variables count as unset, because GitHub Actions passes undefined
    repository variables as empty strings.
    """
    models = dict(config.models)
    for provider in PROVIDER_IDS:
        override = env.get(model_env_var(provider), "").strip()
        if override:
            models[provider] = override
    base_url = env.get(ENV_LLM_BASE_URL, "").strip() or config.base_url
    return config.evolve(
        providers=resolve_providers(config.providers, env), models=models, base_url=base_url
    )
