"""The workshops of the Simons Institute (html_list) over their recorded list pages.

The page lists upcoming and past workshops in two tabs of one HTML, ten cards
per tab and page. A card gives a title, a link and the dates: a range of all
days ("Monday, Aug. 30 - Friday, Sept. 3, 2027", the year only at the end) or
one day with times. The list runs about a year ahead, so the source keeps its
meetings up to the meeting window; the end-to-end test runs the pipeline over
this source's fixtures alone, at the time of their recording, and checks what
it stores.
"""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from factories import make_window
from fakes import FakeClock, FakeHttpClient
from seminars import cli
from seminars.config import PROVIDER_CREDENTIAL_ENV, load_settings, load_sources
from seminars.http import offline_client
from seminars.models import EventDraft, read_events
from seminars.sources.base import ParseError, get_adapter

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCES = {source.id: source for source in load_sources(REPO_ROOT / "config" / "sources.d")}
SOURCE_ID = "bayarea-simons-workshops"
LIST = "https://simons.berkeley.edu/programs-events/workshop-symposia"
PAGES = [LIST, f"{LIST}?page=1", f"{LIST}?page=2"]
NOW = datetime(2026, 11, 18, 9, 58, 33, tzinfo=UTC)
"""The time of the latest recording of this source."""
FIXTURES = REPO_ROOT / "tests" / "fixtures" / SOURCE_ID
LAST_PAGE = FIXTURES / "programs-events-workshop-symposia-e291e058.html"
CALVIN_LAB = "Calvin Lab, UC Berkeley"
DIFFUSION = {
    "Diffusion Generative Modeling Boot Camp": (date(2027, 8, 30), date(2027, 9, 3)),
    "Engineering of Diffusion Models: Scaling, Modalities, Safety": (
        date(2027, 9, 20),
        date(2027, 9, 24),
    ),
    "Science of Diffusion Models: Speciation, Creativity, New Testbeds": (
        date(2027, 10, 18),
        date(2027, 10, 22),
    ),
    "Beyond Generative Modeling: Steering Models and Learning to Sample": (
        date(2027, 11, 15),
        date(2027, 11, 19),
    ),
}
"""The Fall 2027 program, first and last day (inclusive), as the cards print them."""


def run() -> tuple[list[str], list[EventDraft]]:
    source = SOURCES[SOURCE_ID]
    http = offline_client(SOURCE_ID)
    adapter = get_adapter(source.adapter)
    window = make_window(NOW, days_ahead=365)
    drafts = adapter.parse(adapter.fetch(http, source, window=window), source)
    return list(http.requests), drafts


def by_title(drafts: list[EventDraft]) -> dict[str, EventDraft]:
    found = {draft.title: draft for draft in drafts if draft.title is not None}
    assert len(found) == len(drafts)
    return found


def day_of(value: object) -> date:
    """``value`` as a day: the start or end of an all-day draft."""
    assert isinstance(value, date)
    assert not isinstance(value, datetime)
    return value


def test_the_source_entry() -> None:
    source = SOURCES[SOURCE_ID]
    assert source.enabled
    assert (source.group, source.adapter, source.institution) == ("bayarea", "html_list", "other")
    assert source.all_urls == PAGES
    assert source.host_org == "Simons Institute for the Theory of Computing"
    assert source.default_fields == ["cs"]
    assert source.default_event_type == "workshop"
    assert source.meeting_horizon
    assert source.ignore_robots == []


def test_three_list_pages_and_the_upcoming_cards_only() -> None:
    requests, drafts = run()
    assert requests == PAGES
    # 10 upcoming cards on the first page, 5 on the second, none on the third; two of
    # the 15 are left out (see below). The past tab of each page has 10 cards.
    assert len(drafts) == 13
    titles = set(by_title(drafts))
    assert "Beyond Graph Laplacians" not in titles  # the first past card of page 0
    assert all(draft.source_id == SOURCE_ID and draft.extraction == "html" for draft in drafts)
    workshops = "https://simons.berkeley.edu/workshops/"
    assert all((draft.url or "").startswith(workshops) for draft in drafts)
    assert len({draft.url for draft in drafts}) == len(drafts)


def test_the_fall_2027_diffusion_program_lasts_its_days_in_2027() -> None:
    _, drafts = run()
    found = by_title(drafts)
    for title, (first, last) in DIFFUSION.items():
        draft = found[title]
        assert (draft.start, draft.end) == (first, last), title
        assert draft.is_all_day
        assert draft.location == CALVIN_LAB
    url = "https://simons.berkeley.edu/workshops/diffusion-generative-modeling-boot-camp"
    assert found["Diffusion Generative Modeling Boot Camp"].url == url


