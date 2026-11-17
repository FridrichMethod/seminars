"""Schema of one data source entry in config/sources.d/*.yaml."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import BeforeValidator, ConfigDict, Field, StringConstraints, model_validator

from seminars.models.base import (
    EventType,
    ExtractionMethod,
    FieldList,
    HttpUrlStr,
    Institution,
    Model,
    NonEmptyStr,
    SourceId,
    TimezoneName,
)

SOURCE_GROUP_PREFIXES: Final[dict[str, str]] = {
    "localist": "localist-",
    "stanford-sites": "sites-",
    "stanford-custom": "custom-",
    "bayarea": "bayarea-",
    "researchseminars": "rsorg-",
}
"""Mandatory id prefix for each of the five built-in source groups."""

LLM_PAGE_ADAPTER: Final = "llm_page"
"""Adapter name of sources that are extracted by the LLM page extractor only.

It is a registered adapter like any other: it fetches the source's URLs and
parses nothing, and the pipeline's LLM fallback does the rest.
"""

DEFAULT_TIMEZONE: Final = "America/Los_Angeles"

GroupName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
AdapterName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
HostName = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$"
    ),
]
"""A host name in lower case, such as ``indico.slac.stanford.edu``."""


def _as_list(value: Any) -> Any:
    return [value] if isinstance(value, str) else value


class ResearchInfo(Model):
    """Findings of the source research, kept next to the source they describe.

    The listed keys are typed; additional keys are allowed so that a research
    node can record facts nobody anticipated (for example ``template: A``).
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    verified: date | None = None  # day the URLs were last checked by hand
    sample_events: Annotated[int, Field(ge=0)] | None = None  # events seen in the sample
    has_rss: bool | None = None
    has_ics: bool | None = None
    has_jsonld: bool | None = None
    has_jsonapi: bool | None = None
    extraction: ExtractionMethod | None = None  # chosen method: structured data first
    crawl_delay: Annotated[float, Field(ge=0)] | None = None  # robots.txt Crawl-delay, seconds
    notes: Annotated[list[str], BeforeValidator(_as_list)] = []


class SourceConfig(Model):
    """One entry of the ``sources`` list in a config/sources.d/*.yaml file.

    Common keys are strict (unknown keys are an error); everything that only
    one adapter understands goes into ``params``::

        sources:
          - id: sites-chemistry            # unique; prefix fixed by the group
            name: Stanford Chemistry
            group: stanford-sites
            adapter: drupal_jsonapi          # name in the adapter registry
            url: https://chemistry.stanford.edu/jsonapi/node/hs_event
            institution: stanford
            host_org: Stanford Chemistry
            default_fields: [chem]
            params: {page_limit: 50}
            research: {verified: 2026-11-10, sample_events: 12, has_jsonapi: true}

    * ``id``: lower-case words joined by hyphens. It names the fixture
      directory tests/fixtures/<id>/ and prefixes ``Event.source_ids``. The
      five built-in groups require the prefix in SOURCE_GROUP_PREFIXES.
    * ``url`` / ``urls``: at least one absolute http(s) URL; ``all_urls``
      lists them in order. URLs are used exactly as written.
    * ``adapter``: name of the adapter, which is the name of its module: one
      of the generic ones (``localist``, ``drupal_jsonapi``, ``ics``, ``rss``,
      ``jsonld``, ``html_list``, ``llm_page``) or a module in
      seminars.sources.custom. It is checked against the registry at run
      time, not at load time, so a source can be described before its adapter
      exists.
    * ``homepage``: human-facing page for the status page (default: first URL).
    * ``default_fields``: fields of every event of this source (may be empty
      for campus-wide calendars, which are then classified by rules).
    * ``default_event_type`` / ``series`` / ``host_org``: used when a draft
      does not say otherwise.
    * ``timezone``: how naive times of this source are interpreted.
    * ``aggregator``: true for calendars that re-list other hosts' events
      (Localist, researchseminars.org); merging prefers detail pages of
      non-aggregator sources.
    * ``llm_fallback``: allow the LLM page extractor when rule-based parsing
      yields nothing or fails. Always on for ``adapter: llm_page``, whose
      adapter never yields anything itself.
    * ``ignore_robots``: hosts of this source whose robots.txt ``Disallow``
      rules are not followed when this source is fetched (see
      ``PoliteClient.ignoring_robots_on``). Each one is a host of ``url`` /
      ``urls`` / ``homepage`` or a name of ``params.extra_hosts``. The
      Crawl-delay, the pause between requests and the request budgets still
      apply, and other sources on the same host still follow robots.txt.
      A comment next to the entry says why.
    * ``params``: free-form mapping read only by the adapter, with one key
      that the shared helpers read too: ``extra_hosts``, a list of host
      names the adapter may fetch besides the hosts of ``url`` / ``urls`` /
      ``homepage`` (see ``seminars.sources.base.source_hosts``).
    * ``research``: see ResearchInfo.

    A disabled source (``enabled: false``) states why on the status page:
    ``site.status.disabled_reasons.<id>`` in both locale files.
    """

    id: SourceId
    name: NonEmptyStr
    group: GroupName
    adapter: AdapterName
    enabled: bool = True
    url: HttpUrlStr | None = None
    urls: list[HttpUrlStr] = []
    homepage: HttpUrlStr | None = None
    institution: Institution
    host_org: NonEmptyStr
    default_fields: FieldList = []
    default_event_type: EventType = "seminar"
    series: str | None = None
    timezone: TimezoneName = DEFAULT_TIMEZONE
    aggregator: bool = False
    llm_fallback: bool = False
    ignore_robots: list[HostName] = []
    params: dict[str, Any] = {}
    research: ResearchInfo | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.url is None and not self.urls:
            raise ValueError("a source needs 'url' or a non-empty 'urls' list")
        prefix = SOURCE_GROUP_PREFIXES.get(self.group)
        if prefix is not None and not self.id.startswith(prefix):
            raise ValueError(f"ids in group {self.group!r} must start with {prefix!r}")
        stray = sorted(set(self.ignore_robots) - self._own_hosts())
        if stray:
            names = ", ".join(stray)
            raise ValueError(f"ignore_robots names hosts this source is not fetched from: {names}")
        return self

    def _own_hosts(self) -> set[str]:
        """The hosts of the URLs and of ``params.extra_hosts``, as written (lower case)."""
        urls = [*self.all_urls, *([self.homepage] if self.homepage else [])]
        hosts = {urlsplit(url).hostname or "" for url in urls}
        extra = self.params.get("extra_hosts", [])
        for name in extra if isinstance(extra, list) else []:
            if isinstance(name, str):
                hosts.add(name.lower())
        return hosts

    @property
    def all_urls(self) -> list[str]:
        """``url`` followed by ``urls``, without duplicates, in order."""
        candidates = ([self.url] if self.url is not None else []) + self.urls
        return list(dict.fromkeys(candidates))

    @property
    def wants_llm_fallback(self) -> bool:
        """True when the LLM page extractor may be used for this source."""
        return self.llm_fallback or self.adapter == LLM_PAGE_ADAPTER


ProbeFormat = Literal["localist", "drupal_jsonapi", "rss", "ics", "jsonld", "html", "unknown"]


class ProbeResult(Model):
    """Answer of the cheap format probe behind ``seminars add-source <url>``."""

    url: str
    format: ProbeFormat
    content_type: str | None = None
    suggested_yaml: str  # a sources.d entry ready to paste, ASCII only
    notes: list[str] = []
