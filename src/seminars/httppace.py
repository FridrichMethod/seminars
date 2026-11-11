"""The pace of the live client: whose turn it is on a host, and what a host may be asked.

``Pacer`` sends the requests ``PoliteClient`` has decided on. It makes sure
that a host gets one request at a time (among threads, clients and
processes), waits out the pause the host is owed, keeps the two request
budgets of every host, retries what is worth retrying and remembers a host
that must not be contacted again. The module docstring of ``seminars.http``
states the rules; ``seminars.httpwire`` sends the single requests.
"""

from __future__ import annotations

import contextlib
import logging
import math
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Final

import httpx

from seminars.config import HttpSettings
from seminars.httpbase import (
    RETRY_STATUSES,
    Clock,
    ForbiddenHostError,
    HostBudgetExceededError,
    HttpRequestError,
    Sleeper,
)
from seminars.httpcache import HostRecord, HttpCache
from seminars.httpwire import AttemptFailed, Exchange, Wire
from seminars.robots import RobotsPolicy
from seminars.textrules import ascii_safe

logger = logging.getLogger(__name__)

_REFUSED_EARLIER: Final = (
    "refusing a host whose answer came from a local or private network address earlier in this run"
)

_ROBOTS_COPIES_KEPT: Final = 4
"""How many robots.txt copies of one host are kept in the cache directory (the newest ones)."""


@dataclass
class HostState:
    """Mutable per-host bookkeeping of one client.

    ``lock`` guards one request to the host from start to end, together with
    the host lock of the cache directory (``Pacer.turn``); ``record``
    is brought up to date from that directory whenever the lock is taken.
    ``robots_lock`` makes sure that only one thread works out the robots
    policy of the host at a time. It is taken before ``lock``, never while
    holding it, and a thread never holds two ``robots_lock`` at once.
    """

    record: HostRecord
    lock: threading.Lock = field(default_factory=threading.Lock)
    robots_lock: threading.Lock = field(default_factory=threading.Lock)
    policies: dict[str, RobotsPolicy] = field(default_factory=dict)  # by origin
    robots_failures: dict[str, str] = field(default_factory=dict)  # origin -> why it is blocked
    refused: str | None = None  # why nothing more is sent to the host in this run
    blocked_until: float = 0.0  # end of the current retry backoff
    requests: int = 0  # page requests issued by this client
    robots_requests: int = 0  # robots.txt requests issued by this client, hops to this host too


