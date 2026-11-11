"""docs/LLM_CREDENTIALS.md and its Chinese twin: the same content, and true to the code."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Final

import pytest

from seminars.config import PROVIDER_CREDENTIAL_ENV, PROVIDER_IDS
from seminars.llm.claude_cli import MAX_BUDGET_USD, QUIET_ENV, claude_argv
from seminars.llm.codex_cli import DISABLED_FEATURES, codex_argv

DOCS: Final = Path(__file__).resolve().parents[1] / "docs"
ENGLISH: Final = DOCS / "LLM_CREDENTIALS.md"
CHINESE: Final = DOCS / "LLM_CREDENTIALS.zh-CN.md"
FENCE: Final = re.compile(r"^ *```\n(.*?)^ *```$", re.S | re.M)
OPENAI_QUOTES: Final = (
    '"The right way to authenticate automation is with an API key."',
    '"Do not use this workflow for public or open-source repositories."',
    '"trusted private infrastructure"',
)


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def code_blocks(document: str) -> list[str]:
    return FENCE.findall(document)


def inline_code(document: str) -> Counter[str]:
    prose = FENCE.sub("", document)
    return Counter(re.findall(r"`([^`\n]+)`", prose))


def heading_levels(document: str) -> list[int]:
    return [len(match.group(1)) for match in re.finditer(r"^(#+) ", FENCE.sub("", document), re.M)]


def block_with(document: str, start: str) -> str:
    [block] = [block for block in code_blocks(document) if block.startswith(start)]
    return block


class TestSameContent:
    def test_the_two_versions_link_to_each_other_at_the_top(self) -> None:
        assert text(ENGLISH).splitlines()[0] == "English | [Chinese](LLM_CREDENTIALS.zh-CN.md)"
        assert text(CHINESE).splitlines()[0] == "[English](LLM_CREDENTIALS.md) | \u4e2d\u6587"

    def test_the_same_sections(self) -> None:
        assert heading_levels(text(ENGLISH)) == heading_levels(text(CHINESE))

    def test_the_same_commands_word_for_word(self) -> None:
        assert code_blocks(text(ENGLISH)) == code_blocks(text(CHINESE))

    def test_the_same_names_commands_and_values_in_the_text(self) -> None:
        assert inline_code(text(ENGLISH)) == inline_code(text(CHINESE))

    def test_the_same_tables(self) -> None:
        def rows(document: str) -> list[int]:
            return [line.count("|") for line in document.splitlines() if line.startswith("|")]

        assert rows(text(ENGLISH)) == rows(text(CHINESE))

    @pytest.mark.parametrize("path", [ENGLISH, CHINESE])
    def test_openai_is_quoted_on_why_codex_stays_out_of_ci(self, path: Path) -> None:
        prose = " ".join(text(path).split())
        for quote in OPENAI_QUOTES:
            assert quote in prose


class TestTrueToTheCode:
    @pytest.mark.parametrize("path", [ENGLISH, CHINESE])
    def test_every_provider_and_credential_is_described(self, path: Path) -> None:
        document = text(path)
        for provider in (*PROVIDER_IDS, "none"):
            assert f"`{provider}`" in document
        for variable in PROVIDER_CREDENTIAL_ENV.values():
            if variable is not None:
                assert f"`{variable}`" in document

    def test_the_claude_command_is_the_one_the_code_runs(self) -> None:
        block = block_with(text(ENGLISH), "claude -p")
        argv = claude_argv("claude", model="<model>", system="<system prompt>", schema={})
        words = " ".join(block.replace("\\\n", " ").split())
        for argument in argv[1:]:
            if argument in ("", "{}", "<system prompt>"):
                continue
            assert argument in words, argument
        assert f"--max-budget-usd {MAX_BUDGET_USD}" in words
        for name in QUIET_ENV:
            assert f"`{name}=1`" in text(ENGLISH)

    def test_the_codex_command_is_the_one_the_code_runs(self) -> None:
        block = block_with(text(ENGLISH), "codex exec")
        argv = codex_argv(
            "codex",
            model="<model>",
            workdir=Path("<empty temporary directory>"),
            schema_file=Path("<schema.json>"),
            answer_file=Path("<answer.json>"),
        )
        words = " ".join(block.replace("\\\n", " ").split())
        quoted = (f"'{argument}'" if '"' in argument else argument for argument in argv)
        assert words == " ".join(quoted) + " < prompt"
        for feature in DISABLED_FEATURES:
            assert f"--disable {feature}" in words

    @pytest.mark.parametrize("path", [ENGLISH, CHINESE])
    def test_no_secret_of_the_dropped_codex_set_up_is_named(self, path: Path) -> None:
        for name in ("CODEX" + "_AUTH_JSON", "SECRETS" + "_WRITER_PAT"):
            assert name not in text(path)

    def test_the_note_on_the_disable_list_matches_the_list(self) -> None:
        """The documents name the five flags that matter today and count the other twelve."""
        effective = {"shell_tool", "view_image", "sleep_tool", "apps", "plugins"}
        assert effective <= set(DISABLED_FEATURES)
        assert len(DISABLED_FEATURES) - len(effective) == 12
        assert "the other twelve change" in " ".join(text(ENGLISH).split())
        assert "\u5176\u4f59\u5341\u4e8c\u4e2a" in text(CHINESE)
