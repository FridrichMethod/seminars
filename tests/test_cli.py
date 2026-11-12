"""Tests for the command line: every command, its output and its exit status.

The commands run in a scratch project below ``tmp_path`` with two sources
served from recorded fixtures by a small adapter of this file. Every
implementation of another package is a fake handed out through
``seminars.pipeline.services.load``.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from factories import REFERENCE_NOW, make_document, make_event
from fakes import FakeEventFilter, FakeFieldTagger, FakeHttpClient, FakeIssueClient, FakePicksScorer
from fakes_outputs import FakeEmitters
from seminars import cli
from seminars.config import PROVIDER_CREDENTIAL_ENV
from seminars.contracts import ContractNotImplementedError
from seminars.fixtures import FixtureStore
from seminars.http import HttpClient
from seminars.models import (
    EventDraft,
    LastRun,
    LLMProviderResult,
    LLMUsage,
    RawPayload,
    RunReport,
    SourceConfig,
    SourceRunResult,
    TimeWindow,
    dump_events,
    read_model,
)
from seminars.outputs.health import update_health
from seminars.pipeline import services as wiring
from seminars.pipeline.run import LIVE_OVER_OFFLINE
from seminars.pipeline.secret_scan import build_secret_scanner
from seminars.sources.base import AdapterRegistry, fetch_urls

REPO_ROOT = Path(__file__).resolve().parents[1]
A_URL = "https://a.example.edu/events"
B_URL = "https://b.example.edu/events"
SOURCES_YAML = f"""sources:
  - id: sites-a
    name: Site A
    group: stanford-sites
    adapter: lines
    url: {A_URL}
    institution: stanford
    host_org: Stanford A
    default_fields: [chem]
  - id: sites-b
    name: Site B
    group: stanford-sites
    adapter: lines
    url: {B_URL}
    institution: stanford
    host_org: Stanford B
    default_fields: [math]
  - id: sites-off
    name: Site Off
    group: stanford-sites
    adapter: lines
    url: https://off.example.edu/events
    institution: stanford
    host_org: Stanford Off
    default_fields: [cs]
    enabled: false