class Pacer:
    """Per-host turns, pauses, budgets and retries of one client; safe to share between threads."""

    def __init__(
        self,
        settings: HttpSettings,
        *,
        cache: HttpCache,
        wire: Wire,
        clock: Clock,
        sleep: Sleeper,
    ) -> None:
        self._settings = settings
        self._cache = cache
        self._wire = wire
        self._clock = clock
        self._sleep = sleep
        self._hosts: dict[str, HostState] = {}
        self._hosts_lock = threading.Lock()

    def request_counts(self) -> dict[str, int]:
        """Page requests issued so far, per host (robots.txt requests are counted apart)."""
        with self._hosts_lock:
            return {host: state.requests for host, state in sorted(self._hosts.items())}

    def state(self, host: str) -> HostState:
        with self._hosts_lock:
            state = self._hosts.get(host)
            if state is None:
                state = HostState(record=HostRecord())
                self._hosts[host] = state
            return state

    @contextlib.contextmanager
    def turn(self, state: HostState, host: str) -> Iterator[None]:
        """Be the only one who talks to ``host``: among threads, clients and processes.

        Requests are only sent inside this block. Nothing else is locked
        while it is held, so it cannot be part of a deadlock.
        """
        with state.lock, self._cache.host_lock(host):
            # Other clients and processes may have used the host in the meantime.
            now = self._clock()
            state.record = state.record.merge(self._cache.host(host)).trimmed(
                oldest=now - self._settings.robots_cache_seconds,
                newest=now,
                keep=_ROBOTS_COPIES_KEPT,
            )
            yield

    def send(
        self,
        state: HostState,
        host: str,
        url: str,
        headers: Mapping[str, str],
        *,
        interval: float,
        robots: bool = False,
    ) -> Exchange:
        """Send one request with retries; the caller holds the turn of ``host``.

        ``robots`` says which of the two budgets of the host pays for it.
        """
        if state.refused is not None:
            raise ForbiddenHostError(url, state.refused)
        attempts = self._settings.retries + 1
        for attempt in range(1, attempts + 1):
            self._check_budget(state, host, url, robots=robots)
            self._wait_turn(state, interval)
            try:
                exchange = self._attempt(state, host, url, headers, robots=robots)
            except AttemptFailed as error:
                logger.debug("request failed (%s): %s", error.reason, ascii_safe(url))
                if attempt == attempts:
                    raise HttpRequestError(
                        url, f"request failed after {attempts} attempt(s) ({error.reason})"
                    ) from error
                state.blocked_until = self._clock() + self._backoff(attempt)
                continue
            logger.debug("HTTP %d: %s", exchange.status, ascii_safe(url))
            if exchange.status in RETRY_STATUSES and attempt < attempts:
                pause = max(self._backoff(attempt), self._retry_after(exchange.headers))
                state.blocked_until = self._clock() + pause
                continue
            return exchange
        raise AssertionError("unreachable: the retry loop always returns or raises")

    def _attempt(
        self, state: HostState, host: str, url: str, headers: Mapping[str, str], *, robots: bool
    ) -> Exchange:
        """Look the host up, then send the request once.

        ``ForbiddenHostError`` (the name resolves to a non-public address)
        leaves before anything is counted or sent. Everything after that is
        an attempt that uses up budget; ``AttemptFailed`` asks for another.

        When the answer itself is refused because of where it came from,
        the request has left already. The host then gets nothing more from
        this client: the lookup said "public" and the connection went
        somewhere else, and whoever arranged that would arrange it again.
        """
        resolves = self._wire.resolves(url)
        if robots:
            state.robots_requests += 1
        else:
            state.requests += 1
        if not resolves:
            raise AttemptFailed("host name does not resolve")
        try:
            return self._wire.exchange(url, headers)
        except ForbiddenHostError:
            state.refused = _REFUSED_EARLIER
            raise
        finally:
            self._touch(state, host)

    def _check_budget(self, state: HostState, host: str, url: str, *, robots: bool) -> None:
        """Refuse a request that the budget of ``host`` does not cover any more.

        Pages and robots.txt requests have a budget of
        ``max_requests_per_host`` each, counted on the host a request goes
        to. A host therefore gets at most twice that many requests from
        this client, whatever links, ports and redirects point at it.
        """
        limit = self._settings.max_requests_per_host
        used, what = (
            (state.robots_requests, "robots.txt request")
            if robots
            else (
                state.requests,
                "request",
            )
        )
        if used >= limit:
            raise HostBudgetExceededError(
                url, f"{what} budget for host {host} is exhausted ({limit} requests per run)"
            )

    def _wait_turn(self, state: HostState, interval: float) -> None:
        now = self._clock()
        ready = state.blocked_until
        last = state.record.last_request_at
        if last is not None:
            ready = max(ready, min(last, now) + interval)
        if ready > now:
            self._sleep(ready - now)

    def _touch(self, state: HostState, host: str) -> None:
        state.record = state.record.evolve(last_request_at=self._clock())
        self._cache.store_host(host, state.record)

    def _backoff(self, attempt: int) -> float:
        delay = self._settings.backoff_base_seconds * 2 ** (attempt - 1)
        return min(delay, self._settings.backoff_max_seconds)

    def _retry_after(self, headers: httpx.Headers) -> float:
        value = headers.get("retry-after")
        if not value:
            return 0.0
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(value).timestamp() - self._clock()
            except (TypeError, ValueError, OverflowError):
                return 0.0
        if math.isnan(seconds):  # "nan" is a float to Python; it would switch every wait off
            return 0.0
        return min(max(seconds, 0.0), self._settings.backoff_max_seconds)
