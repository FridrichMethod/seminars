"""Tests for the output checks: secret scan and charset rule, before and after writing."""

from __future__ import annotations

from pathlib import Path

import pytest

from fakes import FakeIssueClient, FakeSecretScanner
from seminars.config import ProjectPaths
from seminars.models import Issue, IssueStateFilter, SecretFinding
from seminars.pipeline.checks import (
    CHECK_ISSUE_TITLE,
    EXIT_OUTPUT_CHECK,
    MAX_LISTED,
    CheckResult,
    check_issue_body,
    check_texts,
    check_tree,
    describe,
    sync_check_issue,
)
from seminars.pipeline.secret_scan import build_secret_scanner

NEEDLE = "needle-value"
EMOJI = "\U0001f600"


def finding(origin: str, line: int = 1, rule: str = "fake-needle") -> SecretFinding:
    return SecretFinding(origin=origin, line=line, rule=rule)


class TestCheckTexts:
    def test_clean_files_pass(self, tmp_path: Path) -> None:
        files = {tmp_path / "data" / "events.json": "[]\n"}
        assert check_texts(files, FakeSecretScanner([NEEDLE]), root=tmp_path).ok

    def test_a_secret_is_reported_by_file_and_line(self, tmp_path: Path) -> None:
        files = {tmp_path / "public" / "events.json": f"ok\nthe {NEEDLE}\n"}
        result = check_texts(files, FakeSecretScanner([NEEDLE]), root=tmp_path)
        assert result.secret_findings == (finding("public/events.json", 2),)
        assert not result.ok

    def test_the_charset_rule_applies_to_data_only(self, tmp_path: Path) -> None:
        files = {
            tmp_path / "data" / "events.json": f'["{EMOJI}"]\n',
            tmp_path / "public" / "events.json": f'["{EMOJI}"]\n',
        }
        result = check_texts(files, FakeSecretScanner(), root=tmp_path)
        assert result.charset_findings == ("data/events.json:1:3 U+1F600",)

    def test_a_file_outside_the_root_keeps_its_full_path(self, tmp_path: Path) -> None:
        outside = tmp_path / "elsewhere.txt"
        result = check_texts(
            {outside: NEEDLE}, FakeSecretScanner([NEEDLE]), root=tmp_path / "project"
        )
        assert result.secret_findings[0].origin == outside.as_posix()

    def test_the_real_scanner_stops_a_forged_token(self, tmp_path: Path) -> None:
        forged = "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        files = {tmp_path / "data" / "events.json": f'{{"title": "{forged}"}}\n'}
        result = check_texts(files, build_secret_scanner(), root=tmp_path)
        assert [(f.origin, f.line) for f in result.secret_findings] == [("data/events.json", 1)]
        assert forged not in " ".join(describe(result))


class TestCheckTree:
    def test_what_is_on_disk_is_checked(self, tmp_path: Path) -> None:
        paths = ProjectPaths(tmp_path)
        (paths.data_dir / "llm_cache").mkdir(parents=True)
        (paths.data_dir / "llm_cache" / "entry.json").write_text(f"{EMOJI}\n", encoding="utf-8")
        paths.public_feeds_dir.mkdir(parents=True)
        (paths.public_feeds_dir / "all.ics").write_text(f"X:{NEEDLE}\n", encoding="utf-8")
        result = check_tree(paths, FakeSecretScanner([NEEDLE]))
        assert result.secret_findings == (finding("public/feeds/all.ics"),)
        assert result.charset_findings == ("data/llm_cache/entry.json:1:1 U+1F600",)

    def test_an_empty_project_passes(self, tmp_path: Path) -> None:
        assert check_tree(ProjectPaths(tmp_path), FakeSecretScanner([NEEDLE])).ok


class TestResults:
    def test_plus_unites_without_repeats_in_a_stable_order(self) -> None:
        first = CheckResult((finding("b"), finding("a")), ("z", "y"))
        second = CheckResult((finding("a"),), ("y", "x"))
        both = first.plus(second)
        assert both.secret_findings == (finding("a"), finding("b"))
        assert both.charset_findings == ("x", "y", "z")
        assert both == second.plus(first)

    def test_describe_names_place_and_rule_only(self) -> None:
        result = CheckResult(
            (finding("data/x.json", 3, "github-token"),), ("data/x.json:1:2 U+00A0",)
        )
        assert describe(result) == [
            "secret scan: data/x.json:3 (rule github-token)",
            "charset: data/x.json:1:2 U+00A0",
        ]

    def test_the_issue_body_lists_a_limited_number_of_findings(self) -> None:
        many = CheckResult(
            charset_findings=tuple(f"data/f{n:03}.json:1:1 U+00A0" for n in range(60))
        )
        body = check_issue_body(many)
        assert body.count("    charset: ") == MAX_LISTED
        assert f"... and {60 - MAX_LISTED} more" in body
        assert body.endswith("\n")
        assert body.isascii()

    def test_the_exit_status_is_three(self) -> None:
        assert EXIT_OUTPUT_CHECK == 3


FAILED = CheckResult(charset_findings=("data/x.json:1:1 U+00A0",))


class BrokenIssues(FakeIssueClient):
    def find_by_title(self, title: str, *, state: IssueStateFilter = "all") -> Issue | None:
        raise RuntimeError("GitHub is down")


class TestCheckIssue:
    def test_no_client_does_nothing(self) -> None:
        sync_check_issue(None, FAILED)

    def test_a_failed_check_opens_one_issue_and_updates_it(
        self, fake_issues: FakeIssueClient
    ) -> None:
        sync_check_issue(fake_issues, FAILED)
        sync_check_issue(fake_issues, FAILED)
        assert [issue.title for issue in fake_issues.issues] == [CHECK_ISSUE_TITLE]
        assert not [call for call in fake_issues.calls if call[0] == "update"]
        other = CheckResult(charset_findings=("data/y.json:2:2 U+00A0",))
        sync_check_issue(fake_issues, other)
        assert "data/y.json" in fake_issues.get(1).body
        assert len(fake_issues.issues) == 1

    def test_a_passing_check_closes_the_issue(self, fake_issues: FakeIssueClient) -> None:
        sync_check_issue(fake_issues, FAILED)
        sync_check_issue(fake_issues, CheckResult())
        assert fake_issues.get(1).state == "closed"
        assert fake_issues.comments[1] == ["The output check passes again."]

    def test_a_passing_check_without_an_issue_opens_nothing(
        self, fake_issues: FakeIssueClient
    ) -> None:
        sync_check_issue(fake_issues, CheckResult())
        assert fake_issues.issues == []

    def test_an_api_failure_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        sync_check_issue(BrokenIssues(), FAILED)
        assert "the output check issue was not updated (RuntimeError)" in caplog.text
