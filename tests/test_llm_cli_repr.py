"""The text form of a child process request and result never shows a credential or the output."""

from __future__ import annotations

from pathlib import Path

from seminars.llm.cli import CliRequest, CliResult
from test_llm_fakes import FAKE_TOKEN


def request() -> CliRequest:
    return CliRequest(
        argv=("claude", "-p"),
        stdin="page text",
        env={"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN},
        cwd=Path("/tmp/empty"),
        timeout=120.0,
    )


def test_a_request_names_its_variables_but_not_their_values() -> None:
    for text in (repr(request()), str(request()), f"{request()}"):
        assert FAKE_TOKEN not in text
        assert "oat01" not in text
        assert "CLAUDE_CODE_OAUTH_TOKEN" in text
        assert "PATH" in text
        assert "/usr/bin" not in text
        assert "'claude'" in text


def test_a_request_in_a_container_does_not_show_the_credential_either() -> None:
    assert FAKE_TOKEN not in repr([request()])
    assert FAKE_TOKEN not in repr({"request": request()})


def test_the_environment_is_still_there_for_the_runner() -> None:
    assert request().env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_TOKEN


def test_a_result_gives_the_length_of_stderr_and_not_its_text() -> None:
    result = CliResult(1, "{}", f"401 Unauthorized for owner@example.org {FAKE_TOKEN}")
    text = repr(result)
    assert "owner@example.org" not in text
    assert FAKE_TOKEN not in text
    assert f"stderr=<{len(result.stderr)} characters>" in text
    assert "returncode=1" in text
    assert result == CliResult(1, "{}", result.stderr)


def test_a_result_gives_the_length_of_stdout_and_not_its_text() -> None:
    stdout = '{"session_id": "c3167271", "credential_expires_at": "2026-11-28T02:51:31Z"}'
    result = CliResult(1, stdout, "")
    text = repr(result)
    assert "credential_expires_at" not in text
    assert "c3167271" not in text
    assert f"stdout=<{len(stdout)} characters>" in text
    assert result.stdout == stdout
