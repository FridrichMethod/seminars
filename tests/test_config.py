"""Tests for the configuration schemas and loaders."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from factories import REFERENCE_NOW
from seminars.config import (
    LOCAL_ONLY_PROVIDERS,
    OPT_IN_PROVIDERS,
    PROVIDER_CREDENTIAL_ENV,
    PROVIDER_IDS,
    TAG_ONLY_PROVIDERS,
    AppConfig,
    ConfigError,
    HttpSettings,
    Interests,
    LLMConfig,
    ProjectPaths,
    Settings,
    Taxonomy,
    apply_llm_env,
    find_project_root,
    load_config,
    load_interests,
    load_llm_config,
    load_settings,
    load_sources,
    load_taxonomy,
    model_env_var,
    parse_provider_list,
    read_env,
    resolve_providers,
)
from seminars.models import FIELD_IDS

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SOURCES = REPO_ROOT / "tests" / "data" / "sources.d" / "example.yaml"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A minimal project: only the required config/settings.yaml."""
    (tmp_path / "config").mkdir()
    shutil.copy(REPO_ROOT / "config" / "settings.yaml", tmp_path / "config" / "settings.yaml")
    return tmp_path


SOURCE_ENTRY = """\
  - id: {id}
    name: Example
    group: stanford-sites
    adapter: rss
    url: https://example.stanford.edu/events/all/rss
    institution: stanford
    host_org: Example Department
"""


class TestRepositoryConfig:
    def test_settings_hold_the_agreed_values(self) -> None:
        settings = load_settings(REPO_ROOT / "config" / "settings.yaml")
        assert settings.timezone == "America/Los_Angeles"
        assert (settings.window.days_back, settings.window.days_ahead) == (14, 120)
        assert settings.uid_domain == "seminars.fridrichmethod.github.io"
        assert settings.site.base_url == "https://fridrichmethod.github.io/seminars/"
        http = settings.http
        assert http.user_agent == "seminars-bot/1.0 (+https://fridrichmethod.github.io/seminars/)"
        assert http.robots_token == "seminars-bot"
        assert http.min_interval_seconds == 1.0
        assert http.timeout_seconds == 20
        assert http.retries == 3
        assert http.max_requests_per_host == 20
        assert settings.events.default_duration_minutes == 60
        assert settings.events.delete_after_missing_runs == 3
        assert settings.health.source_failure_threshold == 3
        assert settings.health.source_empty_threshold == 3
        assert settings.health.llm_failure_threshold == 2
        assert (settings.dedup.max_start_difference_minutes, settings.dedup.title_similarity) == (
            15,
            85,
        )

    def test_llm_file_is_schema_valid(self) -> None:
        llm = load_llm_config(REPO_ROOT / "config" / "llm.yaml")
        assert llm.batch_size == 20
        assert llm.max_calls_per_run == 60
        assert llm.max_input_chars == 12000
        assert llm.timeout_seconds == 120
        assert llm.providers == list(PROVIDER_IDS)

    def test_whole_configuration_loads(self) -> None:
        config = load_config(REPO_ROOT, env={})
        assert isinstance(config, AppConfig)
        assert config.paths.root == REPO_ROOT
        assert list(config.taxonomy.fields) == list(FIELD_IDS)
        assert len({source.id for source in config.sources}) == len(config.sources)

    def test_find_project_root_from_a_subdirectory(self) -> None:
        assert find_project_root(REPO_ROOT / "src" / "seminars") == REPO_ROOT

    def test_find_project_root_fails_outside_a_project(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"no config/settings\.yaml"):
            find_project_root(tmp_path)


