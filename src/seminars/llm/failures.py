"""Turning what a failed request left behind into one of the failure kinds.

A backend fails with an exit status and some text (the CLIs), or with an
HTTP status and a message (the APIs). The text is read here to decide what
kind of failure it was, and for nothing else: it can name the account, quote
the request or echo part of a credential, so it never becomes part of an
error message, a log line or the run report. ``provider_failure`` builds
the message from a fixed sentence per kind plus a detail that the caller
writes itself, such as ``exit status 1`` or ``HTTP 429``.

The kinds are the subclasses of ``seminars.contracts.LLMProviderError``. All
of them make the provider chain move on to the next provider; the kind is
what health and the ``llm-auth`` issues report.

A status number in a text only counts after a word that says it is one
(``HTTP 429``, ``status 401``, ``API Error: 401``, ``Error code: 429``), so
a line number in a stack trace (``cli.js:2:401``) is not taken for a
status. The words for throttling that only a rate limit uses ("Rate limit
reached for ...", ``rate_limit_exceeded``, "tokens per min") are read
before the words for a used-up allowance, because the same message of the
OpenAI API also points to the billing page.
"""

from __future__ import annotations

import re
from typing import Final

from seminars.contracts import (
    LLMAuthError,
    LLMNetworkError,
    LLMProviderError,
    LLMQuotaError,
    LLMRateLimitError,
    LLMTimeoutError,
)

_STATUS_WORD: Final = (
    r"(?<![a-z])(?:http(?:/[0-9.]+)?|status(?:[ _]code)?|error(?:[ _]code)?|code)\W{0,3}"
)
"""What must stand right before a status number in a text for it to count as one."""


def _markers(*words: str, statuses: tuple[int, ...] = ()) -> re.Pattern[str]:
    """A pattern for any of ``words`` (plain text) or ``statuses`` (after a status word)."""
    parts = [re.escape(word) for word in words]
    parts += [rf"{_STATUS_WORD}{status}(?!\d)" for status in statuses]
    return re.compile("|".join(parts), re.IGNORECASE)


_AUTH: Final = _markers(
    "unauthorized",
    "authentication",
    "not logged in",
    "invalid api key",
    "invalid_api_key",
    "invalid x-api-key",
    "incorrect api key",
    "oauth token",
    "token has expired",
    "token expired",
    "expired token",
    "/login",
    "login required",
    "please log in",
    "forbidden",
    "permission_error",
    statuses=(401, 403),
)
_QUOTA: Final = _markers(
    "usage limit",
    "usage_limit",
    "hit your limit",
    "hit your weekly limit",
    "hit your daily limit",
    "hit your monthly limit",
    "hit your session limit",
    "daily limit",
    "weekly limit",
    "monthly limit",
    "quota",
    "credit balance",
    "out of credits",
    "billing",
    "spend limit",
    "payment required",
    statuses=(402,),
)
_THROTTLE: Final = _markers(
    "rate limit reached for",
    "rate_limit_exceeded",
    "per min",
)
"""Words only a rate limit uses; they win over the words for a used-up allowance."""
_RATE_LIMIT: Final = _markers(
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "overloaded",
    "throttl",
    statuses=(429, 529),
)
_TIMEOUT: Final = _markers(
    "timed out", "timeout", "time-out", "deadline exceeded", statuses=(408, 504)
)
_NETWORK: Final = _markers(
    "connection",
    "network",
    "disconnected",
    "getaddrinfo",
    "enotfound",
    "econnrefused",
    "econnreset",
    "unreachable",
    "fetch failed",
    "socket hang up",
    "dns ",
    "tls ",
    "certificate",
)

_ORDER: Final[tuple[tuple[re.Pattern[str], type[LLMProviderError]], ...]] = (
    (_AUTH, LLMAuthError),
    (_THROTTLE, LLMRateLimitError),
    (_QUOTA, LLMQuotaError),
    (_RATE_LIMIT, LLMRateLimitError),
    (_TIMEOUT, LLMTimeoutError),
    (_NETWORK, LLMNetworkError),
)
"""Most specific first: a text that names several problems gets the first kind that fits."""

_STATUS_KINDS: Final[dict[int, type[LLMProviderError]]] = {
    401: LLMAuthError,
    403: LLMAuthError,
    402: LLMQuotaError,
    429: LLMRateLimitError,
    529: LLMRateLimitError,
    408: LLMTimeoutError,
    504: LLMTimeoutError,
}

_MODEL: Final = re.compile(r"\bmodels?\b", re.IGNORECASE)
_MODEL_TROUBLE: Final = _markers(
    "not exist",
    "not_found",
    "not found",
    "have access",
    "not supported",
    "unsupported",
    "not available",
    "unavailable",
    "unknown model",
    "invalid model",
    "issue with the selected model",
)

_SENTENCES: Final[dict[type[LLMProviderError], str]] = {
    LLMAuthError: "the credential is missing, invalid or expired",
    LLMQuotaError: "the usage allowance or the credit is used up",
    LLMRateLimitError: "the provider is throttling requests",
    LLMTimeoutError: "no answer within the time limit",
    LLMNetworkError: "the provider could not be reached",
    LLMProviderError: "the provider could not serve the request",
}


def failure_for_text(text: str) -> type[LLMProviderError] | None:
    """The failure kind that ``text`` (an error message, stderr) points to, if any."""
    for pattern, kind in _ORDER:
        if pattern.search(text) is not None:
            return kind
    return None


def failure_for_status(status: int, text: str = "") -> type[LLMProviderError]:
    """The failure kind of an HTTP ``status``, refined by the message that came with it.

    A status that is about the credential decides on its own. Otherwise a
    message in the words of a rate limit ("Rate limit reached for ...")
    makes it a rate limit, and a message that names a quota, the billing or
    the credit balance makes it a quota failure: that is how a used-up
    account answers, with 429 or 400 depending on the service. A status
    without a kind of its own gives the generic ``LLMProviderError``.
    """
    kind = _STATUS_KINDS.get(status, LLMProviderError)
    if kind is LLMAuthError:
        return kind
    if _THROTTLE.search(text) is not None:
        return LLMRateLimitError
    if _QUOTA.search(text) is not None:
        return LLMQuotaError
    return kind


def is_model_problem(text: str) -> bool:
    """Whether ``text`` says that the requested model does not exist or may not be used."""
    return _MODEL.search(text) is not None and _MODEL_TROUBLE.search(text) is not None


def provider_failure(
    kind: type[LLMProviderError], provider: str, detail: str | None = None
) -> LLMProviderError:
    """An error of ``kind`` with its fixed sentence; ``detail`` is appended in parentheses.

    ``detail`` is written by the caller (``exit status 1``, ``HTTP 429``)
    and must never be text that came from the backend.
    """
    sentence = _SENTENCES.get(kind, _SENTENCES[LLMProviderError])
    return kind(f"{sentence} ({detail})" if detail else sentence, provider=provider)
