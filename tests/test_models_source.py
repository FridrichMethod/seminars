"""Tests for SourceConfig and ResearchInfo (one entry of config/sources.d/*.yaml)."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from factories import make_source
from seminars.models import SOURCE_GROUP_PREFIXES, ProbeResult, ResearchInfo, SourceConfig

COMMON_KEYS = {
    "id",
    "name",
    "group",
    "adapter",
    "enabled",
    "url",
    "urls",
    "institution",
    "host_org",
    "default_fields",
    "default_event_type",
    "series",
    "timezone",
    "params",
    "research",
}


def test_schema_has_all_common_keys() -> None:
    assert set(SourceConfig.model_fields) >= COMMON_KEYS
    assert set(SourceConfig.model_fields) - COMMON_KEYS == {
        "homepage",
        "aggregator",
        "llm_fallback",
        "meeting_horizon",
        "ignore_robots",
    }


def test_defaults() -> None:
    source = make_source()
    assert source.enabled is True
    assert source.default_event_type == "seminar"
    assert source.timezone == "America/Los_Angeles"
    assert source.series is None
    assert source.params == {}
    assert source.research is None
    assert source.aggregator is False
    assert source.wants_llm_fallback is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "Sites-Chemistry"),
        ("id", "sites_chemistry"),
        ("id", "sites--chemistry"),
        ("id", ""),
        ("name", ""),
        ("group", "Stanford Sites"),
        ("adapter", "drupal-jsonapi"),
        ("adapter", ""),
        ("institution", "mit"),
        ("host_org", ""),
        ("default_fields", ["history"]),
        ("default_event_type", "class"),
        ("timezone", "PST"),
        ("url", "ftp://example.edu/feed"),
        ("url", "/events/all/rss"),
        ("url", "https://example.edu/a b"),
        ("urls", ["not a url"]),
        ("enabled", "sometimes"),
    ],
)
def test_common_keys_are_strict(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_source(**{field: value})


def test_unknown_common_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="selector"):
        make_source(selector="div.event")


def test_adapter_specific_settings_go_into_params() -> None:
    params = {
        "selectors": {"item": "div.event", "title": "h3 a"},
        "type_ids": [37952570025304, 37952570034524],
        "department_fields": {"Chemistry": ["chem"]},
        "page_limit": 50,
    }
    assert make_source(adapter="html_list", params=params).params == params


def test_adapter_name_is_not_checked_against_the_registry() -> None:
    assert make_source(adapter="adapter_written_later").adapter == "adapter_written_later"


def test_a_url_is_required() -> None:
    with pytest.raises(ValidationError, match="needs 'url'"):
        make_source(url=None)
    assert make_source(url=None, urls=["https://example.edu/a"]).all_urls == [
        "https://example.edu/a"
    ]


def test_ignore_robots_is_empty_by_default() -> None:
    assert make_source().ignore_robots == []


@pytest.mark.parametrize(
    ("overrides", "hosts"),
    [
        ({}, ["bioengineering.stanford.edu"]),
        ({"homepage": "https://www.example.edu/talks"}, ["www.example.edu"]),
        ({"urls": ["https://feeds.example.edu/a.ics"]}, ["feeds.example.edu"]),
        ({"params": {"extra_hosts": ["Calendar.Example.EDU"]}}, ["calendar.example.edu"]),
    ],
)
def test_ignore_robots_takes_hosts_the_source_is_fetched_from(
    overrides: dict[str, object], hosts: list[str]
) -> None:
    assert make_source(ignore_robots=hosts, **overrides).ignore_robots == hosts


def test_ignore_robots_refuses_a_host_of_another_site() -> None:
    with pytest.raises(ValidationError, match=r"not fetched from: calendar\.google\.com"):
        make_source(ignore_robots=["calendar.google.com"])


@pytest.mark.parametrize(
    "host", ["Bioengineering.stanford.edu", "https://bioengineering.stanford.edu", "localhost", ""]
)
def test_ignore_robots_takes_host_names_in_lower_case_only(host: str) -> None:
    with pytest.raises(ValidationError):
        make_source(ignore_robots=[host])


def test_all_urls_keeps_order_and_drops_duplicates() -> None:
    source = make_source(
        url="https://example.edu/a",
        urls=["https://example.edu/b", "https://example.edu/a", "https://example.edu/c"],
    )
    assert source.all_urls == [
        "https://example.edu/a",
        "https://example.edu/b",
        "https://example.edu/c",
    ]


def test_urls_are_kept_exactly_as_written() -> None:
    url = "https://events.stanford.edu/api/2/events?type[]=37952570025304&pp=100"
    assert make_source(url=url).url == url


@pytest.mark.parametrize(("group", "prefix"), sorted(SOURCE_GROUP_PREFIXES.items()))
def test_built_in_groups_require_their_id_prefix(group: str, prefix: str) -> None:
    assert make_source(group=group, id=f"{prefix}example").id == f"{prefix}example"
    with pytest.raises(ValidationError, match="must start with"):
        make_source(group=group, id="wrong-example")


def test_group_prefixes_cover_the_five_research_groups_and_the_registry() -> None:
    assert SOURCE_GROUP_PREFIXES == {
        "localist": "localist-",
        "stanford-sites": "sites-",
        "stanford-custom": "custom-",
        "bayarea": "bayarea-",
        "researchseminars": "rsorg-",
        "conferences": "conf-",
    }


def test_other_groups_may_use_any_id() -> None:
    assert make_source(group="my-lab", id="anything-goes").group == "my-lab"


def test_llm_page_adapter_implies_llm_fallback() -> None:
    assert make_source(adapter="llm_page").wants_llm_fallback
    assert make_source(llm_fallback=True).wants_llm_fallback


def test_research_block() -> None:
    source = make_source(
        research={
            "verified": "2026-11-10",
            "sample_events": 12,
            "has_rss": True,
            "has_ics": False,
            "has_jsonld": False,
            "has_jsonapi": True,
            "extraction": "api",
            "crawl_delay": 30,
            "notes": "single note",
            "template": "A",
            "list_path": "/events/upcoming-events",
        }
    )
    research = source.research
    assert research is not None
    assert research.verified == date(2026, 11, 10)
    assert research.crawl_delay == 30.0
    assert research.notes == ["single note"]
    assert research.model_extra == {"template": "A", "list_path": "/events/upcoming-events"}


def test_research_typed_keys_are_validated() -> None:
    with pytest.raises(ValidationError):
        ResearchInfo.model_validate({"sample_events": -1})
    with pytest.raises(ValidationError):
        ResearchInfo.model_validate({"extraction": "magic"})
    with pytest.raises(ValidationError):
        ResearchInfo.model_validate({"verified": "yesterday"})
    assert ResearchInfo().notes == []


def test_evolve_keeps_research_extras() -> None:
    research = ResearchInfo.model_validate({"template": "B", "notes": ["a", "b"]})
    changed = research.evolve(sample_events=4)
    assert changed.sample_events == 4
    assert changed.model_extra == {"template": "B"}


def test_probe_result() -> None:
    result = ProbeResult(
        url="https://example.edu/events", format="rss", suggested_yaml="sources:\n  - id: x\n"
    )
    assert result.notes == []
    with pytest.raises(ValidationError):
        ProbeResult(url="https://example.edu", format="pdf", suggested_yaml="")  # type: ignore[arg-type]
