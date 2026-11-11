"""Tests for the secret scanner.

Every credential-shaped value is put together from pieces at run time, so
this file holds nothing that looks like a token.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from seminars.contracts import SecretScanner, load_implementation
from seminars.pipeline.secret_scan import (
    REDACTED,
    PatternSecretScanner,
    build_secret_scanner,
    known_credentials,
)

ALNUM = "A1b2C3d4E5f6G7h8I9j0"


def forged(prefix: str, length: int = 40) -> str:
    """A token-shaped string: ``prefix`` followed by ``length`` letters and digits."""
    body = (ALNUM * (length // len(ALNUM) + 1))[:length]
    return prefix + body


FORGED: dict[str, str] = {
    "github-token": forged("gh" + "p_", 36),
    "github-oauth": forged("gh" + "o_", 36),
    "github-server": forged("gh" + "s_", 36),
    "github-pat": forged("github" + "_pat_", 60),
    "anthropic-api": forged("sk-" + "ant-" + "api03-", 80),
    "anthropic-oauth": forged("sk-" + "ant-" + "oat01-", 80),
    "openai-legacy": forged("s" + "k-", 48),
    "openai-project": forged("sk-" + "proj-", 90),
    "bearer": "Authorization: " + "Bear" + "er " + forged("", 40),
    "jwt": ".".join([forged("ey" + "J", 20), forged("ey" + "J", 30), forged("", 30)]),
    "private-key": "-" * 5 + "BEGIN " + "OPENSSH PRIVATE " + "KEY" + "-" * 5,
    "rsa-key": "-" * 5 + "BEGIN " + "RSA PRIVATE " + "KEY" + "-" * 5,
    "plain-key": "-" * 5 + "BEGIN " + "PRIVATE " + "KEY" + "-" * 5,
    "refresh": '"refresh' + '_token": "' + forged("", 30) + '"',
    "access": "access" + "_token=" + forged("", 30),
}

EXPECTED_RULE: dict[str, str] = {
    "github-token": "github-token",
    "github-oauth": "github-token",
    "github-server": "github-token",
    "github-pat": "github-token",
    "anthropic-api": "anthropic-key",
    "anthropic-oauth": "anthropic-key",
    "openai-legacy": "openai-key",
    "openai-project": "openai-key",
    "bearer": "bearer-token",
    "jwt": "jwt",
    "private-key": "private-key",
    "rsa-key": "private-key",
    "plain-key": "private-key",
    "refresh": "token-keyword",
    "access": "token-keyword",
}

HARMLESS = [
    "Protein design with diffusion models",
    "A risk-averse approach to task-based learning",
    "https://bioengineering.stanford.edu/events/sk-learn-tutorial-advanced-topics-in-python",
    "The standard bearer of representation theory",
    "uid sites-chemistry.0b9c1c36-8a7e-4f7b-9a53-0d2a1b3c4d5e",
    "ghp_ is the prefix of a GitHub token and sk-ant- that of an Anthropic key",
    "Zoom https://stanford.zoom.us/j/1234567890",
    "export ANTHROPIC_API_KEY and run gh secret set CLAUDE_CODE_OAUTH_TOKEN",
    "sha256 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
]


@pytest.fixture
def scanner() -> PatternSecretScanner:
    return build_secret_scanner()


class TestRules:
    @pytest.mark.parametrize("name", sorted(FORGED))
    def test_a_forged_credential_is_found(self, scanner: PatternSecretScanner, name: str) -> None:
        text = f"first line\nsome text {FORGED[name]} more text\nlast line\n"
        findings = scanner.scan_text(text, origin="data/events.json")
        assert [(finding.origin, finding.line) for finding in findings] == [
            ("data/events.json", 2)
        ] * len(findings)
        assert EXPECTED_RULE[name] in {finding.rule for finding in findings}

    @pytest.mark.parametrize("text", HARMLESS)
    def test_ordinary_text_is_not_reported(self, scanner: PatternSecretScanner, text: str) -> None:
        assert scanner.scan_text(text + "\n", origin="x") == []
        assert scanner.redact(text) == text
        assert not scanner.has_secret(text)

    def test_a_finding_never_holds_the_matched_text(self, scanner: PatternSecretScanner) -> None:
        token = FORGED["github-token"]
        (finding,) = scanner.scan_text(f"token: {token}\n", origin="report")
        assert token not in finding.model_dump_json()
        assert token[:8] not in finding.model_dump_json()

    def test_findings_are_sorted_and_reported_once_per_line_and_rule(
        self, scanner: PatternSecretScanner
    ) -> None:
        token = FORGED["github-token"]
        text = f"{token} {token}\nclean\n{FORGED['jwt']}\n{token}\n"
        found = [(finding.line, finding.rule) for finding in scanner.scan_text(text, origin="x")]
        assert found == [(1, "github-token"), (3, "jwt"), (4, "github-token")]

    def test_a_token_inside_a_json_string_is_found(self, scanner: PatternSecretScanner) -> None:
        text = '{"abstract": "see ' + FORGED["anthropic-oauth"] + '"}\n'
        assert [finding.rule for finding in scanner.scan_text(text, origin="x")] == [
            "anthropic-key"
        ]

    def test_empty_text_has_no_findings(self, scanner: PatternSecretScanner) -> None:
        assert scanner.scan_text("", origin="x") == []


class TestKnownCredentials:
    def test_the_value_of_a_credential_variable_is_found_whatever_it_looks_like(self) -> None:
        value = "plain" + "-looking-" + "value-0001"
        scanner = build_secret_scanner().knowing([value])
        assert build_secret_scanner().scan_text(f"x {value}\n", origin="o") == []
        (finding,) = scanner.scan_text(f"x {value}\n", origin="o")
        assert (finding.line, finding.rule) == (1, "known-credential")
        assert scanner.redact(f"x {value} y") == f"x {REDACTED} y"

    def test_short_and_blank_values_are_ignored(self) -> None:
        scanner = build_secret_scanner().knowing(["", "  ", "abc", "seven77"])
        assert scanner.scan_text("abc seven77 text\n", origin="o") == []

    def test_known_credentials_reads_the_credential_variables(self) -> None:
        env = {
            "CLAUDE_CODE_OAUTH_TOKEN": " first-value-1234 \n",
            "ANTHROPIC_API_KEY": "second-value-1234",
            "OPENAI_API_KEY": "",
            "LLM_API_KEY": "third-value-1234",
            "GITHUB_TOKEN": "fourth-value-1234",
            "HOME": "/home/someone/long-enough",
        }
        assert known_credentials(env) == [
            "first-value-1234",
            "fourth-value-1234",
            "second-value-1234",
            "third-value-1234",
        ]

    def test_the_scanner_does_not_show_what_it_knows(self) -> None:
        value = "plain" + "-looking-" + "value-0002"
        scanner = build_secret_scanner().knowing([value])
        assert value not in repr(scanner)
        assert value not in str(scanner)


class TestRedact:
    @pytest.mark.parametrize("name", sorted(FORGED))
    def test_redaction_removes_what_the_scan_finds(
        self, scanner: PatternSecretScanner, name: str
    ) -> None:
        text = f"before {FORGED[name]} after"
        cleaned = scanner.redact(text)
        assert REDACTED in cleaned
        assert cleaned.startswith("before ")
        assert cleaned.endswith(" after")
        assert scanner.scan_text(cleaned, origin="x") == []
        assert scanner.has_secret(text)

    def test_redaction_is_idempotent(self, scanner: PatternSecretScanner) -> None:
        once = scanner.redact(f"a {FORGED['openai-project']} b {FORGED['jwt']}")
        assert scanner.redact(once) == once


class TestScanPaths:
    def test_directories_are_scanned_recursively(
        self, scanner: PatternSecretScanner, tmp_path: Path
    ) -> None:
        data = tmp_path / "data"
        (data / "llm_cache" / "ab").mkdir(parents=True)
        (data / "events.json").write_text("clean\n", encoding="utf-8")
        leak = data / "llm_cache" / "ab" / "entry.json"
        leak.write_text("{\n" + f'"x": "{FORGED["github-pat"]}"' + "\n}\n", encoding="utf-8")
        single = tmp_path / "single.txt"
        single.write_text(FORGED["private-key"] + "\n", encoding="utf-8")
        findings = scanner.scan_paths([data, single, tmp_path / "missing"])
        assert [(finding.origin, finding.line, finding.rule) for finding in findings] == [
            (str(leak), 2, "github-token"),
            (str(single), 1, "private-key"),
        ]

    def test_bytes_that_are_not_text_do_not_stop_the_scan(
        self, scanner: PatternSecretScanner, tmp_path: Path
    ) -> None:
        path = tmp_path / "blob.bin"
        path.write_bytes(b"\xff\xfe\x00junk\n" + FORGED["github-token"].encode("ascii") + b"\n")
        assert [finding.line for finding in scanner.scan_paths([path])] == [2]


class TestContract:
    def test_the_factory_is_the_registered_implementation(self) -> None:
        assert load_implementation("secret_scanner") is build_secret_scanner
        assert isinstance(build_secret_scanner(), SecretScanner)

    def test_this_file_and_the_scanner_source_hold_no_token_shaped_text(
        self, scanner: PatternSecretScanner
    ) -> None:
        import seminars.pipeline.secret_scan as module

        assert module.__file__ is not None
        assert scanner.scan_paths([Path(__file__), Path(module.__file__)]) == []
