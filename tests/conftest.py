"""Shared pytest fixtures. The fake classes live in fakes.py, the model factories
in factories.py; both modules can also be imported directly by test modules."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import respx

from fakes import FakeClock
from seminars.config import HttpSettings


@pytest.fixture
def fake_clock() -> FakeClock:
    """A clock that starts at factories.REFERENCE_NOW and never really sleeps."""
    return FakeClock()


@pytest.fixture
def http_settings() -> HttpSettings:
    """HTTP settings with the project defaults (1 s interval, 3 retries, budget 20)."""
    return HttpSettings(user_agent="seminars-bot/1.0 (+https://example.org/seminars/)")


@pytest.fixture
def respx_router() -> Iterator[respx.MockRouter]:
    """A respx router; unmatched requests fail, unused routes are fine."""
    with respx.mock(assert_all_called=False) as router:
        yield router
