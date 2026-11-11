"""Tests for the GitHub issue client; the API is a mock transport, never the network."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from seminars.contracts import (
    LABEL_DIGEST,
    LABEL_SOURCE_BROKEN,
    IssueClient,
    load_implementation,
)
from seminars.github import (
    API_VERSION,
    ATTEMPTS,
    MAX_BODY_CHARS,
    GitHubError,
    GitHubIssueClient,
    build_issue_client,
    repository_owner,
)

TOKEN = "t0ken-" + "x" * 6
REPOSITORY = "example/seminars"
PREFIX = f"/repos/{REPOSITORY}"

Handler = Callable[[httpx.Request], httpx.Response]


class FakeGitHub:
    """A small in-memory stand-in for the issue endpoints of the REST API."""

    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []
        self.labels: dict[str, dict[str, Any]] = {}
        self.comments: dict[int, list[str]] = {}
        self.requests: list[httpx.Request] = []
        self.fail: list[int | Exception] = []  # answers to give before the real ones

    def add(self, title: str, *, labels: tuple[str, ...] = (), state: str = "open", **extra: Any):
        number = len(self.issues) + 1
        issue = {
            "number": number,
            "title": title,
            "body": extra.pop("body", "text"),
            "state": state,
            "labels": [{"name": name} for name in labels],
            "created_at": f"2026-11-{number:02d}T12:00:00Z",
            "html_url": f"https://github.com/{REPOSITORY}/issues/{number}",
            **extra,
        }
        self.issues.append(issue)
        return issue

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            answer = self.fail.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return httpx.Response(answer, json={"message": "Server\x1b[31m trouble\nagain"})
        path = request.url.path.removeprefix(PREFIX)
        payload = json.loads(request.content) if request.content else {}
        if path == "/issues" and request.method == "GET":
            return self._list(request)
        if path == "/issues" and request.method == "POST":
            issue = self.add(
                payload["title"], labels=tuple(payload["labels"]), body=payload["body"]
            )
            return httpx.Response(201, json=issue)
        if path.startswith("/issues/") and path.endswith("/comments"):
            number = int(path.split("/")[2])
            self.comments.setdefault(number, []).append(payload["body"])
            return httpx.Response(201, json={})
        if path.startswith("/issues/"):
            issue = self.issues[int(path.split("/")[2]) - 1]
            issue.update(payload)
            return httpx.Response(200, json=issue)
        if path == "/labels" and request.method == "POST":
            if payload["name"] in self.labels:
                return httpx.Response(422, json={"message": "Validation Failed"})
            self.labels[payload["name"]] = payload
            return httpx.Response(201, json=payload)
        if path.startswith("/labels/"):
            name = path.removeprefix("/labels/")
            found = name in self.labels
            return httpx.Response(200 if found else 404, json=self.labels.get(name, {}))
        return httpx.Response(404, json={"message": "Not Found"})

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        chosen = [
            issue
            for issue in reversed(self.issues)
            if params["state"] in ("all", issue["state"])
            and ("labels" not in params or {"name": params["labels"]} in issue["labels"])
        ]
        size, page = int(params["per_page"]), int(params["page"])
        return httpx.Response(200, json=chosen[(page - 1) * size : page * size])


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def client(github: FakeGitHub) -> GitHubIssueClient:
    sleeps: list[float] = []
    made = GitHubIssueClient(
        REPOSITORY, TOKEN, transport=httpx.MockTransport(github), sleep=sleeps.append
    )
    made.sleeps = sleeps  # type: ignore[attr-defined]
    return made


class TestFactory:
    def test_no_client_without_token_or_repository(self) -> None:
        assert build_issue_client({}) is None
        assert build_issue_client({"GITHUB_TOKEN": TOKEN}) is None
        assert build_issue_client({"GITHUB_REPOSITORY": REPOSITORY}) is None
        assert build_issue_client({"GITHUB_TOKEN": "  ", "GITHUB_REPOSITORY": REPOSITORY}) is None

    def test_a_client_for_the_repository_of_the_environment(self) -> None:
        made = build_issue_client({"GITHUB_TOKEN": f" {TOKEN}\n", "GITHUB_REPOSITORY": REPOSITORY})
        assert isinstance(made, GitHubIssueClient)
        assert isinstance(made, IssueClient)
        assert made.repository == REPOSITORY
        made.release()

    def test_it_is_the_registered_implementation(self) -> None:
        assert load_implementation("issue_client") is build_issue_client

    @pytest.mark.parametrize("repository", ["seminars", "a/b/c", "../x/y", "owner/na me", "/x"])
    def test_a_malformed_repository_is_refused(self, repository: str) -> None:
        with pytest.raises(GitHubError, match="owner/name"):
            build_issue_client({"GITHUB_TOKEN": TOKEN, "GITHUB_REPOSITORY": repository})

    def test_the_token_is_never_shown(self, client: GitHubIssueClient) -> None:
        assert TOKEN not in repr(client)
        assert TOKEN not in str(client)

    @pytest.mark.parametrize(
        ("env", "owner"),
        [
            ({"GITHUB_REPOSITORY_OWNER": "FridrichMethod"}, "FridrichMethod"),
            ({"GITHUB_REPOSITORY": "some-one/seminars"}, "some-one"),
            ({"GITHUB_REPOSITORY_OWNER": "a b", "GITHUB_REPOSITORY": "x/y"}, None),
            ({"GITHUB_REPOSITORY_OWNER": "@team/all"}, None),
            ({}, None),
        ],
    )
    def test_repository_owner(self, env: dict[str, str], owner: str | None) -> None:
        assert repository_owner(env) == owner


class TestRequests:
    def test_headers(self, client: GitHubIssueClient, github: FakeGitHub) -> None:
        client.find_by_label("digest")
        headers = github.requests[0].headers
        assert headers["authorization"] == "Bearer " + TOKEN
        assert headers["accept"] == "application/vnd.github+json"
        assert headers["x-github-api-version"] == API_VERSION
        assert headers["user-agent"] == "seminars-bot"
        assert github.requests[0].url.host == "api.github.com"

    def test_transport_errors_and_bad_gateways_are_retried(
        self, client: GitHubIssueClient, github: FakeGitHub
    ) -> None:
        github.add("Seminars - week of 2026-11-09", labels=("digest",))
        github.fail = [httpx.ConnectError("refused"), 503]
        assert [issue.number for issue in client.find_by_label("digest")] == [1]
        assert len(github.requests) == 3
        assert client.sleeps == [2.0, 4.0]  # type: ignore[attr-defined]

    def test_a_request_that_keeps_failing_is_an_error(
        self, client: GitHubIssueClient, github: FakeGitHub
    ) -> None:
        github.fail = [502] * ATTEMPTS
        with pytest.raises(GitHubError, match="HTTP 502 after 3 attempts"):
            client.find_by_label("digest")
        assert len(github.requests) == ATTEMPTS

    def test_an_error_answer_is_not_retried_and_its_text_is_escaped(
        self, client: GitHubIssueClient, github: FakeGitHub
    ) -> None:
        github.fail = [403]
        with pytest.raises(GitHubError) as caught:
            client.open("title", "body")
        assert caught.value.status == 403
        message = str(caught.value)
        assert message.isascii()
        assert "\x1b" not in message
        assert "\n" not in message
        assert "Server\\x1b[31m trouble\\nagain" in message
        assert TOKEN not in message
        assert len(github.requests) == 1

    def test_an_answer_that_is_not_json(self, github: FakeGitHub) -> None:
        def html(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>maintenance</html>")

        broken = GitHubIssueClient(REPOSITORY, TOKEN, transport=httpx.MockTransport(html))
        with pytest.raises(GitHubError, match="not JSON"):
            broken.find_by_label("digest")
        with pytest.raises(GitHubError, match="not JSON"):
            broken.update(1)

    @pytest.mark.parametrize("answer", [{"message": "x"}, [1, 2], "text"])
    def test_an_answer_of_the_wrong_shape(self, answer: Any) -> None:
        def odd(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=answer)

        broken = GitHubIssueClient(REPOSITORY, TOKEN, transport=httpx.MockTransport(odd))
        with pytest.raises(GitHubError):
            broken.update(7)
        if not isinstance(answer, list):
            with pytest.raises(GitHubError, match="not a list"):
                broken.find_by_label("digest")


class TestIssues:
    def test_open_update_and_close(self, client: GitHubIssueClient, github: FakeGitHub) -> None:
        issue = client.open("Source broken: sites-x", "body one", labels=["source-broken"])
        assert (issue.number, issue.title, issue.body, issue.state) == (
            1,
            "Source broken: sites-x",
            "body one",
            "open",
        )
        assert issue.labels == ["source-broken"]
        assert issue.url == f"https://github.com/{REPOSITORY}/issues/1"
        assert issue.created_at.isoformat() == "2026-11-01T12:00:00+00:00"
        changed = client.update(1, body="body two")
        assert (changed.title, changed.body) == ("Source broken: sites-x", "body two")
        renamed = client.update(1, title="Other title")
        assert (renamed.title, renamed.body) == ("Other title", "body two")
        assert client.update(1) == renamed
        client.close(1, comment="Recovered.")
        assert github.issues[0]["state"] == "closed"
        assert github.comments == {1: ["Recovered."]}
        client.close(1)
        assert github.comments == {1: ["Recovered."]}

    def test_find_by_label_lists_newest_first_and_skips_pull_requests(
        self, client: GitHubIssueClient, github: FakeGitHub
    ) -> None:
        github.add("week 1", labels=("digest",), state="closed")
        github.add("week 2", labels=("digest",))
        github.add("a pull request", labels=("digest",), pull_request={"url": "x"})
        github.add("broken", labels=("source-broken",))
        assert [issue.title for issue in client.find_by_label("digest")] == ["week 2"]
        everything = client.find_by_label("digest", state="all")
        assert [issue.title for issue in everything] == ["week 2", "week 1"]
        assert [issue.state for issue in everything] == ["open", "closed"]
        assert client.find_by_label("llm-auth") == []

    def test_find_by_title_is_exact_and_takes_the_newest(
        self, client: GitHubIssueClient, github: FakeGitHub
    ) -> None:
        github.add("Seminars - week of 2026-11-09", state="closed")
        github.add("Seminars - week of 2026-11-09")
        github.add("Seminars - week of 2026-11-09 (draft)")
        found = client.find_by_title("Seminars - week of 2026-11-09")
        assert found is not None
        assert found.number == 2
        closed = client.find_by_title("Seminars - week of 2026-11-09", state="closed")
        assert closed is not None
        assert closed.number == 1
        assert client.find_by_title("Seminars - week of 2026-11-16") is None

    def test_listings_follow_the_pages(self, client: GitHubIssueClient, github: FakeGitHub) -> None:
        for number in range(230):
            github.add(f"issue {number}", labels=("digest",))
        for issue in github.issues:
            issue["created_at"] = "2026-11-01T12:00:00Z"
        found = client.find_by_label("digest")
        assert len(found) == 230
        assert [issue.number for issue in found[:2]] == [230, 229]
        assert [request.url.params["page"] for request in github.requests] == ["1", "2", "3"]

    def test_missing_optional_parts_of_an_issue(
        self, client: GitHubIssueClient, github: FakeGitHub
    ) -> None:
        github.add("bare", body=None, html_url=None)
        issue = client.find_by_title("bare")
        assert issue is not None
        assert (issue.body, issue.url) == ("", None)

    def test_long_bodies_are_cut(self, client: GitHubIssueClient, github: FakeGitHub) -> None:
        issue = client.open("long", "x" * (MAX_BODY_CHARS + 500))
        assert len(issue.body) == MAX_BODY_CHARS
        assert issue.body.endswith("(truncated)\n")


class TestLabels:
    def test_a_missing_label_is_created_once(
        self, client: GitHubIssueClient, github: FakeGitHub
    ) -> None:
        client.ensure_label(LABEL_SOURCE_BROKEN)
        assert github.labels == {
            "source-broken": {
                "name": "source-broken",
                "color": "d93f0b",
                "description": "A data source keeps failing",
            }
        }
        client.ensure_label(LABEL_SOURCE_BROKEN)
        assert [request.method for request in github.requests] == ["GET", "POST"]

    def test_an_existing_label_is_left_alone(
        self, client: GitHubIssueClient, github: FakeGitHub
    ) -> None:
        github.labels["digest"] = {"name": "digest", "color": "ffffff"}
        client.ensure_label(LABEL_DIGEST)
        assert github.labels["digest"]["color"] == "ffffff"
        assert [request.method for request in github.requests] == ["GET"]

    def test_a_label_created_by_somebody_else_in_between_is_fine(
        self, client: GitHubIssueClient, github: FakeGitHub
    ) -> None:
        real = github.__call__

        def racing(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(404, json={"message": "Not Found"})
            return real(request)

        github.labels["digest"] = {"name": "digest"}
        racer = GitHubIssueClient(REPOSITORY, TOKEN, transport=httpx.MockTransport(racing))
        racer.ensure_label(LABEL_DIGEST)

    def test_the_context_manager_releases_the_client(self, github: FakeGitHub) -> None:
        with GitHubIssueClient(REPOSITORY, TOKEN, transport=httpx.MockTransport(github)) as made:
            assert made.find_by_label("digest") == []
        with pytest.raises(RuntimeError):
            made.find_by_label("digest")
