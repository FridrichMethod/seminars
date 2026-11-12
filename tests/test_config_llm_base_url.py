"""The endpoint of openai-compat must be https, so LLM_API_KEY never travels in clear text.

Only an endpoint on this machine (a loopback address or ``localhost``) may
use plain http: nothing leaves the machine there.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from seminars.config import ConfigError, LLMConfig, apply_llm_env, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project with the configuration files of the repository that loading needs."""
    (tmp_path / "config").mkdir()
    for name in ("settings.yaml", "llm.yaml"):
        shutil.copy(REPO_ROOT / "config" / name, tmp_path / "config" / name)
    return tmp_path


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example/v1/",
        "http://llm.example.org/v1",
        "HTTP://llm.example.org/v1",
        "http://10.0.0.5:8080/v1",
        "http://192.168.1.20/v1",
        "http://localhost.example.org/v1",
        "http://[2001:db8::1]/v1",
    ],
)
def test_plain_http_to_another_machine_is_refused(url: str) -> None:
    with pytest.raises(ValidationError, match="https"):
        LLMConfig(base_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "https://llm.example.org/v1",
        "HTTPS://llm.example.org/v1",
        "http://localhost:8000/v1",
        "http://LOCALHOST/v1",
        "http://127.0.0.1:11434/v1",
        "http://127.8.9.10/v1",
        "http://[::1]:8080/v1",
    ],
)
def test_https_or_this_machine_is_accepted(url: str) -> None:
    assert LLMConfig(base_url=url).base_url == url


def test_the_environment_override_is_held_to_the_same_rule() -> None:
    with pytest.raises(ValidationError, match="https"):
        apply_llm_env(LLMConfig(), {"LLM_BASE_URL": "http://evil.example/v1/"})


def test_load_config_refuses_a_plain_http_endpoint(project: Path) -> None:
    with pytest.raises(ConfigError, match="https"):
        load_config(project, env={"LLM_BASE_URL": "http://evil.example/v1/"})


def test_the_configuration_file_is_held_to_the_same_rule(project: Path) -> None:
    llm_file = project / "config" / "llm.yaml"
    text = llm_file.read_text(encoding="utf-8").replace(
        "base_url: null", "base_url: http://llm.example.org/v1"
    )
    assert "http://llm.example.org/v1" in text
    llm_file.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="https"):
        load_config(project, env={})


def test_no_endpoint_is_still_allowed() -> None:
    assert LLMConfig().base_url is None
    assert apply_llm_env(LLMConfig(), {"LLM_BASE_URL": "  "}).base_url is None
