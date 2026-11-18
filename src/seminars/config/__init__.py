"""Configuration: schemas, loaders and project paths.

Import everything from ``seminars.config``.

* settings.py   Settings: config/settings.yaml
* llm.py        LLMConfig: config/llm.yaml, plus LLM_PROVIDERS / LLM_MODEL_* / LLM_BASE_URL
* classify.py   Taxonomy and Interests: config/taxonomy.yaml, config/interests.yaml
* paths.py      ProjectPaths: the location of every project file
* loader.py     load_config and the per-file loaders; AppConfig

The schema of one source entry (config/sources.d/*.yaml) is
``seminars.models.SourceConfig``.
"""

from seminars.config.classify import (
    FieldRules,
    Interests,
    PicksMultipliers,
    Taxonomy,
    WeightedTerms,
)
from seminars.config.llm import (
    ENV_GITHUB_ACTIONS,
    ENV_LLM_BASE_URL,
    ENV_LLM_MODEL_PREFIX,
    ENV_LLM_PROVIDERS,
    LOCAL_ONLY_PROVIDERS,
    NO_PROVIDER,
    OPT_IN_PROVIDERS,
    PROVIDER_CREDENTIAL_ENV,
    PROVIDER_IDS,
    TAG_ONLY_PROVIDERS,
    CliProviderId,
    LLMConfig,
    ProviderId,
    apply_llm_env,
    model_env_var,
    parse_provider_list,
    resolve_providers,
)
from seminars.config.loader import (
    AppConfig,
    load_config,
    load_interests,
    load_llm_config,
    load_settings,
    load_sources,
    load_taxonomy,
    read_env,
    read_yaml,
)
from seminars.config.paths import (
    ENV_HTTP_CACHE_DIR,
    ConfigError,
    ProjectPaths,
    find_project_root,
    main_checkout,
)
from seminars.config.settings import (
    DedupSettings,
    EventSettings,
    FeedSettings,
    HealthSettings,
    HttpSettings,
    MeetingSettings,
    Settings,
    SiteSettings,
    WindowSettings,
)

__all__ = [
    "ENV_GITHUB_ACTIONS",
    "ENV_HTTP_CACHE_DIR",
    "ENV_LLM_BASE_URL",
    "ENV_LLM_MODEL_PREFIX",
    "ENV_LLM_PROVIDERS",
    "LOCAL_ONLY_PROVIDERS",
    "NO_PROVIDER",
    "OPT_IN_PROVIDERS",
    "PROVIDER_CREDENTIAL_ENV",
    "PROVIDER_IDS",
    "TAG_ONLY_PROVIDERS",
    "AppConfig",
    "CliProviderId",
    "ConfigError",
    "DedupSettings",
    "EventSettings",
    "FeedSettings",
    "FieldRules",
    "HealthSettings",
    "HttpSettings",
    "Interests",
    "LLMConfig",
    "MeetingSettings",
    "PicksMultipliers",
    "ProjectPaths",
    "ProviderId",
    "Settings",
    "SiteSettings",
    "Taxonomy",
    "WeightedTerms",
    "WindowSettings",
    "apply_llm_env",
    "find_project_root",
    "load_config",
    "load_interests",
    "load_llm_config",
    "load_settings",
    "load_sources",
    "load_taxonomy",
    "main_checkout",
    "model_env_var",
    "parse_provider_list",
    "read_env",
    "read_yaml",
    "resolve_providers",
]
