"""Fakes for every cross-node protocol, shared by all test modules.

Import them directly (``from fakes import FakeClock``) or use the fixtures
defined in conftest.py. Nothing in here touches the network or the clock.
"""

from __future__ import annotations

import threading
from collections.abc import Collection, Mapping
from datetime import UTC, datetime

from factories import REFERENCE_NOW
from seminars.http import FixtureNotFoundError, HttpStatusError
from seminars.models import FetchedDocument


class FakeClock:
    """Deterministic time: calling it reads the time, ``sleep`` advances it.

    Pass the instance as ``clock`` and its ``sleep`` method as ``sleep`` to
    anything that takes injectable time, for example ``PoliteClient``.
    """

    def __init__(self, start: datetime = REFERENCE_NOW) -> None:
        self._now = start.timestamp()
        self._lock = threading.Lock()
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def now(self) -> datetime:
        """Current fake time as an aware UTC datetime."""
        return datetime.fromtimestamp(self(), tz=UTC)

    def sleep(self, seconds: float) -> None:
        """Record the request to sleep and jump ahead without waiting."""
        with self._lock:
            self.sleeps.append(seconds)
            self._now += seconds

    def advance(self, seconds: float) -> None:
        """Move time forward without recording a sleep."""
        with self._lock:
            self._now += seconds


class FakeHttpClient:
    """In-memory ``HttpClient``: serves the pages given to it and records requests.

    Use it when a test needs a few hand-written responses; use
    ``offline_client`` for recorded fixtures. Unknown URLs raise
    ``FixtureNotFoundError`` and error statuses behave like the live client.
    """

    def __init__(self, pages: Mapping[str, str] | None = None, *, clock: FakeClock | None = None):
        self._clock = clock or FakeClock()
        self._documents: dict[str, FetchedDocument] = {}
        self.requests: list[str] = []
        for url, text in (pages or {}).items():
            self.add(url, text)

    def add(
        self,
        url: str,
        text: str,
        *,
        status: int = 200,
        content_type: str | None = "text/html",
        final_url: str | None = None,
    ) -> None:
        """Register the response for ``url``."""
        self._documents[url] = FetchedDocument(
            url=url,
            final_url=final_url or url,
            status=status,
            content_type=content_type,
            text=text,
            fetched_at=self._clock.now(),
        )

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        allow_statuses: Collection[int] = (),
    ) -> FetchedDocument:
        self.requests.append(url)
        document = self._documents.get(url)
        if document is None:
            raise FixtureNotFoundError(url, "FakeHttpClient has no page for this URL")
        if not 200 <= document.status < 300 and document.status not in allow_statuses:
            raise HttpStatusError(url, document.status)
        return document.evolve(fetched_at=self._clock.now())
