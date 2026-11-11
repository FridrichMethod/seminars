"""Fakes for every cross-node protocol, shared by all test modules.

Import them directly (``from fakes import FakeClock``) or use the fixtures
defined in conftest.py. Nothing in here touches the network or the clock.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from factories import REFERENCE_NOW


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
