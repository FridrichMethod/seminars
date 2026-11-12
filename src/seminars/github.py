"""GitHub issues over the REST API: the ``IssueClient`` of health tracking and the digest.

``build_issue_client(env)`` returns a client for the repository named in
``GITHUB_REPOSITORY`` (``owner/name``) that authenticates with
``GITHUB_TOKEN``, or ``None`` when either is missing, which is the normal
case outside GitHub Actions. Nothing else is read from the environment;
``repository_owner`` gives the login the digest mentions.

The client talks to ``https://api.github.com`` only. It ignores proxy
settings and ``.netrc`` (``trust_env=False``), follows no redirects, retries
a request that failed on the way or was answered with 502, 503 or 504, and
turns everything else into ``GitHubError``. The token is sent in the
``Authorization`` header and appears nowhere else: not in an error, not in
a log line, not in ``repr``. What the API answers is data from outside;
the text of an error answer is escaped and cut before it becomes part of a
message.

Tests pass an ``httpx.MockTransport``; the suite never reaches the network.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from types import TracebackType
from typing import Any, Final, Self
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from seminars.models import Issue, IssueLabel, IssueStateFilter
from seminars.textrules import ascii_printable

API_URL: Final = "https://api.github.com"
API_VERSION: Final = "2022-11-28"
USER_AGENT: Final = "seminars-bot"
ENV_TOKEN: Final = "GITHUB_TOKEN"
ENV_REPOSITORY: Final = "GITHUB_REPOSITORY"
ENV_REPOSITORY_OWNER: Final = "GITHUB_REPOSITORY_OWNER"

PER_PAGE: Final = 100
MAX_PAGES: Final = 30
"""Listing stops after this many pages (3000 issues); older issues are not looked at."""

MAX_BODY_CHARS: Final = 60000
"""GitHub refuses bodies above 65536 characters; longer text is cut."""

TRUNCATION_NOTE: Final = "\n\n(truncated)\n"
TIMEOUT_SECONDS: Final = 30.0
ATTEMPTS: Final = 3
RETRY_PAUSE_SECONDS: Final = 2.0
RETRY_STATUSES: Final = frozenset({502, 503, 504})
MAX_ERROR_CHARS: Final = 200

_REPOSITORY: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+")
_LOGIN: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


class GitHubError(Exception):
    """A request to the GitHub API failed. ``status`` is the HTTP status, if there was one."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(ascii_printable(message)[: 2 * MAX_ERROR_CHARS])


def repository_owner(env: Mapping[str, str]) -> str | None:
    """The login that owns the repository: GITHUB_REPOSITORY_OWNER, else from GITHUB_REPOSITORY.

    ``None`` when neither is set or the value is not a GitHub login; the
    result is put into an issue as a mention, so it is checked first.
    """
    owner = env.get(ENV_REPOSITORY_OWNER, "").strip()
    owner = owner or env.get(ENV_REPOSITORY, "").strip().partition("/")[0]
    return owner if _LOGIN.fullmatch(owner) else None


def _bounded(body: str) -> str:
    if len(body) <= MAX_BODY_CHARS:
        return body
    return body[: MAX_BODY_CHARS - len(TRUNCATION_NOTE)] + TRUNCATION_NOTE


