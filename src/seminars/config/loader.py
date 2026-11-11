"""Loaders for config/*.yaml. YAML is always read with ``yaml.safe_load``."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ValidationError

from seminars.config.classify import Interests, Taxonomy
from seminars.config.llm import LLMConfig, apply_llm_env
from seminars.config.paths import ConfigError, ProjectPaths, find_project_root
from seminars.config.settings import Settings
from seminars.models import SourceConfig
from seminars.textio import read_text

SOURCES_KEY = "sources"
EXTENSION_KEY_PREFIX = "x-"


@dataclass(frozen=True)
class AppConfig:
    """Everything loaded from config/, plus the project paths."""

    paths: ProjectPaths
    settings: Settings
    llm: LLMConfig  # environment overrides already applied
    taxonomy: Taxonomy
    interests: Interests
    sources: tuple[SourceConfig, ...]  # sorted by id, disabled ones included

    @property
    def enabled_sources(self) -> tuple[SourceConfig, ...]:
        return tuple(source for source in self.sources if source.enabled)

    def source(self, source_id: str) -> SourceConfig:
        """The source with ``source_id``; ``KeyError`` when there is none."""
        for source in self.sources:
            if source.id == source_id:
                return source
        raise KeyError(source_id)


def _describe(error: ValidationError) -> str:
    problems = []
    for item in error.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in item["loc"]) or "(document)"
        problems.append(f"{location}: {item['msg']}")
    return "; ".join(problems)


def read_yaml(path: Path) -> Any:
    """Parse one YAML file; an empty file yields ``None``."""
    try:
        return yaml.safe_load(read_text(path))
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path}: cannot read file: {error}") from error
    except yaml.YAMLError as error:
        raise ConfigError(f"{path}: invalid YAML: {error}") from error


def _load_model[T: BaseModel](path: Path, model: type[T], *, required: bool) -> T:
    if not path.is_file():
        if required:
            raise ConfigError(f"{path}: required configuration file is missing")
        return model()
    data = read_yaml(path)
    try:
        return model.model_validate({} if data is None else data)
    except ValidationError as error:
        raise ConfigError(f"{path}: {_describe(error)}") from error


def load_settings(path: Path) -> Settings:
    """Load config/settings.yaml; the file is required."""
    return _load_model(path, Settings, required=True)


def load_llm_config(path: Path) -> LLMConfig:
    """Load config/llm.yaml without environment overrides; defaults when missing."""
    return _load_model(path, LLMConfig, required=False)


def load_taxonomy(path: Path) -> Taxonomy:
    """Load config/taxonomy.yaml; an empty taxonomy when missing."""
    return _load_model(path, Taxonomy, required=False)


def load_interests(path: Path) -> Interests:
    """Load config/interests.yaml; no interests (and no picks) when missing."""
    return _load_model(path, Interests, required=False)


def _load_sources_file(path: Path) -> list[SourceConfig]:
    data = read_yaml(path)
    if data is None:
        return []
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping with a '{SOURCES_KEY}' list")
    unknown = sorted(
        str(key)
        for key in data
        if key != SOURCES_KEY and not str(key).startswith(EXTENSION_KEY_PREFIX)
    )
    if unknown:
        raise ConfigError(
            f"{path}: unknown top-level key(s): {', '.join(unknown)} "
            f"(only '{SOURCES_KEY}' and '{EXTENSION_KEY_PREFIX}*' keys are allowed)"
        )
    entries = data.get(SOURCES_KEY) or []
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: '{SOURCES_KEY}' must be a list")
    sources = []
    for index, entry in enumerate(entries):
        label = entry.get("id", "?") if isinstance(entry, dict) else "?"
        try:
            sources.append(SourceConfig.model_validate(entry))
        except ValidationError as error:
            raise ConfigError(
                f"{path}: {SOURCES_KEY}[{index}] (id: {label}): {_describe(error)}"
            ) from error
    return sources


def load_sources(directory: Path) -> list[SourceConfig]:
    """Load every ``*.yaml`` file in config/sources.d, sorted by source id.

    Each file is a mapping with a ``sources`` list of SourceConfig entries.
    Top-level keys starting with ``x-`` are ignored, which leaves room for
    YAML anchors holding shared values. A missing directory means no sources.
    A source id that appears twice (in one file or across files) is an error.
    """
    if not directory.is_dir():
        return []
    origin: dict[str, Path] = {}
    sources: list[SourceConfig] = []
    for path in sorted(directory.glob("*.yaml")):
        for source in _load_sources_file(path):
            if source.id in origin:
                raise ConfigError(
                    f"{path}: duplicate source id {source.id!r} "
                    f"(already defined in {origin[source.id]})"
                )
            origin[source.id] = path
            sources.append(source)
    return sorted(sources, key=lambda source: source.id)


def read_env(root: Path | None = None) -> dict[str, str]:
    """Process environment merged over the optional ``.env`` file of the project.

    Real environment variables win over ``.env`` entries. Nothing is written
    back to ``os.environ`` and values are never logged.
    """
    env_file = ProjectPaths(root or find_project_root()).env_file
    values = {key: value for key, value in dotenv_values(env_file).items() if value is not None}
    return {**values, **os.environ}


def load_config(root: Path | None = None, env: Mapping[str, str] | None = None) -> AppConfig:
    """Load the whole configuration of the project rooted at ``root``.

    ``root`` defaults to the repository that contains the current directory.
    ``env`` only feeds the LLM overrides (LLM_PROVIDERS, LLM_MODEL_<PROVIDER>,
    LLM_BASE_URL, GITHUB_ACTIONS) and defaults to ``os.environ``; pass
    ``read_env(root)`` to honour a local ``.env`` file as well.
    """
    paths = ProjectPaths(root or find_project_root())
    try:
        llm = apply_llm_env(load_llm_config(paths.llm_file), os.environ if env is None else env)
    except (ValueError, ValidationError) as error:
        raise ConfigError(f"LLM environment overrides: {error}") from error
    return AppConfig(
        paths=paths,
        settings=load_settings(paths.settings_file),
        llm=llm,
        taxonomy=load_taxonomy(paths.taxonomy_file),
        interests=load_interests(paths.interests_file),
        sources=tuple(load_sources(paths.sources_dir)),
    )
