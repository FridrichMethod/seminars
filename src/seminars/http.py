"""HTTP access for adapters: the polite live client and the offline client.

Adapters only ever see the ``HttpClient`` protocol: ``get(url)`` returns a
``FetchedDocument`` or raises a subclass of ``HttpError``. The text of a
document is always in canonical form (no byte order mark, LF line endings,
final newline), so a live response and its recorded fixture give an adapter
exactly the same text. There are two implementations:

* ``OfflineHttpClient`` (build it with ``offline_client(source_id)``) serves
  recorded fixtures by URL and raises ``FixtureNotFoundError`` for a URL that
  was never recorded. It backs ``seminars run --offline`` and adapter tests;
  see ``seminars.fixtures`` and tests/fixtures/README.md.
* ``PoliteClient`` is the live client.

Rules of the live client:

* only ``http`` and ``https`` URLs of public hosts (no localhost, no private
  addresses, whichever way the address is written; see ``is_public_host``);
  a fixed User-Agent from the settings;
* robots.txt is fetched once per origin and honoured for ``Disallow`` and
  ``Crawl-delay``; a missing one (4xx other than 429) allows everything
  (RFC 9309). An unreachable one (network error, 5xx, 429, not a text
  file, too large, or a redirect that cannot be followed) blocks the origin
  for the rest of the run: the failure is remembered, so every later
  request to that origin raises ``RobotsUnavailableError`` at once, without
  contacting the host again;
* a redirect of robots.txt is followed like any other, one hop at a time
  and at most ``max_redirects`` times: every hop must point at a public
  http(s) address and waits for its turn on the host it goes to;
* the pause between two requests to one host is
  ``max(min_interval_seconds, Crawl-delay)``, measured from the end of the
  previous request. The Crawl-delay is the one of the origin the request
  goes to. A request for a robots.txt cannot know that one yet: it waits
  for the longest Crawl-delay known for any origin of its host (http and
  https, other ports), at most ``max_crawl_delay_seconds``;
* requests to one host are strictly serial, different hosts may be used
  concurrently from several threads;
* both rules also hold between clients and between processes, provided they
  share one ``cache_dir``: every request is made while holding the lock of
  its host in that directory, and the time of the last request is read from
  there after the lock was taken. Parallel runs wait for each other instead
  of overlapping. Clients without a cache directory, or with different
  ones, do not see each other: checkouts that fetch from the same hosts at
  the same time (git worktrees) must be given a common directory through
  ``SEMINARS_HTTP_CACHE_DIR``. Always build the live client with
  ``cache_dir=paths.http_cache_dir()``, which honours that variable. On
  Windows there is no lock between processes; see ``seminars.httpcache``;
* every host has a request budget per client (``max_requests_per_host``,
  retries included); exceeding it raises ``HostBudgetExceededError``.
  robots.txt does not count: it costs at most ``retries + 1`` requests per
  hop, origin and run, whether it can be fetched or not;
* network errors and 408/425/429/5xx answers are retried with exponential
  backoff (``Retry-After`` is respected);
* redirects are followed hop by hop so that each hop obeys the rules of its
  own host;
* responses with an ETag or Last-Modified are cached under ``cache_dir`` and
  revalidated with a conditional request;
* images and other binary media types are refused before the body is read;
* a body is read in pieces and given up as soon as it exceeds
  ``max_body_bytes`` (counted after decompression), which raises
  ``ResponseTooLargeError``;
* bodies are decoded by ``decode_body``: a byte order mark decides first,
  then the charset of the Content-Type header, then the encoding the
  document itself declares (XML declaration, HTML ``<meta>``); undeclared
  text is UTF-8 when it is valid UTF-8 and windows-1252 otherwise.

Time and sleeping are injected (``clock`` and ``sleep``), so tests never wait.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import TracebackType
from typing import Self
from urllib.parse import urljoin

import httpx

from seminars.config import HttpSettings
from seminars.fixtures import FixtureNotFoundError, OfflineHttpClient, offline_client
from seminars.httpbase import (
    BINARY_MEDIA_PREFIXES,
    BINARY_MEDIA_TYPES,
    REDIRECT_STATUSES,
    RETRY_STATUSES,
    Clock,
    CrawlDelayTooLongError,
    ForbiddenHostError,
    HostBudgetExceededError,
    HttpClient,
    HttpError,
    HttpRequestError,
    HttpStatusError,
    ResponseTooLargeError,
    RobotsDisallowedError,
    RobotsUnavailableError,
    Sleeper,
    TooManyRedirectsError,
    UnsupportedContentTypeError,
    UnsupportedSchemeError,
    ascii_safe,
    canonical_host,
    decode_body,
    is_binary_media_type,
    is_public_host,
    media_type,
    split_url,
)
from seminars.httpcache import CachedPage, CachedRobots, HostRecord, HttpCache
from seminars.models import FetchedDocument
from seminars.robots import ALLOW_ALL, ROBOTS_PATH, RobotsPolicy, parse_robots

logger = logging.getLogger(__name__)

__all__ = [
    "BINARY_MEDIA_PREFIXES",
    "BINARY_MEDIA_TYPES",
    "REDIRECT_STATUSES",
    "RETRY_STATUSES",
    "Clock",
    "CrawlDelayTooLongError",
    "FixtureNotFoundError",
    "ForbiddenHostError",
    "HostBudgetExceededError",
    "HttpClient",
    "HttpError",
    "HttpRequestError",
    "HttpStatusError",
    "OfflineHttpClient",
    "PoliteClient",
    "ResponseTooLargeError",
    "RobotsDisallowedError",
    "RobotsUnavailableError",
    "Sleeper",
    "TooManyRedirectsError",
    "UnsupportedContentTypeError",
    "UnsupportedSchemeError",
    "ascii_safe",
    "canonical_host",
    "decode_body",
    "is_binary_media_type",
    "is_public_host",
    "media_type",
    "offline_client",
    "split_url",
]


@dataclass(frozen=True)
class _Exchange:
    """One HTTP response as received."""

    status: int
    headers: httpx.Headers
    text: str


@dataclass(frozen=True)
class _Hop:
    """The outcome of one hop after cache handling."""

    status: int
    content_type: str | None
    text: str
    from_cache: bool = False
    location: str | None = None  # absolute redirect target


@dataclass
class _HostState:
    """Mutable per-host bookkeeping of one client.

    ``lock`` guards one request to the host from start to end, together with
    the host lock of the cache directory (``PoliteClient._turn``); ``record``
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
    blocked_until: float = 0.0  # end of the current retry backoff
    requests: int = 0  # budgeted requests issued by this client


