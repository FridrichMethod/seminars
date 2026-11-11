"""HTTP access for adapters: the polite live client and the offline client.

Adapters only ever see the ``HttpClient`` protocol: ``get(url)`` returns a
``FetchedDocument`` or raises a subclass of ``HttpError``, whatever a site
sends and whatever the cache directory does. The text of a
document is always in canonical form (no byte order mark, LF line endings,
final newline), so a live response and its recorded fixture give an adapter
exactly the same text. There are two implementations:

* ``OfflineHttpClient`` (build it with ``offline_client(source_id)``) serves
  recorded fixtures by URL and raises ``FixtureNotFoundError`` for a URL that
  was never recorded. It backs ``seminars run --offline`` and adapter tests;
  see ``seminars.fixtures`` and tests/fixtures/README.md.
* ``PoliteClient`` is the live client.

Rules of the live client:

* only ``http`` and ``https`` URLs of public hosts: no localhost and no
  private address, whichever way it is written, whatever a name resolves to
  and wherever a connection really ends up (see "Public hosts only" below);
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
  ones, do not see each other. Always build the live client with
  ``cache_dir=paths.http_cache_dir()``: that directory is the same for
  every git worktree of one repository, and ``SEMINARS_HTTP_CACHE_DIR``
  moves it, which separate clones that fetch at the same time need. On
  Windows there is no lock between processes; see ``seminars.httpcache``;
* every host has a request budget per client (``max_requests_per_host``,
  retries included); exceeding it raises ``HostBudgetExceededError``.
  Requests for a robots.txt do not use it up. They have a budget of the
  same size of their own, counted on the host each of them goes to (also
  when it is a redirect hop of another host's robots.txt). One client thus
  sends a host at most ``2 * max_requests_per_host`` requests per run,
  however many links, ports and redirects point at it. A robots.txt costs
  at most ``retries + 1`` requests per hop and origin, whether it can be
  fetched or not; only its first 512000 characters are used;
* network errors and 408/425/429/5xx answers are retried with exponential
  backoff (``Retry-After`` is respected);
* redirects are followed hop by hop so that each hop obeys the rules of its
  own host;
* the headers a caller passes to ``get`` go to the origin of the URL it
  asked for. A redirect to another origin (another scheme, host or port)
  only takes ``Accept`` and ``Accept-Language`` along, so an API key or an
  ``Authorization`` header never reaches whoever a redirect points at.
  robots.txt requests carry none of them;
* cookies are neither stored nor sent: no answer changes a later request,
  and a recorded fixture can reproduce everything an adapter sees;
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

Time, sleeping, name resolution and the question whether an address belongs
to this machine are injected (``clock``, ``sleep``, ``resolver`` and
``own_address``), so tests never wait and never touch DNS or the routing
table.

Public hosts only
-----------------

URLs come from the configuration, from redirects and from scraped pages, and
what is fetched ends up in the public data directory. No request may go to
this machine or to a private network, and no answer from there may be read.
Every request the client sends (a page, a robots.txt, each redirect hop of
either) passes four checks:

1. The spelling of the host (``split_url``, ``is_public_host``). Localhost,
   internal names and non-global IP addresses in any notation are refused
   before anything else happens.
2. The lookup. The name is looked up through ``resolver`` (default: the
   system resolver, ``socket.getaddrinfo``) right before the request. When
   ANY address of the answer is not globally routable, lies in one of the
   networks of ``http.forbidden_networks`` (``is_public_address``) or is an
   address of this machine (``own_address``, default
   ``system_own_address``), nothing is sent and ``ForbiddenHostError`` is
   raised. A host written as an IP address is looked up and judged the same
   way. A refused host costs no request and no budget. (When it is the
   target of a robots.txt redirect, the origin is blocked with
   ``RobotsUnavailableError``, like for any redirect that cannot be
   followed.) A name without an address is not forbidden but unreachable:
   an ordinary failed attempt, retried with backoff and reported as
   ``HttpRequestError`` in the end.
3. The connection. The client opens its connections itself
   (``seminars.httpconnect``): to one of the addresses check 2 has just
   cleared, by address. The name is not looked up a second time, so a DNS
   server cannot give one answer to the check and another to the connection
   (DNS rebinding). Before anything is sent, both ends of the new
   connection are examined; a peer that is not public, that is an address
   of this machine or that is the same address as the local end gets the
   connection closed unused. TLS still verifies the certificate against
   the name.
4. The answer. Before the body is read, the peer of the connection the
   answer came over is examined the same way (the ``network_stream``
   extension of httpx). A refused answer is not read, not cached and not
   retried.

A host that check 3 or 4 refused gets nothing more for the rest of the run:
every later request to it, for any URL or origin, raises
``ForbiddenHostError`` at once, without a lookup or a request.

Checks 1, 2 and 4 hold for every transport. Check 3 belongs to the
client's own transport. A transport that is passed in (the mock transports
of the tests) takes its place, and one without network connections has no
peer, so check 4 has nothing to look at.

What remains:

* With the client's own transport no request leaves for an address that
  was not cleared.
* A transport that is passed in and resolves names by itself makes a second
  lookup, which can differ from the checked one. Check 4 notices, but only
  after the request left: one blind GET per host name and run can reach a
  non-public address. It carries the fixed User-Agent and, when the host is
  the one the caller asked for, the headers the caller passed; never a
  cookie, and this project holds no credentials for any site. Its answer
  is never read and nothing of it is kept.
* An internal host with a globally routable address cannot be recognised
  at all. A machine of a network that uses public addresses behind a
  firewall, or a service of a cloud platform at a public address, looks
  like any site on the internet to every check. Only the address of the
  machine the client runs on is found out (checks 2 to 4). For everything
  else there is ``http.forbidden_networks`` in config/settings.yaml, which
  holds the platform address of Azure by default; list the address ranges
  of your own network there when the client runs inside it.

Proxies are not supported. The client ignores the proxy variables of the
environment (``HTTP_PROXY``, ``HTTPS_PROXY``, ``ALL_PROXY``, ``NO_PROXY``)
together with the other settings httpx would take from there
(``SSL_CERT_FILE``, ``SSL_CERT_DIR``, ``.netrc``); it is built with
``trust_env=False``, and its own transport has no proxy support at all.
Through a proxy the peer of every connection is the proxy, and no check
could tell where a request went.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self
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
    OwnAddressCheck,
    Resolver,
    ResponseTooLargeError,
    RobotsDisallowedError,
    RobotsUnavailableError,
    Sleeper,
    TooManyRedirectsError,
    UnsupportedContentTypeError,
    UnsupportedSchemeError,
    is_binary_media_type,
    media_type,
)
from seminars.httpcache import CachedPage, CachedRobots, HttpCache, header_safe
from seminars.httppace import HostState, Pacer
from seminars.httptext import decode_body
from seminars.httpurl import (
    canonical_host,
    is_public_address,
    is_public_host,
    parse_url,
    split_url,
)
from seminars.httpwire import Exchange, Wire, system_own_address, system_resolver
from seminars.models import FetchedDocument
from seminars.robots import ALLOW_ALL, ROBOTS_PATH, RobotsPolicy, parse_robots
from seminars.textrules import ascii_safe

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
    "OwnAddressCheck",
    "PoliteClient",
    "Resolver",
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
    "is_public_address",
    "is_public_host",
    "media_type",
    "offline_client",
    "split_url",
    "system_own_address",
    "system_resolver",
]


_UNFORESEEN: Final = (
    ValueError,  # UnicodeError is one
    OSError,
    OverflowError,
    httpx.HTTPError,
    httpx.InvalidURL,
    httpx.StreamError,
)
"""What ``get`` turns into ``HttpRequestError`` as a last resort.

