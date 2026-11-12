"""How the text and the status of a failed request become one of the failure kinds."""

from __future__ import annotations

import pytest

from seminars.contracts import (
    LLMAuthError,
    LLMNetworkError,
    LLMProviderError,
    LLMQuotaError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from seminars.llm.failures import (
    failure_for_status,
    failure_for_text,
    is_model_problem,
    provider_failure,
)

FAKE_TOKEN = "-".join(["sk", "ant", "oat01", "never", "shown" * 6])


class TestText:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Invalid API key. Please run /login", LLMAuthError),
            (
                'API Error: 401 {"type":"error","error":{"type":"authentication_error"}}',
                LLMAuthError,
            ),
            ("OAuth token has expired. Please obtain a new token", LLMAuthError),
            ("Not logged in. Run codex login", LLMAuthError),
            ("403 Forbidden", LLMAuthError),
            ("Claude AI usage limit reached|1794500000", LLMQuotaError),
            ("You've hit your usage limit. Upgrade to Pro or try again later.", LLMQuotaError),
            ("Your credit balance is too low to access the API", LLMQuotaError),
            ("429 You exceeded your current quota (insufficient_quota)", LLMQuotaError),
            ("402 Payment Required", LLMQuotaError),
            ('API Error: 429 {"error":{"type":"rate_limit_error"}}', LLMRateLimitError),
            ("Too Many Requests", LLMRateLimitError),
            ("API Error: 529 Overloaded", LLMRateLimitError),
            ("Request timed out after 60s", LLMTimeoutError),
            ("504 Gateway Timeout", LLMTimeoutError),
            ("stream disconnected before completion: connection reset", LLMNetworkError),
            ("getaddrinfo ENOTFOUND api.example.com", LLMNetworkError),
            ("fetch failed", LLMNetworkError),
        ],
    )
    def test_known_failures(self, text: str, expected: type[LLMProviderError]) -> None:
        assert failure_for_text(text) is expected

    @pytest.mark.parametrize("text", ["", "something odd happened", "exit 1", "request 14013"])
    def test_unknown_text_has_no_kind(self, text: str) -> None:
        assert failure_for_text(text) is None

    def test_a_missing_credential_wins_over_everything_else(self) -> None:
        text = "429 rate limit; also 401 unauthorized; quota exceeded"
        assert failure_for_text(text) is LLMAuthError

    def test_a_used_up_allowance_wins_over_throttling(self) -> None:
        assert failure_for_text("rate limit: usage limit reached") is LLMQuotaError

    def test_case_does_not_matter(self) -> None:
        assert failure_for_text("UNAUTHORIZED") is LLMAuthError
        assert failure_for_text("Rate Limit Exceeded") is LLMRateLimitError

    def test_a_status_code_inside_a_longer_number_is_not_a_status(self) -> None:
        assert failure_for_text("request id 94011 took 4290 ms") is None


class TestStatus:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, LLMAuthError),
            (403, LLMAuthError),
            (402, LLMQuotaError),
            (429, LLMRateLimitError),
            (529, LLMRateLimitError),
            (408, LLMTimeoutError),
            (504, LLMTimeoutError),
            (400, LLMProviderError),
            (404, LLMProviderError),
            (500, LLMProviderError),
            (503, LLMProviderError),
        ],
    )
    def test_statuses(self, status: int, expected: type[LLMProviderError]) -> None:
        assert failure_for_status(status) is expected

    def test_a_throttled_request_that_names_a_quota_is_a_quota_failure(self) -> None:
        assert failure_for_status(429, "You exceeded your current quota") is LLMQuotaError
        assert failure_for_status(429, "slow down") is LLMRateLimitError

    def test_a_rejected_request_that_names_the_credit_balance_is_a_quota_failure(self) -> None:
        assert failure_for_status(400, "Your credit balance is too low") is LLMQuotaError
        assert failure_for_status(400, "max_tokens: too large") is LLMProviderError

    def test_the_status_decides_when_it_is_about_the_credential(self) -> None:
        assert failure_for_status(401, "quota") is LLMAuthError


class TestModelProblem:
    @pytest.mark.parametrize(
        "text",
        [
            "There's an issue with the selected model (nope). It may not exist or you may not "
            "have access to it.",
            'API Error: 404 {"error":{"type":"not_found_error","message":"model: nope"}}',
            "The model `nope` does not exist or you do not have access to it.",
            "The 'nope' model is not supported when using Codex with a ChatGPT account.",
            "unknown model nope",
        ],
    )
    def test_messages_about_the_model(self, text: str) -> None:
        assert is_model_problem(text)

    @pytest.mark.parametrize(
        "text",
        ["", "401 unauthorized", "file not found", "the model answered", "rate limit reached"],
    )
    def test_other_messages(self, text: str) -> None:
        assert not is_model_problem(text)


class TestMessage:
    def test_a_failure_names_the_provider_and_the_detail_and_nothing_else(self) -> None:
        error = provider_failure(LLMAuthError, "claude-cli", "exit status 1")
        assert isinstance(error, LLMAuthError)
        assert error.provider == "claude-cli"
        assert error.failure_kind == "auth"
        assert str(error) == (
            "claude-cli: the credential is missing, invalid or expired (exit status 1)"
        )

    @pytest.mark.parametrize(
        "kind",
        [
            LLMAuthError,
            LLMQuotaError,
            LLMRateLimitError,
            LLMTimeoutError,
            LLMNetworkError,
            LLMProviderError,
        ],
    )
    def test_every_kind_has_a_fixed_ascii_message(self, kind: type[LLMProviderError]) -> None:
        error = provider_failure(kind, "openai", "HTTP 500")
        assert type(error) is kind
        assert str(error).isascii()
        assert str(error).startswith("openai: ")
        assert str(error).endswith(" (HTTP 500)")

    def test_without_a_detail(self) -> None:
        assert str(provider_failure(LLMNetworkError, "anthropic")) == (
            "anthropic: the provider could not be reached"
        )

    def test_classifying_a_text_never_puts_it_into_the_message(self) -> None:
        text = f"401 unauthorized: token {FAKE_TOKEN} was rejected for user owner@example.org"
        kind = failure_for_text(text)
        assert kind is LLMAuthError
        message = str(provider_failure(kind, "claude-cli", "exit status 1"))
        assert FAKE_TOKEN not in message
        assert "owner@example.org" not in message