class GitHubIssueClient:
    """``IssueClient`` for one repository. Use ``build_issue_client`` to make one."""

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        api_url: str = API_URL,
    ) -> None:
        if _REPOSITORY.fullmatch(repository) is None:
            raise GitHubError(f"{ENV_REPOSITORY} must have the form owner/name")
        if not token.strip():
            raise GitHubError(f"{ENV_TOKEN} is empty")
        self.repository = repository
        self._sleep = sleep
        self._labels: set[str] = set()
        self._client = httpx.Client(
            base_url=api_url,
            headers={
                "Authorization": f"Bearer {token.strip()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
            timeout=TIMEOUT_SECONDS,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"GitHubIssueClient(repository={self.repository!r})"

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def release(self) -> None:
        """Release the connections (``close`` is taken: it closes an issue)."""
        self._client.close()

    # --- requests --------------------------------------------------------------------------

    def _path(self, tail: str) -> str:
        return f"/repos/{self.repository}/{tail}"

    def _send(self, method: str, path: str, **options: Any) -> httpx.Response:
        """One request with retries; ``GitHubError`` when no answer could be obtained."""
        problem = "no attempt was made"
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = self._client.request(method, path, **options)
            except httpx.HTTPError as error:
                problem = f"request failed ({type(error).__name__})"
            else:
                if response.status_code not in RETRY_STATUSES:
                    return response
                problem = f"HTTP {response.status_code}"
            if attempt < ATTEMPTS:
                self._sleep(RETRY_PAUSE_SECONDS * attempt)
        raise GitHubError(f"{method} {path}: {problem} after {ATTEMPTS} attempts")

    def _request(
        self, method: str, tail: str, *, expected: Sequence[int] = (200,), **options: Any
    ) -> httpx.Response:
        path = self._path(tail)
        response = self._send(method, path, **options)
        if response.status_code not in expected:
            raise GitHubError(
                f"{method} {path}: HTTP {response.status_code}: {_error_text(response)}",
                status=response.status_code,
            )
        return response

    def _list(self, tail: str, params: Mapping[str, str]) -> list[dict[str, Any]]:
        """All items of a paginated listing, up to ``MAX_PAGES`` pages."""
        items: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            query = {**params, "per_page": str(PER_PAGE), "page": str(page)}
            data = _json(self._request("GET", tail, params=query))
            if not isinstance(data, list):
                raise GitHubError(f"GET {self._path(tail)}: the answer is not a list")
            items.extend(item for item in data if isinstance(item, dict))
            if len(data) < PER_PAGE:
                break
        return items

    def _issues(self, params: Mapping[str, str]) -> list[Issue]:
        """Issues (pull requests left out), newest first."""
        query = {**params, "sort": "created", "direction": "desc"}
        found = [_issue(item) for item in self._list("issues", query) if "pull_request" not in item]
        return sorted(found, key=lambda issue: (issue.created_at, issue.number), reverse=True)

    # --- IssueClient -----------------------------------------------------------------------

    def ensure_label(self, label: IssueLabel) -> None:
        if label.name in self._labels:
            return
        tail = f"labels/{quote(label.name, safe='')}"
        if self._request("GET", tail, expected=(200, 404)).status_code == 404:
            payload = {"name": label.name, "color": label.color, "description": label.description}
            # 422: somebody created the label in the meantime.
            self._request("POST", "labels", json=payload, expected=(201, 422))
        self._labels.add(label.name)

    def find_by_label(self, label: str, *, state: IssueStateFilter = "open") -> list[Issue]:
        return self._issues({"labels": label, "state": state})

    def find_by_title(self, title: str, *, state: IssueStateFilter = "all") -> Issue | None:
        return next(
            (issue for issue in self._issues({"state": state}) if issue.title == title), None
        )

    def open(self, title: str, body: str, *, labels: Sequence[str] = ()) -> Issue:
        payload = {"title": title, "body": _bounded(body), "labels": list(labels)}
        return _issue(_json(self._request("POST", "issues", json=payload, expected=(201,))))

    def update(self, number: int, *, title: str | None = None, body: str | None = None) -> Issue:
        changes: dict[str, str] = {}
        if title is not None:
            changes["title"] = title
        if body is not None:
            changes["body"] = _bounded(body)
        if not changes:
            return _issue(_json(self._request("GET", f"issues/{number}")))
        return _issue(_json(self._request("PATCH", f"issues/{number}", json=changes)))

    def close(self, number: int, *, comment: str | None = None) -> None:
        if comment is not None:
            payload = {"body": _bounded(comment)}
            self._request("POST", f"issues/{number}/comments", json=payload, expected=(201,))
        self._request("PATCH", f"issues/{number}", json={"state": "closed"})


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as error:
        raise GitHubError(
            "the answer of the API is not JSON", status=response.status_code
        ) from error


def _error_text(response: httpx.Response) -> str:
    """The ``message`` of an error answer, escaped and cut; never the whole body."""
    try:
        data = response.json()
    except ValueError:
        return "no details"
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, str) or not message:
        return "no details"
    return ascii_printable(message)[:MAX_ERROR_CHARS]


def _issue(data: Any) -> Issue:
    """The parts of an API issue object this project reads."""
    if not isinstance(data, dict):
        raise GitHubError("the answer of the API is not an issue")
    labels = data.get("labels") or []
    names = [item.get("name") for item in labels if isinstance(item, dict)]
    try:
        return Issue.model_validate(
            {
                "number": data.get("number"),
                "title": data.get("title") or "",
                "body": data.get("body") or "",
                "state": data.get("state"),
                "labels": [name for name in names if isinstance(name, str)],
                "created_at": data.get("created_at"),
                "url": data.get("html_url"),
            }
        )
    except ValidationError as error:
        raise GitHubError("the answer of the API is not an issue") from error


def build_issue_client(env: Mapping[str, str]) -> GitHubIssueClient | None:
    """A client for GITHUB_REPOSITORY using GITHUB_TOKEN; ``None`` when either is unset.

    Raises ``GitHubError`` when the repository name is not ``owner/name``.
    """
    token = env.get(ENV_TOKEN, "").strip()
    repository = env.get(ENV_REPOSITORY, "").strip()
    if not token or not repository:
        return None
    return GitHubIssueClient(repository, token)
