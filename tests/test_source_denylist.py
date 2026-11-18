"""No source and no registered meeting points at a site that lists conferences for a fee.

Aggregators such as waset.org or 10times.com list paid or predatory "conferences" next to
real ones, and their dates are copied from elsewhere at best. No source is fetched from
them, and the registry of meetings (config/sources.d/conferences.yaml) takes its facts
from the organizer's own page: neither the page of a meeting nor the page it was verified
on is one of theirs.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from seminars.config import load_sources
from seminars.sources.custom.registry import RegistryParams
from seminars.sources.generic.common import load_params

ROOT = Path(__file__).resolve().parents[1]

DENIED_DOMAINS = (
    "waset.org",
    "conferenceindex.org",
    "10times.com",
    "allconferencealert.com",
    "wikicfp.com",
    "clocate.com",
)
"""Domains that are denied with every host below them."""

DENIED_NAMES = ("conferencealerts",)
"""Names denied under any top-level domain: conferencealerts.com, conferencealerts.co.in."""


def host_of(address: str) -> str:
    """The host of a URL, or ``address`` itself when it is a host name."""
    host = urlsplit(address).hostname if "://" in address else address
    return (host or "").lower().rstrip(".")


def denied(address: str) -> bool:
    host = host_of(address)
    if any(host == domain or host.endswith(f".{domain}") for domain in DENIED_DOMAINS):
        return True
    return any(name in host.split(".")[:-1] for name in DENIED_NAMES)


def addresses() -> Iterator[tuple[str, str]]:
    """``(where, address)`` for every address a source or a registered meeting names."""
    for source in load_sources(ROOT / "config" / "sources.d"):
        for url in [*source.all_urls, *([source.homepage] if source.homepage else [])]:
            yield source.id, url
        extra = source.params.get("extra_hosts", [])
        for host in extra if isinstance(extra, list) else []:
            yield f"{source.id} extra_hosts", str(host)
        if source.adapter == "registry":
            for meeting in load_params(RegistryParams, source).meetings:
                yield f"{source.id} {meeting.id}", meeting.url
                yield f"{source.id} {meeting.id} verified", meeting.verified.url


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("https://waset.org/conferences", True),
        ("https://www.waset.org/", True),
        ("https://conferenceindex.org/event/x", True),
        ("https://10times.com/e/x", True),
        ("https://allconferencealert.com/", True),
        ("https://www.conferencealerts.com/topic", True),
        ("https://conferencealerts.co.in/", True),
        ("http://wikicfp.com/cfp/", True),
        ("www.clocate.com", True),
        ("https://notwaset.org/", False),
        ("https://waset.org.example.edu/", False),
        ("https://iclr.cc/Conferences/2027", False),
        ("events.stanford.edu", False),
    ],
)
def test_the_denylist_matches_the_hosts_of_those_sites(address: str, expected: bool) -> None:
    assert denied(address) is expected


def test_the_configuration_names_addresses_of_sources_and_registered_meetings() -> None:
    found = list(addresses())
    assert any(where.startswith("conf-registry ") for where, _ in found)
    assert any(where == "localist-stanford" for where, _ in found)


def test_no_source_or_registered_meeting_is_on_a_denied_site() -> None:
    assert [(where, address) for where, address in addresses() if denied(address)] == []