"""
A_LINES = (
    "1|Catalysis at interfaces|2026-11-17T16:00:00-08:00\n"
    "2|Enzymes by design|2026-11-18T16:00:00-08:00\n"
)
B_LINES = "7|Knots and braids|2026-11-19T15:00:00-08:00\n"
GITHUB_ENV = {"GITHUB_TOKEN": "x" * 24, "GITHUB_REPOSITORY": "someone/seminars"}


class LineAdapter:
    """Reads ``native id|title|start`` lines from every document of its source."""

    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        return fetch_urls(http, source, window=window)

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        drafts = []
        for document in payload.documents:
            for line in document.text.splitlines():
                native_id, title, start = line.split("|")
                drafts.append(
                    EventDraft(
                        source_id=source.id,
                        extraction="feed",
                        native_id=native_id,
                        url=f"{document.final_url}/{native_id}",
                        title=title,
                        start=start,
                    )
                )
        return drafts


@dataclass
class Fakes:
    """The implementations the commands get, and what they were asked."""

    emitters: FakeEmitters = field(default_factory=FakeEmitters)
    issues: FakeIssueClient = field(default_factory=FakeIssueClient)
    probes: list[LLMProviderResult] = field(default_factory=list)
    missing: set[str] = field(default_factory=set)
    field_tagger: FakeFieldTagger = field(default_factory=FakeFieldTagger)

    def load(self, key: str) -> Any:
        if key in self.missing:
            raise ContractNotImplementedError(f"contract {key!r} is not implemented: test")
        table: dict[str, Any] = {
            "event_filter": lambda taxonomy, settings: FakeEventFilter(),
            "field_tagger": lambda taxonomy: self.field_tagger,
            "picks_scorer": lambda interests: FakePicksScorer(),
            "ics_feeds": self.emitters.write_ics_feeds,
            "ics_validator": self.emitters.validate_ics,
            "site_data": self.emitters.write_site_data,
            "digest_publish": self.emitters.publish_digest,
            "source_probe": self.emitters.probe_source,
            "health_update": update_health,
            "secret_scanner": build_secret_scanner,
            "llm_client": lambda config, env: None,
            "llm_probe": lambda config, env: list(self.probes),
            "issue_client": lambda env: self.issues if env.get("GITHUB_TOKEN") else None,
        }
        return table[key]


@pytest.fixture
def project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_registry: AdapterRegistry
) -> Path:
    """A scratch project with two enabled sources and their recordings; the current directory."""
    root = tmp_path / "project"
    (root / "config" / "sources.d").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "config" / "settings.yaml", root / "config" / "settings.yaml")
    (root / "config" / "sources.d" / "test.yaml").write_text(SOURCES_YAML, encoding="utf-8")
    fixtures = root / "tests" / "fixtures"
    FixtureStore.of(fixtures, "sites-a").add(
        make_document(A_URL, A_LINES, content_type="text/plain")
    )
    FixtureStore.of(fixtures, "sites-b").add(
        make_document(B_URL, B_LINES, content_type="text/plain")
    )
    fake_registry.register("lines")(LineAdapter)
    for name in [*filter(None, PROVIDER_CREDENTIAL_ENV.values()), *GITHUB_ENV]:
        monkeypatch.delenv(name, raising=False)
    for name in ("LLM_PROVIDERS", "GITHUB_ACTIONS", "GITHUB_REPOSITORY_OWNER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SEMINARS_HTTP_CACHE_DIR", str(tmp_path / "http-cache"))
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    fakes = Fakes()
    monkeypatch.setattr(wiring, "load", fakes.load)
    return fakes


def invoke(*args: str, env: Mapping[str, str] | None = None) -> Result:
    return CliRunner().invoke(cli.app, list(args), env=dict(env or {}))


def events_of(root: Path) -> list[dict[str, Any]]:
    return json.loads((root / "data" / "events.json").read_text(encoding="utf-8"))["events"]


class TestRunOffline:
    def test_an_offline_run_goes_from_fixtures_to_files(self, project: Path, fakes: Fakes) -> None:
        result = invoke("run", "--offline")
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert lines[0] == f"run: offline at {REFERENCE_NOW.isoformat()}"
        assert "sources: 2 ok, 0 empty, 0 failed" in lines
        assert "llm: off (rules only)" in lines
        assert lines[-1] == "result: ok"
        assert result.output.isascii()
        assert sorted(event["title"] for event in events_of(project)) == [
            "Catalysis at interfaces",
            "Enzymes by design",
            "Knots and braids",
        ]
        for name in ("events.json", "uid_map.json", "health.json", "last_run.json"):
            assert (project / "data" / name).is_file()
        assert (project / "public" / "feeds" / "all.ics").is_file()
        assert fakes.issues.calls == []

    def test_a_second_run_changes_no_data_file(self, project: Path, fakes: Fakes) -> None:
        assert invoke("run", "--offline").exit_code == 0
        before = (project / "data" / "events.json").read_bytes()
        result = invoke("run", "--offline")
        assert result.exit_code == 0
        assert (project / "data" / "events.json").read_bytes() == before
        assert "events: 3 stored, 0 new, 0 changed" in result.output

    def test_one_source_only(self, project: Path, fakes: Fakes) -> None:
        result = invoke("run", "--offline", "--source", "sites-b")
        assert result.exit_code == 0, result.output
        assert [event["title"] for event in events_of(project)] == ["Knots and braids"]

    def test_a_failing_source_is_listed_and_the_run_succeeds(
        self, project: Path, fakes: Fakes
    ) -> None:
        (project / "tests" / "fixtures" / "sites-b" / "manifest.json").unlink()
        result = invoke("run", "--offline")
        assert result.exit_code == 0, result.output
        assert "sources: 1 ok, 0 empty, 1 failed" in result.output
        assert "  failed sites-b: 0 events, 1 requests, " in result.output
        assert "http: FixtureNotFoundError: " in result.output

    def test_every_source_failing_fails_the_run(self, project: Path, fakes: Fakes) -> None:
        for name in ("sites-a", "sites-b"):
            (project / "tests" / "fixtures" / name / "manifest.json").write_text(
                json.dumps({"schema_version": 1, "source_id": name, "entries": {}}) + "\n",
                encoding="utf-8",
            )
        (project / "tests" / "fixtures" / "sites-c").mkdir()
        store = FixtureStore.of(project / "tests" / "fixtures", "sites-c")
        store.add(make_document("https://c.example.edu/", "x\n", content_type="text/plain"))
        result = invoke("run", "--offline")
        assert result.exit_code == 1
        assert result.output.splitlines()[-1] == "result: failed: every source failed"

    def test_a_dry_run_leaves_the_project_alone(self, project: Path, fakes: Fakes) -> None:
        result = invoke("run", "--offline", "--dry-run")
        assert result.exit_code == 0, result.output
        assert "run: offline, dry run at " in result.output
        assert not (project / "data").exists()
        assert not (project / "public").exists()

    def test_an_offline_run_leaves_the_data_of_a_live_run_alone(
        self, project: Path, fakes: Fakes
    ) -> None:
        record = LastRun(started_at=REFERENCE_NOW, finished_at=REFERENCE_NOW, ok=True, mode="live")
        (project / "data").mkdir()
        last_run = project / "data" / "last_run.json"
        last_run.write_text(record.model_dump_json() + "\n", encoding="utf-8")
        before = last_run.read_bytes()
        result = invoke("run", "--offline")
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert lines[0] == cli.OFFLINE_OVER_LIVE
        assert lines[1].startswith("run: offline, dry run at ")
        assert lines[-1] == "result: ok"
        assert sorted(path.name for path in (project / "data").iterdir()) == ["last_run.json"]
        assert last_run.read_bytes() == before
        assert not (project / "public").exists()

    def test_an_offline_run_over_offline_data_writes_into_the_project(
        self, project: Path, fakes: Fakes
    ) -> None:
        assert invoke("run", "--offline").exit_code == 0
        result = invoke("run", "--offline")
        assert result.exit_code == 0, result.output
        assert cli.OFFLINE_OVER_LIVE not in result.output
        assert result.output.splitlines()[0].startswith("run: offline at ")

    def test_skip_when_a_run_of_the_mode_succeeded_today(self, project: Path, fakes: Fakes) -> None:
        assert invoke("run", "--offline").exit_code == 0
        result = invoke("run", "--offline", "--skip-if-done-today")
        assert result.exit_code == 0
        assert result.output == "skipped: a offline run already succeeded today\n"

    def test_no_skip_after_a_failed_run(self, project: Path, fakes: Fakes) -> None:
        record = LastRun(
            started_at=REFERENCE_NOW, finished_at=REFERENCE_NOW, ok=False, mode="offline"
        )
        (project / "data").mkdir()
        (project / "data" / "last_run.json").write_text(
            record.model_dump_json() + "\n", encoding="utf-8"
        )
        result = invoke("run", "--offline", "--skip-if-done-today")
        assert result.exit_code == 0
        assert "result: ok" in result.output


def snapshot(directory: Path) -> dict[Path, tuple[bytes, int, int]]:
    """Content, inode and modification time of every file below ``directory``."""
    return {
        path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def go_live(
    monkeypatch: pytest.MonkeyPatch, *, at: datetime, pages: Mapping[str, str] | None = None
) -> None:
    """Make the next commands live runs at ``at`` that fetch ``pages`` from a fake client."""
    client = FakeHttpClient()
    for url, text in (pages or {A_URL: A_LINES, B_URL: B_LINES}).items():
        client.add(url, text, content_type="text/plain")
    monkeypatch.setattr(
        wiring, "_http", lambda config, env, offline: (lambda chosen: client, lambda: None)
    )
    monkeypatch.setattr(cli, "run_time", lambda *, offline, fixtures_dir: at)


class TestRunRules:
    """The two rules of ``seminars run`` that the update workflow relies on, and offline data."""

    def test_a_skipped_offline_run_writes_nothing_below_data(
        self, project: Path, fakes: Fakes
    ) -> None:
        assert invoke("run", "--offline").exit_code == 0
        before = snapshot(project / "data")
        result = invoke("run", "--offline", "--skip-if-done-today")
        assert result.exit_code == 0
        assert result.output.startswith("skipped: ")
        assert snapshot(project / "data") == before

    def test_a_skipped_live_run_writes_nothing_below_data(
        self, project: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        go_live(monkeypatch, at=REFERENCE_NOW)
        assert invoke("run").exit_code == 0
        before = snapshot(project / "data")
        go_live(monkeypatch, at=REFERENCE_NOW + timedelta(hours=2))
        result = invoke("run", "--skip-if-done-today")
        assert result.exit_code == 0
        assert result.output == "skipped: a live run already succeeded today\n"
        assert snapshot(project / "data") == before

    def test_a_live_run_rewrites_the_record_when_nothing_else_changed(
        self, project: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        go_live(monkeypatch, at=REFERENCE_NOW)
        assert invoke("run").exit_code == 0
        events = (project / "data" / "events.json").read_bytes()
        record = (project / "data" / "last_run.json").read_bytes()
        go_live(monkeypatch, at=REFERENCE_NOW + timedelta(hours=2))
        result = invoke("run")
        assert result.exit_code == 0, result.output
        assert "events: 3 stored, 0 new, 0 changed" in result.output
        assert (project / "data" / "events.json").read_bytes() == events
        assert (project / "data" / "last_run.json").read_bytes() != record

    def test_a_dry_run_writes_no_record(
        self, project: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        go_live(monkeypatch, at=REFERENCE_NOW)
        assert invoke("run").exit_code == 0
        before = snapshot(project / "data")
        go_live(monkeypatch, at=REFERENCE_NOW + timedelta(hours=2))
        assert invoke("run", "--dry-run").exit_code == 0
        assert snapshot(project / "data") == before

    def test_a_failing_output_writer_ends_with_status_one(
        self, project: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(*args: Any, **kwargs: Any) -> list[Path]:
            raise OSError("read-only file system")

        monkeypatch.setattr(fakes.emitters, "write_site_data", broken)
        result = invoke("run", "--offline")
        assert result.exit_code == 1, result.output
        assert "warning: writing the feeds and the site data failed (OSError)" in result.output
        assert result.output.splitlines()[-1] == "result: failed: see the warnings"
        record = read_model(project / "data" / "last_run.json", LastRun)
        assert record is not None
        assert record.ok is False

    def test_a_live_run_over_offline_data_starts_from_nothing_and_says_so(
        self, project: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert invoke("run", "--offline").exit_code == 0
        pages = {
            A_URL: "9|Live talk|2026-11-20T16:00:00-08:00\n",
            B_URL: "8|Live talk|2026-11-21T16:00:00-08:00\n",
        }
        go_live(monkeypatch, at=REFERENCE_NOW, pages=pages)
        result = invoke("run")
        assert result.exit_code == 0, result.output
        assert f"warning: {LIVE_OVER_OFFLINE}" in result.output.splitlines()
        assert "events: 2 stored, 2 new, 0 changed, 0 missing, 0 removed" in result.output
        assert sorted(event["title"] for event in events_of(project)) == ["Live talk"] * 2
        report = read_model(project / "data" / "run_report.json", RunReport)
        assert report is not None
        assert (report.mode, report.warnings[0]) == ("live", LIVE_OVER_OFFLINE)
        again = invoke("run")
        assert again.exit_code == 0
        assert LIVE_OVER_OFFLINE not in again.output


class TestRunProblems:
    def test_a_leak_ends_with_status_three_and_says_so(self, project: Path, fakes: Fakes) -> None:
        forged = "gh" + "p_" + "L" * 36
        record = make_event(
            uid="planted", start=REFERENCE_NOW + timedelta(days=2), end=None, abstract=forged
        )
        (project / "data").mkdir()
        (project / "data" / "events.json").write_text(dump_events([record]), encoding="utf-8")
        result = invoke("run", "--offline")
        assert result.exit_code == 3, result.output
        assert "OUTPUT CHECK FAILED: nothing of this run may be committed or deployed" in (
            result.output
        )
        assert "  secret scan: data/events.json:" in result.output
        assert result.output.splitlines()[-1] == "result: output check failed (exit status 3)"
        assert forged not in result.output
        report = read_model(project / "data" / "run_report.json", RunReport)
        assert report is not None
        assert not report.ok
        assert report.secret_findings

    def test_an_unknown_source_is_a_usage_error(self, project: Path, fakes: Fakes) -> None:
        result = invoke("run", "--offline", "--source", "sites-x")
        assert result.exit_code == 2
        assert "unknown source id(s): sites-x" in result.output

    def test_a_disabled_source_is_a_usage_error(self, project: Path, fakes: Fakes) -> None:
        assert invoke("run", "--offline", "--source", "sites-off").exit_code == 2

    def test_llm_options_are_checked(self, project: Path, fakes: Fakes) -> None:
        assert invoke("run", "--offline", "--llm", "nobody").exit_code == 2
        both = invoke("run", "--offline", "--no-llm", "--llm", "claude-cli")
        assert both.exit_code == 2
        assert "exclude each other" in both.output

    def test_an_offline_run_without_recordings_cannot_start(
        self, project: Path, fakes: Fakes
    ) -> None:
        shutil.rmtree(project / "tests" / "fixtures")
        result = invoke("run", "--offline")
        assert result.exit_code == 1
        assert "error: offline mode needs recorded fixtures" in result.output

    def test_a_missing_package_is_reported(self, project: Path, fakes: Fakes) -> None:
        fakes.missing.add("ics_feeds")
        result = invoke("run", "--offline")
        assert result.exit_code == 1
        assert "error: contract 'ics_feeds' is not implemented" in result.output

    def test_a_damaged_data_file_stops_the_run(self, project: Path, fakes: Fakes) -> None:
        (project / "data").mkdir()
        (project / "data" / "events.json").write_text("{not json\n", encoding="utf-8")
        result = invoke("run", "--offline")
        assert result.exit_code == 1
        assert "events.json: cannot be read (JSONDecodeError)" in result.output

    def test_outside_a_project_nothing_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: Fakes
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = invoke("run", "--offline")
        assert result.exit_code == 1
        assert "no config/settings.yaml found" in result.output

    def test_the_llm_can_be_asked_for_in_an_offline_run(
        self, project: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[Mapping[str, str]] = []
        original = fakes.load

        def load(key: str) -> Any:
            if key == "llm_client":
                return lambda config, env: asked.append(dict(env))
            return original(key)

        monkeypatch.setattr(wiring, "load", load)
        assert invoke("run", "--offline").exit_code == 0
        assert asked == []
        assert invoke("run", "--offline", "--llm", "claude-cli").exit_code == 0
        assert asked[0]["LLM_PROVIDERS"] == "claude-cli"

    def test_a_live_run_opens_issues_when_github_is_configured(
        self, project: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            wiring, "_http", lambda config, env, offline: (lambda s: FakeHttpClient(), lambda: None)
        )
        result = invoke("run", env=GITHUB_ENV)
        assert result.exit_code == 1  # every source fails: the fake client has no pages
        assert (
            "find_by_title",
            {"title": "Output check failed: the last run was not published", "state": "open"},
        ) in fakes.issues.calls


class TestLLMSmoke:
    def test_every_provider_is_reported(self, project: Path, fakes: Fakes) -> None:
        fakes.probes = [
            LLMProviderResult(provider="claude-cli", status="ok", model="m1"),
            LLMProviderResult(
                provider="anthropic", status="failed", failure_kind="auth", error="bad key"
            ),
            LLMProviderResult(provider="openai", status="unused"),
        ]
        result = invoke("llm-smoke")
        assert result.output.splitlines() == [
            "claude-cli: ok (model m1)",
            "anthropic: failed: auth: bad key",
            "openai: unused",
        ]
        assert result.exit_code == 1

    def test_success_when_every_probed_provider_answers(self, project: Path, fakes: Fakes) -> None:
        fakes.probes = [LLMProviderResult(provider="claude-cli", status="ok")]
        assert invoke("llm-smoke").exit_code == 0

    def test_no_provider_is_a_failure(self, project: Path, fakes: Fakes) -> None:
        result = invoke("llm-smoke")
        assert (result.exit_code, result.output) == (1, "no LLM provider is configured\n")

    def test_a_missing_llm_package_is_reported(self, project: Path, fakes: Fakes) -> None:
        fakes.missing.add("llm_probe")
        assert invoke("llm-smoke").exit_code == 1


class TestSources:
    def test_list(self, project: Path, fakes: Fakes) -> None:
        result = invoke("sources", "list")
        assert result.exit_code == 0
        assert result.output.splitlines() == [
            f"sites-a  stanford-sites  lines  enabled  {A_URL}",
            f"sites-b  stanford-sites  lines  enabled  {B_URL}",
            "sites-off  stanford-sites  lines  disabled  https://off.example.edu/events",
            "2 of 3 sources enabled",
        ]

    def test_check_fetches_live_and_writes_nothing(
        self, project: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeHttpClient({A_URL: A_LINES})
        monkeypatch.setattr(wiring, "live_client", lambda config, env: _Closing(client))
        result = invoke("sources", "check", "sites-a")
        assert result.exit_code == 0, result.output
        assert result.output.startswith("  ok     sites-a: 2 events, 1 requests, ")
        assert client.requests == [A_URL]
        assert not (project / "data").exists()

    def test_check_fails_when_a_source_fails(
        self, project: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeHttpClient({A_URL: A_LINES})
        monkeypatch.setattr(wiring, "live_client", lambda config, env: _Closing(client))
        result = invoke("sources", "check")
        assert result.exit_code == 1
        assert "failed sites-b: " in result.output

    def test_check_refuses_unknown_sources(self, project: Path, fakes: Fakes) -> None:
        assert invoke("sources", "check", "sites-x").exit_code == 2


class _Closing:
    """A context manager around a fake client, like ``PoliteClient``."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def __enter__(self) -> Any:
        return self.client

    def __exit__(self, *exc: object) -> None:
        return None


