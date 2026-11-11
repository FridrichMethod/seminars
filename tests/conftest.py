"""Shared pytest fixtures. The fake classes live in fakes.py, the model factories
in factories.py; both modules can also be imported directly by test modules."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import respx

from fakes import FakeClock
from seminars.config import HttpSettings
from seminars.http import OfflineHttpClient, offline_client

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


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


@pytest.fixture
def offline_http() -> Callable[..., OfflineHttpClient]:
    """Factory for offline HTTP clients: ``offline_http("sites-chemistry")``.

    Serves the recordings in tests/fixtures/<source_id>/ of this repository;
    pass ``fixtures_dir=...`` to read from another directory (e.g. tmp_path).
    """

    def build(*source_ids: str, fixtures_dir: Path = FIXTURES_DIR) -> OfflineHttpClient:
        return offline_client(*source_ids, fixtures_dir=fixtures_dir)

    return build
