"""Tests that error texts and log lines never publish a meeting credential.

The error of a failed source goes into data/run_report.json,
data/health.json, public/status.json, the source-broken issue, the weekly
digest and the job log. A URL in it can come from a scraped page, so it is
scrubbed like any free text of an event.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta
from typing import Any

import httpx
import pytest

from factories import REFERENCE_NOW, make_source
from fakes import (
    FakeAdapter,
    FakeClock,
    FakeEventFilter,
    FakeFieldTagger,
    FakeHttpClient,
    FakeIssueClient,
    FakePicksScorer,
    StubResolver,
)
from fakes_outputs import FakeEmitters
from seminars.config import AppConfig, HttpSettings
from seminars.contracts import SiteDataWriter, load_implementation
from seminars.http import HttpError, HttpRequestError, HttpStatusError, PoliteClient
from seminars.models import RunReport
from seminars.outputs.health import update_health
from seminars.pipeline.collect import describe_error
from seminars.pipeline.run import RunOptions, run_pipeline
from seminars.pipeline.secret_scan import build_secret_scanner
from seminars.pipeline.services import Services
from seminars.sources.base import AdapterRegistry, ForeignHostError
from seminars.sources.generic.common import fetch_optional

ZOOM = "https://stanford.zoom.us/j/99887766554?pwd=SECRETpwdAbC123xyz"
TALK = "https://physics.stanford.edu/events/talk?passcode=731946&tk=abcDEF"
CODES = ("SECRETpwdAbC123xyz", "731946", "abcDEF")
URL = "https://a.example.edu/events"


def no_redaction(text: str) -> str:
    return text


def without_codes(text: str) -> bool:
    return not any(code in text for code in CODES)


class TestDescribeError:
    def test_a_foreign_link_with_a_meeting_password_is_scrubbed(self) -> None:
        text = describe_error(ForeignHostError("bayarea-slac-colloquium", ZOOM), no_redaction)
        assert without_codes(text)
        assert text.startswith("ForeignHostError: ")
        assert "stanford.zoom.us/j/99887766554" in text

    def test_a_page_address_with_codes_is_scrubbed(self) -> None:
        text = describe_error(HttpStatusError(TALK, 404), no_redaction)
        assert without_codes(text)
        assert text == "HttpStatusError: HTTP 404: https://physics.stanford.edu/events/talk"

    def test_a_secret_is_still_redacted(self) -> None:
        forged = "gh" + "p_" + "Q" * 36
        text = describe_error(RuntimeError(f"{forged} at {ZOOM}"), build_secret_scanner().redact)
        assert forged not in text
        assert without_codes(text)


def services(emitters: FakeEmitters, issues: FakeIssueClient, **overrides: Any) -> Services:
    site_data: SiteDataWriter = load_implementation("site_data")
    values: dict[str, Any] = {
        "http_for": lambda chosen: FakeHttpClient(),
        "event_filter": FakeEventFilter(),
        "field_tagger": FakeFieldTagger(),
        "picks_scorer": FakePicksScorer(),
        "write_ics_feeds": emitters.write_ics_feeds,
        "write_site_data": site_data,
        "update_health": update_health,
        "secret_scanner": build_secret_scanner(),
        "issues": issues,
    }
    return Services(**{**values, **overrides})


class TestPublishedChannels:
    def test_no_code_reaches_the_report_health_status_or_issue(
        self,
        app_config: AppConfig,
        fake_registry: AdapterRegistry,
        fake_issues: FakeIssueClient,
    ) -> None:
        FakeAdapter(fetch_error=HttpStatusError(TALK, 500)).register(fake_registry, "down")
        FakeAdapter().register(fake_registry, "fake")
        sources = (
            make_source(id="sites-a", adapter="down", url=URL),
            make_source(id="sites-b", adapter="fake", url="https://b.example.edu/events"),
        )
        config = dataclasses.replace(app_config, sources=sources)
        reports: list[RunReport] = []
        for day in range(3):
            reports.append(
                run_pipeline(
                    config,
                    services(FakeEmitters(), fake_issues),
                    now=REFERENCE_NOW + timedelta(days=day),
                    options=RunOptions(),
                    timer=FakeClock(),
                )
            )
        failed = reports[-1].sources[0]
        assert failed.status == "failed"
        assert failed.error is not None
        assert "physics.stanford.edu/events/talk" in failed.error
        paths = config.paths
        for path in (paths.run_report_file, paths.health_file, paths.public_status_file):
            assert without_codes(path.read_text(encoding="utf-8")), path
        (issue,) = fake_issues.issues
        assert issue.title == "Source broken: sites-a"
        assert "physics.stanford.edu/events/talk" in issue.body
        assert without_codes(issue.body)


class TestLogLines:
    def test_a_skipped_page_is_logged_without_its_codes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        source = make_source(
            id="sites-physics", url="https://physics.stanford.edu/events", adapter="fake"
        )
        http = FakeHttpClient()
        http.add(TALK, "gone", status=404)
        with caplog.at_level(logging.WARNING):
            assert fetch_optional(http, source, [TALK], "detail") == []
        (message,) = [record.getMessage() for record in caplog.records]
        assert "sites-physics: detail page skipped" in message
        assert "physics.stanford.edu/events/talk" in message
        assert without_codes(message)

    def test_an_unexpected_failure_is_logged_without_the_query(
        self, http_settings: HttpSettings, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        class Raising(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                if request.url.path == "/robots.txt":
                    return httpx.Response(404)
                raise ValueError("bad value")

        client = PoliteClient(
            http_settings,
            clock=fake_clock,
            sleep=fake_clock.sleep,
            transport=Raising(),
            resolver=StubResolver(),
        )
        with caplog.at_level(logging.WARNING), pytest.raises(HttpRequestError):
            client.get(TALK)
        messages = [record.getMessage() for record in caplog.records]
        assert "unexpected ValueError: https://physics.stanford.edu/events/talk" in messages
        assert all(without_codes(message) for message in messages)

    def test_the_error_itself_still_names_the_address(self) -> None:
        error: HttpError = HttpStatusError(TALK, 404)
        assert error.url == TALK