class TestSettings:
    def test_window_at(self) -> None:
        settings = load_settings(REPO_ROOT / "config" / "settings.yaml")
        window = settings.window_at(REFERENCE_NOW)
        assert window.start.isoformat() == "2026-10-27T00:00:00-07:00"
        assert window.end.isoformat() == "2027-03-11T00:00:00-08:00"

    def test_missing_settings_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="required configuration file is missing"):
            load_settings(tmp_path / "config" / "settings.yaml")

    def test_unknown_key_is_reported_with_file_and_location(self, project: Path) -> None:
        path = project / "config" / "settings.yaml"
        path.write_text(path.read_text(encoding="utf-8") + "\ncolour: blue\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=r"settings\.yaml: colour: Extra inputs"):
            load_settings(path)

    def test_identity_values_have_no_default(self) -> None:
        with pytest.raises(ValidationError) as caught:
            Settings.model_validate({})
        missing = {str(error["loc"][0]) for error in caught.value.errors()}
        assert missing == {"timezone", "uid_domain", "site", "http"}

    @pytest.mark.parametrize(
        ("section", "key", "value"),
        [
            ("http", "max_requests_per_host", 0),
            ("http", "timeout_seconds", 0),
            ("http", "retries", -1),
            ("window", "days_back", -1),
            ("dedup", "title_similarity", 101),
            ("site", "base_url", "https://example.org/seminars"),
        ],
    )
    def test_out_of_range_values_are_rejected(self, section: str, key: str, value: object) -> None:
        data = yaml.safe_load((REPO_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
        data[section][key] = value
        with pytest.raises(ValidationError):
            Settings.model_validate(data)

    def test_invalid_yaml_is_a_config_error(self, project: Path) -> None:
        path = write(project / "config" / "settings.yaml", "timezone: [unclosed\n")
        with pytest.raises(ConfigError, match="invalid YAML"):
            load_settings(path)


class TestOptionalFiles:
    def test_missing_optional_files_fall_back_to_defaults(self, project: Path) -> None:
        config = load_config(project, env={})
        assert config.llm == apply_llm_env(LLMConfig(), {})
        assert config.taxonomy == Taxonomy()
        assert config.interests == Interests()
        assert config.sources == ()
        assert config.enabled_sources == ()

    def test_empty_optional_file_means_defaults(self, project: Path) -> None:
        path = write(project / "config" / "interests.yaml", "# nothing yet\n")
        assert load_interests(path) == Interests()

    def test_invalid_optional_file_is_an_error(self, project: Path) -> None:
        path = write(project / "config" / "llm.yaml", "batch_size: 0\n")
        with pytest.raises(ConfigError, match=r"llm\.yaml: batch_size"):
            load_llm_config(path)


class TestSources:
    def test_absent_directory_means_no_sources(self, tmp_path: Path) -> None:
        assert load_sources(tmp_path / "config" / "sources.d") == []

    def test_reference_example_is_valid(self) -> None:
        sources = load_sources(EXAMPLE_SOURCES.parent)
        assert [source.id for source in sources] == [
            "custom-example-lab",
            "localist-stanford",
            "rsorg-stanford-example",
            "sites-bioengineering",
            "sites-chemistry",
        ]
        chemistry = sources[-1]
        assert chemistry.group == "stanford-sites"
        assert chemistry.institution == "stanford"
        assert chemistry.research is not None
        assert chemistry.research.crawl_delay == 30
        assert chemistry.research.model_extra == {"template": "A"}
        assert sources[0].wants_llm_fallback
        assert sources[1].aggregator
        assert sources[1].params["department_fields"] == {
            "Chemistry": ["chem"],
            "Bioengineering": ["bio"],
        }

    def test_files_are_merged_and_sorted_by_id(self, project: Path) -> None:
        directory = project / "config" / "sources.d"
        write(directory / "b.yaml", "sources:\n" + SOURCE_ENTRY.format(id="sites-zeta"))
        write(directory / "a.yaml", "sources:\n" + SOURCE_ENTRY.format(id="sites-alpha"))
        write(directory / "notes.txt", "not yaml, ignored\n")
        write(directory / "empty.yaml", "# no sources yet\n")
        write(directory / "none.yaml", "sources:\n")
        config = load_config(project, env={})
        assert [source.id for source in config.sources] == ["sites-alpha", "sites-zeta"]
        assert config.source("sites-zeta").host_org == "Example Department"
        with pytest.raises(KeyError):
            config.source("sites-unknown")

    def test_disabled_sources_are_loaded_but_not_enabled(self, project: Path) -> None:
        entry = SOURCE_ENTRY.format(id="sites-off") + "    enabled: false\n"
        write(project / "config" / "sources.d" / "a.yaml", "sources:\n" + entry)
        config = load_config(project, env={})
        assert [source.id for source in config.sources] == ["sites-off"]
        assert config.enabled_sources == ()

    def test_duplicate_id_across_files_is_an_error(self, project: Path) -> None:
        directory = project / "config" / "sources.d"
        write(directory / "a.yaml", "sources:\n" + SOURCE_ENTRY.format(id="sites-same"))
        write(directory / "b.yaml", "sources:\n" + SOURCE_ENTRY.format(id="sites-same"))
        with pytest.raises(ConfigError) as caught:
            load_sources(directory)
        message = str(caught.value)
        assert "duplicate source id 'sites-same'" in message
        assert "a.yaml" in message
        assert "b.yaml" in message

    def test_duplicate_id_within_one_file_is_an_error(self, project: Path) -> None:
        entry = SOURCE_ENTRY.format(id="sites-same")
        path = write(project / "config" / "sources.d" / "a.yaml", "sources:\n" + entry + entry)
        with pytest.raises(ConfigError, match="duplicate source id"):
            load_sources(path.parent)

    def test_invalid_entry_names_file_index_and_id(self, project: Path) -> None:
        entry = SOURCE_ENTRY.format(id="sites-bad") + "    selector: div.event\n"
        path = write(project / "config" / "sources.d" / "a.yaml", "sources:\n" + entry)
        with pytest.raises(ConfigError) as caught:
            load_sources(path.parent)
        message = str(caught.value)
        assert "a.yaml: sources[0] (id: sites-bad): selector: Extra inputs" in message

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("- id: a\n", "expected a mapping"),
            ("sources: {}\nextra: 1\n", "unknown top-level key"),
            ("sources: 5\n", "'sources' must be a list"),
            ("sources:\n  - just a string\n", r"sources\[0\] \(id: \?\)"),
        ],
    )
    def test_malformed_files_are_rejected(self, project: Path, text: str, expected: str) -> None:
        path = write(project / "config" / "sources.d" / "a.yaml", text)
        with pytest.raises(ConfigError, match=expected):
            load_sources(path.parent)


class TestLLMEnvironment:
    def test_default_chain_skips_opt_in_providers(self) -> None:
        assert resolve_providers(PROVIDER_IDS, {}) == [
            "claude-cli",
            "anthropic",
            "openai",
            "openai-compat",
        ]

    def test_llm_providers_reorders_and_selects(self) -> None:
        env = {"LLM_PROVIDERS": " codex-cli , Claude-CLI,codex-cli "}
        assert resolve_providers(PROVIDER_IDS, env) == ["codex-cli", "claude-cli"]

    def test_codex_never_runs_in_github_actions(self) -> None:
        env = {"LLM_PROVIDERS": "codex-cli,claude-cli", "GITHUB_ACTIONS": "true"}
        assert resolve_providers(PROVIDER_IDS, env) == ["claude-cli"]
        assert "codex-cli" not in resolve_providers(PROVIDER_IDS, {"GITHUB_ACTIONS": "true"})

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_variable_counts_as_unset(self, value: str) -> None:
        assert resolve_providers(PROVIDER_IDS, {"LLM_PROVIDERS": value}) == resolve_providers(
            PROVIDER_IDS, {}
        )

    def test_none_disables_the_llm_or_ends_the_chain(self) -> None:
        assert parse_provider_list("none") == []
        assert parse_provider_list("openai,none,anthropic") == ["openai"]
        assert resolve_providers(PROVIDER_IDS, {"LLM_PROVIDERS": "none"}) == []

    def test_unknown_provider_is_an_error(self, project: Path) -> None:
        with pytest.raises(ValueError, match="unknown LLM provider 'gemini'"):
            parse_provider_list("claude-cli,gemini")
        with pytest.raises(ConfigError, match="unknown LLM provider"):
            load_config(project, env={"LLM_PROVIDERS": "gemini"})

    def test_model_and_base_url_overrides(self) -> None:
        config = LLMConfig(models={"anthropic": "model-a", "openai": "model-b"})
        env = {
            "LLM_MODEL_ANTHROPIC": "model-c",
            "LLM_MODEL_OPENAI_COMPAT": "model-d",
            "LLM_MODEL_OPENAI": "  ",
            "LLM_BASE_URL": "https://llm.example.org/v1",
        }
        effective = apply_llm_env(config, env)
        assert effective.model_for("anthropic") == "model-c"
        assert effective.model_for("openai") == "model-b"
        assert effective.model_for("openai-compat") == "model-d"
        assert effective.model_for("claude-cli") is None
        assert effective.base_url == "https://llm.example.org/v1"
        assert config.model_for("anthropic") == "model-a"

    def test_invalid_base_url_override_is_an_error(self, project: Path) -> None:
        with pytest.raises(ConfigError, match="LLM environment overrides"):
            load_config(project, env={"LLM_BASE_URL": "not-a-url"})

    def test_codex_is_opt_in_local_only_and_tag_only(self) -> None:
        assert OPT_IN_PROVIDERS == LOCAL_ONLY_PROVIDERS == TAG_ONLY_PROVIDERS == {"codex-cli"}

    def test_robots_token_of_odd_user_agents(self) -> None:
        assert HttpSettings(user_agent="Seminars-Bot/1.0 (+https://x.org/)").robots_token == (
            "seminars-bot"
        )
        assert HttpSettings(user_agent="/odd").robots_token == "*"

    def test_model_env_var_names(self) -> None:
        assert model_env_var("claude-cli") == "LLM_MODEL_CLAUDE_CLI"
        assert model_env_var("openai-compat") == "LLM_MODEL_OPENAI_COMPAT"

    def test_credential_variable_names(self) -> None:
        assert PROVIDER_CREDENTIAL_ENV == {
            "claude-cli": "CLAUDE_CODE_OAUTH_TOKEN",
            "codex-cli": None,
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "openai-compat": "LLM_API_KEY",
        }

    def test_load_config_applies_the_environment(self, project: Path) -> None:
        config = load_config(project, env={"LLM_PROVIDERS": "openai", "LLM_MODEL_OPENAI": "m"})
        assert config.llm.providers == ["openai"]
        assert config.llm.model_for("openai") == "m"

    def test_schema_defaults_match_the_spec(self) -> None:
        config = LLMConfig()
        assert (config.batch_size, config.max_calls_per_run) == (20, 60)
        assert (config.max_input_chars, config.timeout_seconds) == (12000, 120)
        assert (config.cache_ttl_days, config.grounding_min_similarity) == (180, 90)
        with pytest.raises(ValidationError):
            LLMConfig.model_validate({"providers": ["gemini"]})
        with pytest.raises(ValidationError):
            LLMConfig.model_validate({"models": {"gemini": "x"}})


class TestTaxonomyAndInterests:
    def test_taxonomy_always_has_all_fields_in_order(self, project: Path) -> None:
        text = (
            "fields:\n"
            "  chem:\n"
            "    keywords: {catalysis: 1.0, synthesis: 0.5}\n"
            "    series: [Physical Chemistry Seminar]\n"
            "  math:\n"
            "    keywords: [number theory, topology]\n"
            "source_defaults:\n"
            "  sites-chemistry: [chem]\n"
            "departments:\n"
            "  Chemical Engineering: [chem]\n"
            "exclusions:\n"
            "  office_hours: [office hours]\n"
        )
        taxonomy = load_taxonomy(write(project / "config" / "taxonomy.yaml", text))
        assert list(taxonomy.fields) == list(FIELD_IDS)
        assert taxonomy.fields["math"].keywords == {"number theory": 1.0, "topology": 1.0}
        assert taxonomy.fields["chem"].keywords["synthesis"] == 0.5
        assert taxonomy.fields["chem"].series == ["Physical Chemistry Seminar"]
        assert taxonomy.fields["bio"].keywords == {}
        assert taxonomy.source_defaults == {"sites-chemistry": ["chem"]}
        assert taxonomy.departments == {"Chemical Engineering": ["chem"]}
        assert taxonomy.exclusions == {"office_hours": ["office hours"]}

    @pytest.mark.parametrize(
        "data",
        [
            {"fields": {"history": {}}},
            {"fields": {"chem": {"keywords": {"x": 0}}}},
            {"fields": {"chem": {"keywords": [" "]}}},
            {"fields": {"chem": {"min_score": 0}}},
            {"departments": {"Chemistry": ["alchemy"]}},
            {"source_defaults": {"Not An Id": ["chem"]}},
            {"exclusions": {"out_of_scope": ["x"]}},
            {"unknown": 1},
        ],
    )
    def test_taxonomy_rejects_invalid_content(self, data: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            Taxonomy.model_validate(data)

    def test_interests(self, project: Path) -> None:
        text = (
            "threshold: 4\n"
            "multipliers: {title: 3, series: 2, abstract: 1}\n"
            "keywords:\n"
            "  protein design: 3\n"
            "  enzyme design: 3\n"
            "series_allowlist: [Math Colloquium, SLAC Colloquium]\n"
            "speaker_watchlist: [Jane Doe]\n"
            "exclusions:\n"
            "  undergraduate: 2\n"
        )
        interests = load_interests(write(project / "config" / "interests.yaml", text))
        assert interests.threshold == 4.0
        assert interests.keywords == {"protein design": 3.0, "enzyme design": 3.0}
        assert interests.series_allowlist == ["Math Colloquium", "SLAC Colloquium"]
        assert interests.exclusions == {"undergraduate": 2.0}
        assert interests.llm_rerank is False

    def test_llm_rerank_defaults_to_off(self) -> None:
        assert Interests().llm_rerank is False
        assert Interests().keywords == {}

    def test_multipliers_must_decrease(self) -> None:
        with pytest.raises(ValidationError, match="title > series > abstract"):
            Interests.model_validate({"multipliers": {"title": 1, "series": 2, "abstract": 1}})


class TestPathsAndEnvironment:
    def test_project_paths(self, tmp_path: Path) -> None:
        paths = ProjectPaths(tmp_path)
        relative = {
            "settings_file": "config/settings.yaml",
            "llm_file": "config/llm.yaml",
            "taxonomy_file": "config/taxonomy.yaml",
            "interests_file": "config/interests.yaml",
            "sources_dir": "config/sources.d",
            "events_file": "data/events.json",
            "uid_map_file": "data/uid_map.json",
            "health_file": "data/health.json",
            "last_run_file": "data/last_run.json",
            "run_report_file": "data/run_report.json",
            "llm_cache_dir": "data/llm_cache",
            "archive_dir": "data/archive",
            "site_dir": "site",
            "locales_dir": "locales",
            "public_dir": "public",
            "public_feeds_dir": "public/feeds",
            "public_locales_dir": "public/locales",
            "public_events_file": "public/events.json",
            "public_status_file": "public/status.json",
            "fixtures_dir": "tests/fixtures",
            "cache_dir": ".cache",
            "http_cache_dir": ".cache/http",
            "env_file": ".env",
        }
        for name, expected in relative.items():
            assert getattr(paths, name) == tmp_path / expected, name
        assert paths.archive_file(2026) == tmp_path / "data" / "archive" / "2026.jsonl"

    def test_read_env_merges_dotenv_under_the_real_environment(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write(project / ".env", "LLM_PROVIDERS=openai\nONLY_IN_FILE=from-file\n")
        monkeypatch.setenv("LLM_PROVIDERS", "anthropic")
        monkeypatch.delenv("ONLY_IN_FILE", raising=False)
        env = read_env(project)
        assert env["LLM_PROVIDERS"] == "anthropic"
        assert env["ONLY_IN_FILE"] == "from-file"

    def test_read_env_without_dotenv_file(self, project: Path) -> None:
        assert "PATH" in read_env(project)