class PoliteClient:
    """Live ``HttpClient``; see the module docstring for the rules it follows.

    One instance serves one run: budgets and robots policies are kept per
    instance. It is safe to share between threads, and several instances
    (in one process or in several) coordinate through a common ``cache_dir``.
    """

    def __init__(
        self,
        settings: HttpSettings,
        *,
        cache_dir: Path | None = None,
        clock: Clock = time.time,
        sleep: Sleeper = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        self._cache = HttpCache(cache_dir)
        self._client = httpx.Client(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )
        self._hosts: dict[str, _HostState] = {}
        self._hosts_lock = threading.Lock()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release the underlying connections."""
        self._client.close()

    def request_counts(self) -> dict[str, int]:
        """Budgeted requests issued so far, per host."""
        with self._hosts_lock:
            return {host: state.requests for host, state in sorted(self._hosts.items())}

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        allow_statuses: Collection[int] = (),
    ) -> FetchedDocument:
        current = url
        for _ in range(self._settings.max_redirects + 1):
            hop = self._fetch_hop(current, headers)
            if hop.location is None:
                break
            current = hop.location
        else:
            raise TooManyRedirectsError(url, f"more than {self._settings.max_redirects} redirects")
        if not 200 <= hop.status < 300 and hop.status not in allow_statuses:
            raise HttpStatusError(url, hop.status)
        return FetchedDocument(
            url=url,
            final_url=current,
            status=hop.status,
            content_type=hop.content_type,
            text=hop.text,
            fetched_at=datetime.fromtimestamp(self._clock(), tz=UTC),
            from_cache=hop.from_cache,
        )

    # --- one hop ---------------------------------------------------------------------------

    def _fetch_hop(self, url: str, extra_headers: Mapping[str, str] | None) -> _Hop:
        origin, host, path = split_url(url)
        state = self._state(host)
        policy = self._policy(state, host, origin)
        if not policy.allows(path):
            raise RobotsDisallowedError(url, "disallowed by robots.txt")
        interval = self._interval(url, policy)
        with self._turn(state, host):
            cached = self._cache.page(url)
            request_headers = self._request_headers(extra_headers, cached)
            exchange = self._send(state, host, url, request_headers, interval=interval)
            return self._to_hop(url, exchange, cached)

    @contextlib.contextmanager
    def _turn(self, state: _HostState, host: str) -> Iterator[None]:
        """Be the only one who talks to ``host``: among threads, clients and processes.

        Requests are only sent inside this block. Nothing else is locked
        while it is held, so it cannot be part of a deadlock.
        """
        with state.lock, self._cache.host_lock(host):
            # Other clients and processes may have used the host in the meantime.
            state.record = state.record.merge(self._cache.host(host))
            yield

    @staticmethod
    def _redirect_target(url: str, exchange: _Exchange) -> str | None:
        """The absolute URL a redirect response points to; ``None`` for any other response."""
        location = exchange.headers.get("location")
        if exchange.status not in REDIRECT_STATUSES or not location:
            return None
        return urljoin(url, location)

    def _to_hop(self, url: str, exchange: _Exchange, cached: CachedPage | None) -> _Hop:
        status = exchange.status
        target = self._redirect_target(url, exchange)
        if target is not None:
            return _Hop(status, None, "", location=target)
        if status == 304 and cached is not None:
            return _Hop(200, cached.content_type, cached.text, from_cache=True)
        content_type = media_type(exchange.headers.get("content-type"))
        etag = exchange.headers.get("etag")
        last_modified = exchange.headers.get("last-modified")
        if status == 200 and (etag or last_modified):
            self._cache.store_page(
                CachedPage(
                    url=url,
                    etag=etag,
                    last_modified=last_modified,
                    content_type=content_type,
                    text=exchange.text,
                )
            )
        return _Hop(status, content_type, exchange.text)

    @staticmethod
    def _request_headers(
        extra_headers: Mapping[str, str] | None, cached: CachedPage | None
    ) -> dict[str, str]:
        headers = {
            name: value
            for name, value in (extra_headers or {}).items()
            if name.lower() != "user-agent"
        }
        if cached is not None and cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached is not None and cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified
        return headers

    # --- robots.txt ------------------------------------------------------------------------

    def _policy(self, state: _HostState, host: str, origin: str) -> RobotsPolicy:
        """The robots policy of ``origin``, worked out once per client."""
        with state.robots_lock:
            policy = state.policies.get(origin)
            if policy is None:
                failure = state.robots_failures.get(origin)
                if failure is not None:
                    raise RobotsUnavailableError(origin + ROBOTS_PATH, failure)
                robots = self._robots(state, host, origin)
                if robots.text is None:
                    policy = ALLOW_ALL
                else:
                    policy = parse_robots(robots.text, self._settings.robots_token)
                state.policies[origin] = policy
            return policy

    def _robots(self, state: _HostState, host: str, origin: str) -> CachedRobots:
        """The robots.txt of ``origin``: a recent copy from the cache directory, or fetched now.

        Redirects are followed hop by hop. Each hop takes the turn of the
        host it goes to, so the rules of that host hold for it as well. The
        answer is stored during the turn of the origin's own host; when it
        arrived there, that is the same turn, and no other client can fetch
        the file a second time in between.
        """
        url, hop_state, hop_host = origin + ROBOTS_PATH, state, host
        for hop in range(self._settings.max_redirects + 1):
            with self._turn(hop_state, hop_host):
                known = self._recent_robots(state, origin) if hop == 0 else None
                if known is not None:
                    return known
                exchange = self._robots_exchange(state, origin, hop_state, hop_host, url)
                target = self._redirect_target(url, exchange)
                if target is None and hop_state is state:
                    return self._store_robots(state, host, origin, exchange)
            if target is None:
                with self._turn(state, host):
                    return self._store_robots(state, host, origin, exchange)
            url = target
            try:
                _, hop_host, _ = split_url(url)
            except HttpError as error:
                reason = "robots.txt redirects to an address that must not be fetched; host skipped"
                raise self._block_origin(state, origin, reason) from error
            hop_state = self._state(hop_host)
        limit = self._settings.max_redirects
        reason = f"robots.txt redirects more than {limit} times; host skipped"
        raise self._block_origin(state, origin, reason)

    def _recent_robots(self, state: _HostState, origin: str) -> CachedRobots | None:
        """The stored robots.txt of ``origin`` if it is young enough to be used."""
        robots = state.record.robots.get(origin)
        if robots is None:
            return None
        age = self._clock() - robots.fetched_at
        return robots if 0 <= age <= self._settings.robots_cache_seconds else None

    def _robots_exchange(
        self, state: _HostState, origin: str, hop_state: _HostState, hop_host: str, url: str
    ) -> _Exchange:
        """One request for the robots.txt of ``origin``; a failure blocks that origin.

        The caller holds the turn of ``hop_host``.
        """
        interval = max(self._settings.min_interval_seconds, self._known_crawl_delay(hop_state))
        try:
            return self._send(hop_state, hop_host, url, {}, interval=interval, budgeted=False)
        except HttpRequestError as error:
            reason = "robots.txt is unreachable; host skipped"
            raise self._block_origin(state, origin, reason) from error
        except UnsupportedContentTypeError as error:
            reason = "robots.txt is not a text file; host skipped"
            raise self._block_origin(state, origin, reason) from error
        except ResponseTooLargeError as error:
            limit = self._settings.max_body_bytes
            reason = f"robots.txt is larger than {limit} bytes; host skipped"
            raise self._block_origin(state, origin, reason) from error

    def _known_crawl_delay(self, state: _HostState) -> float:
        """The longest crawl delay any origin of the host is known to ask for, capped.

        A robots.txt request cannot follow the policy of its own origin,
        which is what it is about to find out. It respects what the other
        origins of the same host (http and https, other ports) asked for:
        the policies this client worked out and the recent robots.txt copies
        that other clients left in the cache directory.
        """
        delays = [policy.crawl_delay or 0.0 for policy in tuple(state.policies.values())]
        for origin in tuple(state.record.robots):
            robots = self._recent_robots(state, origin)
            if robots is not None and robots.text is not None:
                policy = parse_robots(robots.text, self._settings.robots_token)
                delays.append(policy.crawl_delay or 0.0)
        return min(max(delays, default=0.0), self._settings.max_crawl_delay_seconds)

    def _store_robots(
        self, state: _HostState, host: str, origin: str, exchange: _Exchange
    ) -> CachedRobots:
        """Turn the final answer into the robots.txt copy of ``origin`` and remember it.

        The caller holds the turn of ``host``.
        """
        if 200 <= exchange.status < 300:
            text: str | None = exchange.text
        elif 400 <= exchange.status < 500 and exchange.status != 429:
            text = None
        else:
            reason = f"robots.txt answered HTTP {exchange.status}; host skipped"
            raise self._block_origin(state, origin, reason)
        robots = CachedRobots(fetched_at=self._clock(), text=text)
        state.record = state.record.evolve(robots={**state.record.robots, origin: robots})
        self._cache.store_host(host, state.record)
        return robots

    @staticmethod
    def _block_origin(state: _HostState, origin: str, reason: str) -> RobotsUnavailableError:
        """Remember that ``origin`` is off limits for this run; return the error to raise."""
        state.robots_failures[origin] = reason
        return RobotsUnavailableError(origin + ROBOTS_PATH, reason)

    def _interval(self, url: str, policy: RobotsPolicy) -> float:
        delay = policy.crawl_delay or 0.0
        limit = self._settings.max_crawl_delay_seconds
        if delay > limit:
            raise CrawlDelayTooLongError(
                url, f"robots.txt asks for a crawl delay of {delay:g} s (limit {limit:g} s)"
            )
        return max(self._settings.min_interval_seconds, delay)

    # --- sending with rate limit, budget and retries ---------------------------------------

    def _state(self, host: str) -> _HostState:
        with self._hosts_lock:
            state = self._hosts.get(host)
            if state is None:
                state = _HostState(record=HostRecord())
                self._hosts[host] = state
            return state

    def _send(
        self,
        state: _HostState,
        host: str,
        url: str,
        headers: Mapping[str, str],
        *,
        interval: float,
        budgeted: bool = True,
    ) -> _Exchange:
        """Send one request with retries; the caller holds the turn of ``host``."""
        attempts = self._settings.retries + 1
        for attempt in range(1, attempts + 1):
            if budgeted:
                self._spend(state, host, url)
            self._wait_turn(state, interval)
            try:
                exchange = self._exchange(url, headers)
            except httpx.RequestError as error:
                reason = type(error).__name__
                logger.debug("request failed (%s): %s", reason, ascii_safe(url))
                if attempt == attempts:
                    raise HttpRequestError(
                        url, f"request failed after {attempts} attempt(s) ({reason})"
                    ) from error
                state.blocked_until = self._clock() + self._backoff(attempt)
                continue
            finally:
                self._touch(state, host)
            logger.debug("HTTP %d: %s", exchange.status, ascii_safe(url))
            if exchange.status in RETRY_STATUSES and attempt < attempts:
                pause = max(self._backoff(attempt), self._retry_after(exchange.headers))
                state.blocked_until = self._clock() + pause
                continue
            return exchange
        raise AssertionError("unreachable: the retry loop always returns or raises")

    def _exchange(self, url: str, headers: Mapping[str, str]) -> _Exchange:
        try:
            with self._client.stream("GET", url, headers=dict(headers)) as response:
                content_type = media_type(response.headers.get("content-type"))
                if is_binary_media_type(content_type):
                    raise UnsupportedContentTypeError(url, content_type)
                body = self._read_body(url, response)
                text = decode_body(body, response.headers.get("content-type"))
                return _Exchange(response.status_code, response.headers, text)
        except httpx.InvalidURL as error:
            raise UnsupportedSchemeError(url, "invalid URL") from error

    def _read_body(self, url: str, response: httpx.Response) -> bytes:
        """Read the body in pieces and stop as soon as it is larger than ``max_body_bytes``."""
        limit = self._settings.max_body_bytes
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise ResponseTooLargeError(url, limit)
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > limit:
                raise ResponseTooLargeError(url, limit)
            chunks.append(chunk)
        return b"".join(chunks)

    def _spend(self, state: _HostState, host: str, url: str) -> None:
        limit = self._settings.max_requests_per_host
        if state.requests >= limit:
            raise HostBudgetExceededError(
                url, f"request budget for host {host} is exhausted ({limit} requests per run)"
            )
        state.requests += 1

    def _wait_turn(self, state: _HostState, interval: float) -> None:
        now = self._clock()
        ready = state.blocked_until
        last = state.record.last_request_at
        if last is not None:
            ready = max(ready, min(last, now) + interval)
        if ready > now:
            self._sleep(ready - now)

    def _touch(self, state: _HostState, host: str) -> None:
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
            except (TypeError, ValueError):
                return 0.0
        return min(max(seconds, 0.0), self._settings.backoff_max_seconds)
