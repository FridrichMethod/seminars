"""Failure texts seen from the real clients, and the kind each one is given."""

from __future__ import annotations

import json

import pytest

from seminars.contracts import LLMQuotaError, LLMRateLimitError
from seminars.llm.claude_cli import ClaudeCliProvider
from seminars.llm.cli import CliResult
from seminars.llm.failures import failure_for_status, failure_for_text
from seminars.models import Model
from test_llm_fakes import FakeRunner

WEEKLY_LIMIT = "You've hit your weekly limit - resets Nov 13, 9am (America/Los_Angeles)"
"""What Claude Code 2.1.322 put into ``result`` when the subscription was used up (2026-11-11)."""


class Ping(Model):
    ok: bool


@pytest.mark.parametrize(
    "text",
    [
        WEEKLY_LIMIT,
        "You've hit your daily limit",
        "You've hit your monthly limit, it resets on Dec 1",
        "You have reached your weekly limit",
        "Claude AI usage limit reached|1763053200",
    ],
)
def test_a_used_up_subscription_is_a_quota_failure(text: str) -> None:
    assert failure_for_text(text) is LLMQuotaError
    assert failure_for_status(429, text) is LLMQuotaError


def test_a_plain_429_is_still_a_rate_limit() -> None:
    assert failure_for_status(429, "Too many requests, slow down") is LLMRateLimitError


def test_the_claude_cli_reports_a_used_up_subscription_as_quota_without_its_text() -> None:
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "api_error_status": 429,
        "result": WEEKLY_LIMIT,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    runner = FakeRunner(CliResult(1, json.dumps(result) + "\n", ""))
    provider = ClaudeCliProvider(model="haiku", token=None, timeout=5, env={}, runner=runner)
    with pytest.raises(LLMQuotaError) as caught:
        provider.complete("system", "user", Ping, max_tokens=10)
    assert "HTTP 429" in str(caught.value)
    assert "weekly" not in str(caught.value)
    assert "resets" not in str(caught.value)
