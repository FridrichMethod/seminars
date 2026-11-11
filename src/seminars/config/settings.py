"""Schema of config/settings.yaml."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from seminars.models import Model, TimeWindow
from seminars.models.base import HttpUrlStr, NonEmptyStr, TimezoneName

PositiveInt = Annotated[int, Field(ge=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
Hostname = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")]


class WindowSettings(Model):
    """Which events a run keeps: whole local days around today."""

    days_back: NonNegativeInt = 14
    days_ahead: NonNegativeInt = 120


class SiteSettings(Model):
    """Where the static site is published."""

    base_url: HttpUrlStr  # must end with "/"; feed URLs are base_url + "feeds/<id>.ics"

    @model_validator(mode="after")
    def _check_trailing_slash(self) -> Self:
        if not self.base_url.endswith("/"):
            raise ValueError("site.base_url must end with '/'")
        return self


class HttpSettings(Model):
    """Politeness rules of the HTTP client (SPEC section 2).

    The effective interval between two requests to one host is
    ``max(min_interval_seconds, Crawl-delay of that host's robots.txt)``.
    """

    user_agent: NonEmptyStr
    min_interval_seconds: NonNegativeFloat = 1.0
    timeout_seconds: PositiveFloat = 20.0
    retries: NonNegativeInt = 3  # retries after the first attempt
    max_requests_per_host: PositiveInt = 20  # per run, retries included
    backoff_base_seconds: NonNegativeFloat = 1.0  # retry n waits base * 2**(n-1)
    backoff_max_seconds: NonNegativeFloat = 60.0
    max_crawl_delay_seconds: NonNegativeFloat = 120.0  # refuse hosts that demand more
    max_redirects: NonNegativeInt = 5
    robots_cache_seconds: NonNegativeFloat = 86400.0  # reuse a cached robots.txt this long

    @property
    def robots_token(self) -> str:
        """Product token matched against robots.txt groups, e.g. ``seminars-bot``."""
        words = self.user_agent.split("/", 1)[0].split()
        return words[0].lower() if words else "*"


class EventSettings(Model):
    """Event handling knobs of normalization and persistence."""

    default_duration_minutes: PositiveInt = 60  # used when a timed event has no end
    delete_after_missing_runs: PositiveInt = 3  # SPEC section 5.8
    abstract_max_chars: PositiveInt = 4000
    include_defenses: bool = True  # keep PhD defenses (event_type "defense")


class DedupSettings(Model):
    """Thresholds of duplicate detection (SPEC section 5.4)."""

    max_start_difference_minutes: NonNegativeInt = 15
    title_similarity: Annotated[int, Field(ge=0, le=100)] = 85  # rapidfuzz token_set_ratio


class HealthSettings(Model):
    """After how many consecutive bad runs an issue is opened (SPEC sections 6.4, 7.4)."""

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