class TestAddSource:
    def test_the_suggestion_is_printed(
        self, project: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = "https://new.example.edu/events"
        client = FakeHttpClient({url: "<html></html>"})
        monkeypatch.setattr(wiring, "live_client", lambda config, env: _Closing(client))
        result = invoke("add-source", url)
        assert result.exit_code == 0, result.output
        assert result.output.splitlines() == [
            "format: unknown (text/html)",
            "suggested entry for config/sources.d/<group>.yaml:",
            "sources:",
            f"  - url: {url}",
        ]

    def test_a_fetch_error_is_a_failure(
        self, project: Path, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = "https://new.example.edu/events"
        client = FakeHttpClient()
        client.add(url, "gone", status=404)
        monkeypatch.setattr(wiring, "live_client", lambda config, env: _Closing(client))
        result = invoke("add-source", url)
        assert result.exit_code == 1
        assert "error: " in result.output


class TestValidate:
    def test_the_output_of_a_run_is_valid(self, project: Path, fakes: Fakes) -> None:
        assert invoke("run", "--offline").exit_code == 0
        result = invoke("validate")
        assert result.exit_code == 0, result.output
        assert result.output.splitlines()[-1] == "result: ok"

    def test_a_project_without_outputs_does_not_pass(self, project: Path, fakes: Fakes) -> None:
        result = invoke("validate")
        assert result.exit_code == 1
        assert "problem: data/events.json: missing (made by seminars run)" in result.output
        assert "problem: public/feeds/all.ics: missing" in result.output
        assert result.output.splitlines()[-1] == "result: failed"

    def test_a_charset_problem_is_status_three(self, project: Path, fakes: Fakes) -> None:
        assert invoke("run", "--offline").exit_code == 0
        (project / "data" / "note.txt").write_text("no final newline", encoding="utf-8")
        result = invoke("validate")
        assert result.exit_code == 3
        assert "charset: data/note.txt:missing final newline" in result.output

    def test_a_broken_feed_is_a_problem(self, project: Path, fakes: Fakes) -> None:
        assert invoke("run", "--offline").exit_code == 0
        (project / "public" / "feeds" / "math.ics").write_text("garbage\n", encoding="utf-8")
        result = invoke("validate")
        assert result.exit_code == 1
        assert (
            "problem: public/feeds/math.ics: does not start with BEGIN:VCALENDAR" in result.output
        )

    def test_without_a_validator_feeds_cannot_pass(self, project: Path, fakes: Fakes) -> None:
        assert invoke("run", "--offline").exit_code == 0
        fakes.missing.add("ics_validator")
        result = invoke("validate")
        assert result.exit_code == 1
        assert "public/feeds/all.ics: no ICS validator" in result.output


class TestDigest:
    def test_a_dry_run_prints_the_markdown(self, project: Path, fakes: Fakes) -> None:
        assert invoke("run", "--offline").exit_code == 0
        result = invoke("digest", "--dry-run", env=GITHUB_ENV)
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert lines[0].startswith("# Seminars - week of ")
        assert "events: 3" in lines
        assert fakes.issues.issues == []

    def test_the_issue_is_opened_once_per_week(self, project: Path, fakes: Fakes) -> None:
        env = {**GITHUB_ENV, "GITHUB_REPOSITORY_OWNER": "someone"}
        first = invoke("digest", env=env)
        assert first.exit_code == 0, first.output
        assert first.output == "digest: opened issue #1; closed: none\n"
        assert "cc @someone" in fakes.issues.get(1).body
        again = invoke("digest", env=env)
        assert again.output == "digest: issue #1 of this week exists; nothing changed\n"

    def test_without_github_nothing_is_posted(self, project: Path, fakes: Fakes) -> None:
        result = invoke("digest")
        assert result.exit_code == 0
        assert result.output.startswith("# Seminars - week of ")

    def test_control_characters_are_not_printed(self) -> None:
        assert cli.printable_markdown("a\x1b[31mb\tc\nd\x07") == "a[31mb\tc\nd"


class TestBuildSite:
    def make_site(self, root: Path) -> None:
        (root / "site").mkdir()
        (root / "site" / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
        (root / "locales").mkdir()
        (root / "locales" / "en.json").write_text("{}\n", encoding="utf-8")

    def test_public_is_assembled_from_the_committed_data(self, project: Path, fakes: Fakes) -> None:
        assert invoke("run", "--offline").exit_code == 0
        self.make_site(project)
        (project / "public" / "stale.txt").write_text("old\n", encoding="utf-8")
        result = invoke("build-site")
        assert result.exit_code == 0, result.output
        public = project / "public"
        for name in ("index.html", "locales/en.json", "feeds/all.ics", "events.json"):
            assert (public / name).is_file(), name
        assert not (public / "stale.txt").exists()
        status = json.loads((public / "status.json").read_text(encoding="utf-8"))
        last_run = read_model(project / "data" / "last_run.json", LastRun)
        assert last_run is not None
        assert status["generated_at"] == last_run.finished_at.isoformat().replace("+00:00", "Z")

    def test_the_same_data_gives_the_same_site(self, project: Path, fakes: Fakes) -> None:
        assert invoke("run", "--offline").exit_code == 0
        self.make_site(project)
        assert invoke("build-site").exit_code == 0
        first = (project / "public" / "status.json").read_bytes()
        assert invoke("build-site").exit_code == 0
        assert (project / "public" / "status.json").read_bytes() == first

    def test_a_missing_site_fails(self, project: Path, fakes: Fakes) -> None:
        result = invoke("build-site")
        assert result.exit_code == 1
        assert "missing: site/" in result.output
        assert "missing: locales/" in result.output

    def test_a_secret_in_the_site_is_status_three(self, project: Path, fakes: Fakes) -> None:
        self.make_site(project)
        forged = "sk-" + "ant-" + "api03-" + "S" * 40
        (project / "site" / "app.js").write_text(f"const k = '{forged}';\n", encoding="utf-8")
        result = invoke("build-site")
        assert result.exit_code == 3
        assert "secret scan: public/app.js:1 (rule anthropic-key)" in result.output


class TestLines:
    def report(self, **overrides: Any) -> RunReport:
        values: dict[str, Any] = {
            "started_at": REFERENCE_NOW,
            "finished_at": REFERENCE_NOW,
            "mode": "live",
            "ok": True,
        }
        return RunReport(**{**values, **overrides})

    def test_the_llm_line_names_the_provider(self) -> None:
        report = self.report(
            llm=LLMUsage(enabled=True, provider="claude-cli", calls=3, cache_hits=9)
        )
        assert "llm: claude-cli, 3 calls, 9 cached" in cli.report_lines(report)

    def test_a_failure_without_failed_sources_points_at_the_warnings(self) -> None:
        ok_source = SourceRunResult(source_id="sites-a", status="ok")
        report = self.report(ok=False, sources=[ok_source], warnings=["writing failed (X)"])
        lines = cli.report_lines(report)
        assert lines[-2:] == ["warning: writing failed (X)", "result: failed: see the warnings"]

    def test_scraped_text_in_errors_is_escaped(self) -> None:
        failed = SourceRunResult(
            source_id="sites-a", status="failed", error_kind="http", error="bad \u00e9"
        )
        lines = cli.report_lines(self.report(sources=[failed]))
        assert all(line.isascii() for line in lines)

    def test_provider_line_without_details(self) -> None:
        result = LLMProviderResult(provider="openai", status="failed")
        assert cli.provider_line(result) == "openai: failed: other: no detail"


def test_the_help_is_plain_ascii() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    assert result.output.isascii()
    for command in (
        "run",
        "llm-smoke",
        "sources",
        "add-source",
        "validate",
        "digest",
        "build-site",
    ):
        assert command in result.output


def test_a_bad_option_is_a_usage_error_in_plain_ascii() -> None:
    result = invoke("run", "--no-such-option")
    assert result.exit_code == 2
    assert result.output.isascii()