Not ``Exception``: a failed assertion or a programming error must stay
visible.
"""

_HEADERS_FOR_ANY_ORIGIN: Final = frozenset({"accept", "accept-language"})
"""Headers of the caller that also go to an origin a redirect leads to; all others stay home."""

_ROBOTS_MAX_CHARS: Final = 512_000
"""How much of a robots.txt is used; RFC 9309 asks a crawler for at least 500 kibibytes."""


def _robots_text(text: str) -> str:
    """The part of a robots.txt that is parsed and kept: its first ``_ROBOTS_MAX_CHARS``.

    The cut is made at the end of the last complete line, so that no rule
    is used half.
    """
    if len(text) <= _ROBOTS_MAX_CHARS:
        return text
    head = text[:_ROBOTS_MAX_CHARS]
    return head[: max(head.rfind("\n"), 0)]


@dataclass(frozen=True)
class _Hop:
    """The outcome of one hop after cache handling."""

    status: int
    content_type: str | None
    text: str
    from_cache: bool = False
    location: str | None = None  # absolute redirect target


class PoliteClient:
    """Live ``HttpClient``; see the module docstring for the rules it follows.

    One instance serves one run: budgets and robots policies are kept per
    instance. It is safe to share between threads, and several instances
    (in one process or in several) coordinate through a common ``cache_dir``.

    ``resolver`` looks host names up before every request (default:
    ``system_resolver``), and ``own_address`` says whether an address is one
    of this machine (default: ``system_own_address``). Tests always pass a
    stub resolver, and the test suite replaces the default of the second.
    Proxy settings of the environment are ignored; see "Public hosts only"
    in the module docstring.
    """

    def __init__(
        self,
        settings: HttpSettings,
        *,
        cache_dir: Path | None = None,
        clock: Clock = time.time,
        sleep: Sleeper = time.sleep,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver | None = None,
        own_address: OwnAddressCheck | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._cache = HttpCache(cache_dir)
        self._wire = Wire(settings, transport=transport, resolver=resolver, own_address=own_address)
        self._pacer = Pacer(settings, cache=self._cache, wire=self._wire, clock=clock, sleep=sleep)

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
        self._wire.close()

    def request_counts(self) -> dict[str, int]:
        """Page requests issued so far, per host (robots.txt requests are counted apart)."""
        return self._pacer.request_counts()

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        allow_statuses: Collection[int] = (),
    ) -> FetchedDocument:
        try:
            return self._get(url, headers, allow_statuses)
        except HttpError:
            raise
        except _UNFORESEEN as error:
            # The promise to callers: a document or an HttpError, whatever a site sends.
            logger.warning("unexpected %s: %s", type(error).__name__, ascii_safe(url))
            raise HttpRequestError(
                url, f"request failed unexpectedly ({type(error).__name__})"
            ) from error

    def _get(
        self, url: str, headers: Mapping[str, str] | None, allow_statuses: Collection[int]
    ) -> FetchedDocument:
        current = url
        home = parse_url(url).origin
        for _ in range(self._settings.max_redirects + 1):
            hop = self._fetch_hop(current, headers, home)
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

    def _fetch_hop(self, url: str, extra_headers: Mapping[str, str] | None, home: str) -> _Hop:
        """One request of a ``get``: ``url`` as written, sent and cached in its canonical form.

        ``home`` is the origin the caller asked for. A redirect that leaves
        it takes only the harmless headers of the caller along.
        """
        target = parse_url(url)
        if extra_headers and target.origin != home:
            extra_headers = {
                name: value
                for name, value in extra_headers.items()
                if name.lower() in _HEADERS_FOR_ANY_ORIGIN
            }
        state = self._pacer.state(target.host)
        policy = self._policy(state, target.host, target.origin)
        if not policy.allows(target.path):
            raise RobotsDisallowedError(url, "disallowed by robots.txt")
        interval = self._interval(url, policy)
        with self._pacer.turn(state, target.host):
            cached = self._cache.page(target.url)
            request_headers = self._request_headers(extra_headers, cached)
            exchange = self._pacer.send(
                state, target.host, target.url, request_headers, interval=interval
            )
            return self._to_hop(target.url, exchange, cached)

    @staticmethod
    def _redirect_target(url: str, exchange: Exchange) -> str | None:
        """The absolute URL a redirect response points to; ``None`` for any other response.

        Raises ``UnsupportedSchemeError`` when the Location cannot be read
        as a URL at all. Whether the result may be fetched is for
        ``parse_url`` to say.
        """
        location = exchange.headers.get("location")
        if exchange.status not in REDIRECT_STATUSES or not location:
            return None
        try:
            return urljoin(url, location)
        except ValueError as error:
            raise UnsupportedSchemeError(location, "invalid URL in a redirect") from error

    def _to_hop(self, url: str, exchange: Exchange, cached: CachedPage | None) -> _Hop:
        status = exchange.status
        target = self._redirect_target(url, exchange)
        if target is not None:
            return _Hop(status, None, "", location=target)
        if status == 304 and cached is not None:
            return _Hop(200, cached.content_type, cached.text, from_cache=True)
        content_type = media_type(exchange.headers.get("content-type"))
        etag = header_safe(exchange.headers.get("etag"))
        last_modified = header_safe(exchange.headers.get("last-modified"))
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

    def _policy(self, state: HostState, host: str, origin: str) -> RobotsPolicy:
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

    def _robots(self, state: HostState, host: str, origin: str) -> CachedRobots:
        """The robots.txt of ``origin``: a recent copy from the cache directory, or fetched now.

        Redirects are followed hop by hop. Each hop takes the turn of the
        host it goes to, so the rules of that host hold for it as well. The
        answer is stored during the turn of the origin's own host; when it
        arrived there, that is the same turn, and no other client can fetch
        the file a second time in between.
        """
        url, hop_state, hop_host = origin + ROBOTS_PATH, state, host
        for hop in range(self._settings.max_redirects + 1):
            with self._pacer.turn(hop_state, hop_host):
                known = self._recent_robots(state, origin) if hop == 0 else None
                if known is not None:
                    return known
                exchange = self._robots_exchange(state, origin, hop_state, hop_host, url)
                try:
                    target = self._redirect_target(url, exchange)
                    next_hop = None if target is None else parse_url(target)
                except HttpError as error:
                    reason = (
                        "robots.txt redirects to an address that must not be fetched; host skipped"
                    )
                    raise self._block_origin(state, origin, reason) from error
                if next_hop is None and hop_state is state:
                    return self._store_robots(state, host, origin, exchange)
            if next_hop is None:
                with self._pacer.turn(state, host):
                    return self._store_robots(state, host, origin, exchange)
            url, hop_host = next_hop.url, next_hop.host
            hop_state = self._pacer.state(hop_host)
        limit = self._settings.max_redirects
        reason = f"robots.txt redirects more than {limit} times; host skipped"
        raise self._block_origin(state, origin, reason)

    def _recent_robots(self, state: HostState, origin: str) -> CachedRobots | None:
        """The stored robots.txt of ``origin`` if it is young enough to be used."""
        robots = state.record.robots.get(origin)
        if robots is None:
            return None
        age = self._clock() - robots.fetched_at
        return robots if 0 <= age <= self._settings.robots_cache_seconds else None

    def _robots_exchange(
        self, state: HostState, origin: str, hop_state: HostState, hop_host: str, url: str
    ) -> Exchange:
        """One request for the robots.txt of ``origin``; a failure blocks that origin.

        The caller holds the turn of ``hop_host``.
        """
        interval = max(self._settings.min_interval_seconds, self._known_crawl_delay(hop_state))
        try:
            return self._pacer.send(hop_state, hop_host, url, {}, interval=interval, robots=True)
        except ForbiddenHostError as error:
            if hop_state is state:
                raise  # the host of the origin itself: there is nothing to fetch from it
            reason = "robots.txt redirects to an address that must not be fetched; host skipped"
            raise self._block_origin(state, origin, reason) from error
        except HostBudgetExceededError as error:
            if hop_state is state:
                raise  # nothing more is sent to this host in this run anyway
            reason = "robots.txt redirects to a host whose request budget is used up; host skipped"
            raise self._block_origin(state, origin, reason) from error
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

    def _known_crawl_delay(self, state: HostState) -> float:
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
        self, state: HostState, host: str, origin: str, exchange: Exchange
    ) -> CachedRobots:
        """Turn the final answer into the robots.txt copy of ``origin`` and remember it.

        The caller holds the turn of ``host``.
        """
        if 200 <= exchange.status < 300:
            text: str | None = _robots_text(exchange.text)
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
    def _block_origin(state: HostState, origin: str, reason: str) -> RobotsUnavailableError:
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