def test_every_range_ends_after_it_starts_and_within_two_weeks() -> None:
    _, drafts = run()
    ranges = [draft for draft in drafts if draft.is_all_day]
    assert len(ranges) == 12
    for draft in ranges:
        assert 0 < (day_of(draft.end) - day_of(draft.start)).days <= 14, draft.title
    found = by_title(drafts)
    expansion = found["Applications of Pseudorandomness and High-Dimensional Expansion"]
    assert (expansion.start, expansion.end) == (date(2026, 11, 30), date(2026, 12, 4))
    assert (found["Random CSPs"].start, found["Random CSPs"].end) == (
        date(2027, 3, 8),
        date(2027, 3, 12),
    )


def test_a_card_of_one_day_has_its_times_and_the_default_place() -> None:
    _, drafts = run()
    day = by_title(drafts)["5th Annual Quantum Industry Day 2026"]
    # "Wednesday, Nov. 18, 2026 | 9 a.m. - 5 p.m.": naive wall time of the source.
    assert (day.start, day.end) == (datetime(2026, 11, 18, 9), datetime(2026, 11, 18, 17))
    assert day.location == CALVIN_LAB
    assert day.speakers == []
    assert day.abstract is None


def test_reunions_and_the_partner_industry_day_are_left_out() -> None:
    _, drafts = run()
    titles = set(by_title(drafts))
    assert "Complexity and Linear Algebra Reunion" not in titles  # by invitation only
    assert "Twelfth Annual Industry Day" not in titles  # partners and program members
    assert "5th Annual Quantum Industry Day 2026" in titles  # open registration
    assert not [title for title in titles if "reunion" in title.lower()]


def recorded_pages(last_page: str) -> FakeHttpClient:
    http = FakeHttpClient(clock=FakeClock(NOW))
    recorded = offline_client(SOURCE_ID)
    for url in PAGES[:2]:
        http.add(url, recorded.get(url).text)
    http.add(PAGES[2], last_page)
    return http


def test_a_page_past_the_end_needs_the_empty_upcoming_view() -> None:
    source = SOURCES[SOURCE_ID]
    adapter = get_adapter(source.adapter)
    window = make_window(NOW, days_ahead=365)
    text = LAST_PAGE.read_text(encoding="utf-8")
    marker = "view-pattern--block_upcoming_all_workshops"
    assert text.count(marker) == 1
    drafts = adapter.parse(adapter.fetch(recorded_pages(text), source, window=window), source)
    assert len(drafts) == 13
    redesigned = text.replace(marker, "view-pattern--block_something_else")
    with pytest.raises(ParseError, match="empty_marker"):
        adapter.parse(adapter.fetch(recorded_pages(redesigned), source, window=window), source)


def test_an_offline_run_keeps_every_workshop_as_a_meeting_of_computer_science(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    shutil.copytree(REPO_ROOT / "config", root / "config")
    shutil.copytree(REPO_ROOT / "locales", root / "locales")
    # Only this source's fixtures: an offline run happens at the latest recording of
    # the fixtures it finds, so a later recording of another source cannot move the
    # windows below.
    shutil.copytree(FIXTURES, root / "tests" / "fixtures" / SOURCE_ID)
    with pytest.MonkeyPatch.context() as patch:
        for name in (*filter(None, PROVIDER_CREDENTIAL_ENV.values()), "GITHUB_TOKEN"):
            patch.delenv(name, raising=False)
        for name in ("LLM_PROVIDERS", "GITHUB_ACTIONS", "GITHUB_REPOSITORY"):
            patch.delenv(name, raising=False)
        patch.setenv("SEMINARS_HTTP_CACHE_DIR", str(tmp_path / "http-cache"))
        patch.chdir(root)
        args = ["run", "--offline", "--no-llm", "--source", SOURCE_ID]
        result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    events = {event.title: event for event in read_events(root / "data" / "events.json")}
    assert len(events) == 13
    settings = load_settings(REPO_ROOT / "config" / "settings.yaml")
    run_time = datetime.fromisoformat(result.output.splitlines()[0].rpartition(" ")[2])
    assert run_time == NOW
    talks, meetings = settings.window_at(run_time), settings.meeting_window_at(run_time)
    # The talk window ends 120 days after the run; the meetings after it stay.
    beyond = {title for title, event in events.items() if event.start >= talks.end}
    assert beyond == {
        *DIFFUSION,
        "AI Risk, Evaluation and Governance",
        "The Quantum PCP Conjectures: Local Hamiltonians and Nonlocal Games",
    }
    assert all(meetings.contains(event.start) for event in events.values())
    for title, event in events.items():
        assert event.event_type == "workshop", title
        assert event.is_meeting, title
        assert "cs" in event.fields, title
        assert event.location == CALVIN_LAB
    for title, (first, last) in DIFFUSION.items():
        event = events[title]
        assert event.all_day
        assert "ai-ml" in event.fields
        assert event.start.date() == first
        assert event.end is not None
        assert (event.end.date() - last).days == 1  # the end of an all-day event is exclusive
