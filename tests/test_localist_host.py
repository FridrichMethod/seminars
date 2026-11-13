"""The host of a campus calendar event: its first department, else the calendar's own host.

A record of events.stanford.edu names the departments that post the event. The
first one is the host of the event (the organizer on the site, the Host line of
the feeds, the second line of a digest row), so the events of the calendar are
not lumped under one organizer. A record without a named department keeps the
``host_org`` of the source.
"""

from __future__ import annotations

from typing import Any

import pytest

from test_generic_localist import parse_records, record, stanford_source
from test_pipeline_normalize import one


def hosts(*records: dict[str, Any]) -> list[str]:
    source = stanford_source()
    return [one(draft, source).event.host_org for draft in parse_records(list(records), source)]


def test_the_source_host_is_the_university() -> None:
    assert stanford_source().host_org == "Stanford University"


@pytest.mark.parametrize(
    ("departments", "host"),
    [
        ([{"id": 1, "name": "Department of Surgery"}], "Department of Surgery"),
        (
            [
                {"id": 2, "name": "Department of Neurobiology"},
                {"id": 3, "name": "Wu Tsai Neurosciences Institute"},
            ],
            "Department of Neurobiology",
        ),
        ([], "Stanford University"),
        (None, "Stanford University"),
    ],
)
def test_the_event_is_hosted_by_its_first_department(departments: Any, host: str) -> None:
    assert hosts(record(1, departments=departments)) == [host]


def test_a_group_is_never_the_host() -> None:
    item = record(1, departments=[], groups=[{"id": 4, "name": "Stanford Chapter of AAUP"}])
    assert hosts(item) == ["Stanford University"]


def test_entities_in_a_department_name_are_decoded() -> None:
    item = record(1, departments=[{"id": 5, "name": "Department of Art &amp; Art History"}])
    assert hosts(item) == ["Department of Art & Art History"]
