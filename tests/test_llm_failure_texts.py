"""Failure texts seen from the real clients, and the kind each one is given."""

from __future__ import annotations

import json

import pytest

from seminars.contracts import LLMAuthError, LLMProviderError, LLMQuotaError, LLMRateLimitError
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


OPENAI_TPM = (
    "Rate limit reached for gpt-6-luna in organization org-abc123 on tokens per min (TPM): "
    "Limit 200000, Used 199500, Requested 1800. Please try again in 390ms. Visit "
    "https://platform.openai.com/account/rate-limits to learn more."
)
OPENAI_SDK = (
    "Error code: 429 - {'error': {'message': 'Rate limit reached for requests', "
    "'type': 'requests', 'param': None, 'code': 'rate_limit_exceeded'}}"
)
OPENAI_RPD = (
    "Rate limit reached for gpt-6-luna in organization org-abc123 on requests per day (RPD): "
    "Limit 200, Used 200, Requested 1. Please try again in 7m12s. Visit "
    "https://platform.openai.com/account/rate-limits to learn more. You can increase your "
    "rate limit by adding a payment method to your account at "
    "https://platform.openai.com/account/billing."
)
OPENAI_QUOTA = (
    "Error code: 429 - {'error': {'message': 'You exceeded your current quota, please check "
    "your plan and billing details.', 'type': 'insufficient_quota', 'param': None, "
    "'code': 'insufficient_quota'}}"
)
ANTHROPIC_TPM = (
    "This request would exceed the rate limit for your organization of 50,000 input tokens "
    "per minute. For details, refer to the rate limits documentation."
)


@pytest.mark.parametrize("text", [OPENAI_TPM, OPENAI_SDK, OPENAI_RPD, ANTHROPIC_TPM])
def test_the_rate_limit_messages_of_the_apis_stay_rate_limits(text: str) -> None:
    assert failure_for_status(429, text) is LLMRateLimitError
    assert failure_for_text(text) is LLMRateLimitError


def test_a_used_up_api_quota_is_still_a_quota_failure() -> None:
    assert failure_for_status(429, OPENAI_QUOTA) is LLMQuotaError
    assert failure_for_text(OPENAI_QUOTA) is LLMQuotaError


def test_a_bare_limit_reached_is_no_longer_read_as_a_quota() -> None:
    assert failure_for_text("limit reached") is None
    assert failure_for_status(429, "concurrency limit reached") is LLMRateLimitError


@pytest.mark.parametrize(
    "text",
    [
        "TypeError: x is undefined\n    at f (file:///usr/lib/node_modules/cli.js:2:401)",
        "at Object.<anonymous> (cli.js:403:17)",
        "retrying in 401 ms",
        "request 429 of 600",
        "port 4290 and 1429 and 4010",
    ],
)
def test_a_number_that_is_not_called_a_status_is_not_one(text: str) -> None:
    assert failure_for_text(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "HTTP 401",
        "HTTP/1.1 401",
        "status 401",
        "status code: 403",
        "unexpected status 401",
        "API Error: 401 {}",
        'Error code: 403 - {"error": {}}',
        '"api_error_status": 401',
    ],
)
def test_a_status_named_as_such_counts(text: str) -> None:
    assert failure_for_text(text) is LLMAuthError


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("HTTP 429", LLMRateLimitError),
        ("status 402", LLMQuotaError),
        ("error 429", LLMRateLimitError),
    ],
)
def test_other_statuses_named_as_such(text: str, expected: type[LLMProviderError]) -> None:
    assert failure_for_text(text) is expected


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
