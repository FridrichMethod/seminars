"""The secret scanner finds tokens in serialized text: after escapes and glued to words."""

from __future__ import annotations

import json

import pytest

from seminars.pipeline.secret_scan import build_secret_scanner

ALNUM = "Q1w2E3r4T5y6U7i8O9p0"

SAMPLES: dict[str, str] = {
    "github-token": "gh" + "p_" + ALNUM * 2,
    "anthropic-key": "sk-" + "ant-" + "oat01-" + ALNUM * 3,
    "openai-key": "s" + "k-" + ALNUM * 2,
    "bearer-token": "Bear" + "er " + ALNUM * 2,
    "token-keyword": "refresh" + "_token=" + ALNUM,
    "jwt": ".".join(["ey" + "J" + ALNUM, "ey" + "J" + ALNUM, ALNUM]),
}


@pytest.mark.parametrize("rule", sorted(SAMPLES))
@pytest.mark.parametrize("before", ["\n", "\t", "\u00a0", "\u2028"])
def test_a_token_after_an_escape_in_json_is_found(rule: str, before: str) -> None:
    text = json.dumps({"abstract": "Use this:" + before + SAMPLES[rule]}) + "\n"
    found = build_secret_scanner().scan_text(text, origin="data/events.json")
    assert rule in {finding.rule for finding in found}


@pytest.mark.parametrize("rule", ["github-token", "anthropic-key"])
def test_a_distinctive_prefix_is_found_inside_a_word(rule: str) -> None:
    scanner = build_secret_scanner()
    text = "\x1b[31m" + SAMPLES[rule]
    assert rule in {finding.rule for finding in scanner.scan_text(text, origin="x")}
    assert SAMPLES[rule] not in scanner.redact(text)


def test_the_generic_openai_shape_inside_a_word_is_not_a_token() -> None:
    text = "a task-" + ALNUM * 2 + " and a disk-" + ALNUM * 2
    assert build_secret_scanner().scan_text(text, origin="x") == []
