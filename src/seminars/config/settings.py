"""Schema of config/settings.yaml."""

from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Annotated, Self

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from seminars.httpurl import DEFAULT_FORBIDDEN_NETWORKS
from seminars.models import Model, SourceConfig, TimeWindow
from seminars.models.base import HttpUrlStr, NonEmptyStr, TimezoneName

PositiveInt = Annotated[int, Field(ge=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
Hostname = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")]


def _network(value: str) -> str:
    """``value`` as a network in CIDR notation, in its one standard spelling."""
    return str(ipaddress.ip_network(value, strict=False))


CidrNetwork = Annotated[str, AfterValidator(_network)]
"""An IPv4 or IPv6 network such as ``203.0.113.0/24``; a single address means that address."""


class WindowSettings(Model):
    """Which events a run keeps: whole local days around today."""

    days_back: NonNegativeInt = 14
    days_ahead: NonNegativeInt = 120


class MeetingSettings(Model):
    """How far ahead the meetings of a source with ``meeting_horizon`` are kept.

    Conferences and long workshops are announced months before they take
    place; a source that sets ``meeting_horizon`` keeps them from the start
    of the window to ``days_ahead`` local days after today (never less than
    ``window.days_ahead``). Its talks keep the usual window.
    """

    days_ahead: NonNegativeInt = 365


class SiteSettings(Model):
    """Where the static site is published."""

    base_url: HttpUrlStr  # must end with "/"; feed URLs are base_url + "feeds/<id>.ics"

    @model_validator(mode="after")
    def _check_trailing_slash(self) -> Self:
        if not self.base_url.endswith("/"):
            raise ValueError("site.base_url must end with '/'")
        return self


class HttpSettings(Model):
    """Politeness rules of the HTTP client.

    The effective interval between two requests to one host is
    ``max(min_interval_seconds, Crawl-delay of that host's robots.txt)``.

    Two limits bound how long fetching can take, whatever a site does.
    ``max_exchange_seconds`` is the time one request and its answer may
    take from the connection to the last byte of the body (``timeout_seconds``
    only limits each single read, which a slow drip of bytes never
    exceeds). ``max_get_seconds`` is the time one ``get`` may take in all:
    robots.txt, redirects, retries and every pause between them.

    ``forbidden_networks`` lists networks whose addresses are never
    contacted although they are globally routable: nothing is fetched from
    a URL, a redirect or a name that leads into one of them. Private,
    loopback and link-local addresses are refused in any case and need no
    entry. Add the public address ranges of your own network when internal
    services live in them.
    """

    user_agent: NonEmptyStr
    min_interval_seconds: NonNegativeFloat = 1.0
    timeout_seconds: PositiveFloat = 20.0  # each connect, read and write
    max_exchange_seconds: PositiveFloat = 60.0  # one request and its whole answer
    max_get_seconds: PositiveFloat = 600.0  # one get(): all requests and all waits
    retries: NonNegativeInt = 3  # retries after the first attempt
    max_requests_per_host: PositiveInt = 20  # per run, retries included
    backoff_base_seconds: NonNegativeFloat = 1.0  # retry n waits base * 2**(n-1)
    backoff_max_seconds: NonNegativeFloat = 60.0
    max_crawl_delay_seconds: NonNegativeFloat = 120.0  # refuse hosts that demand more
    max_redirects: NonNegativeInt = 5  # per request, and per robots.txt
    max_body_bytes: PositiveInt = 5_000_000  # larger responses are given up, not read
    robots_cache_seconds: NonNegativeFloat = 86400.0  # reuse a cached robots.txt this long
    forbidden_networks: list[CidrNetwork] = list(DEFAULT_FORBIDDEN_NETWORKS)

    @property
    def robots_token(self) -> str:
        """Product token matched against robots.txt groups, e.g. ``seminars-bot``."""
        words = self.user_agent.split("/", 1)[0].split()
        return words[0].lower() if words else "*"


class EventSettings(Model):
    """Event handling knobs of normalization, filtering and persistence.

    ``include_defenses`` is applied by the event filter and nowhere else:
    when it is false, events of type ``defense`` are excluded with reason
    ``defense`` and listed in the run report.
    """

    default_duration_minutes: PositiveInt = 60  # used when a timed event has no end
    delete_after_missing_runs: PositiveInt = 3  # healthy runs a future event may be absent
    abstract_max_chars: PositiveInt = 4000
    include_defenses: bool = True  # keep PhD defenses (event_type "defense")


class DedupSettings(Model):
    """Thresholds of duplicate detection."""

    max_start_difference_minutes: NonNegativeInt = 15
    title_similarity: Annotated[int, Field(ge=0, le=100)] = 85  # rapidfuzz token_set_ratio


class HealthSettings(Model):
    """After how many consecutive bad runs an issue is opened."""

    source_failure_threshold: PositiveInt = 3
    source_empty_threshold: PositiveInt = 3
    llm_failure_threshold: PositiveInt = 2


class FeedSettings(Model):
    """ICS feed header values."""

    refresh_interval_hours: PositiveInt = 6


class Settings(Model):
    """Content of config/settings.yaml.

    ``timezone``, ``uid_domain``, ``site.base_url`` and ``http.user_agent``
    have no default on purpose: they identify the deployment. ``uid_domain``
    is the right-hand side of every ICS UID and must never change once the
    feeds are public.
    """

    timezone: TimezoneName
    uid_domain: Hostname
    site: SiteSettings
    http: HttpSettings
    window: WindowSettings = WindowSettings()
    meetings: MeetingSettings = MeetingSettings()
    events: EventSettings = EventSettings()
    dedup: DedupSettings = DedupSettings()
    health: HealthSettings = HealthSettings()
    feeds: FeedSettings = FeedSettings()

    def window_at(self, now: datetime) -> TimeWindow:
        """The time window of a run that happens at ``now`` (timezone-aware)."""
        return TimeWindow.around(
            now,
            days_back=self.window.days_back,
            days_ahead=self.window.days_ahead,
            timezone=self.timezone,
        )

    def meeting_window_at(self, now: datetime) -> TimeWindow:
        """The window of the meetings of a ``meeting_horizon`` source in a run at ``now``.

        It starts where ``window_at`` starts and ends ``meetings.days_ahead``
        local days after today, or where ``window_at`` ends if that is later.
        """
        return TimeWindow.around(
            now,
            days_back=self.window.days_back,
            days_ahead=max(self.window.days_ahead, self.meetings.days_ahead),
            timezone=self.timezone,
        )

    def fetch_window_at(self, now: datetime, source: SourceConfig) -> TimeWindow:
        """The window ``source`` is fetched with: the meeting window when it has a horizon."""
        return self.meeting_window_at(now) if source.meeting_horizon else self.window_at(now)
