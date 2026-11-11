"""GitHub issue and weekly digest values shared by the issue client and its users."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from seminars.models.base import Model, NonEmptyStr, Timestamp

IssueState = Literal["open", "closed"]
IssueStateFilter = Literal["open", "closed", "all"]


class IssueLabel(Model):
    """A repository label that the code creates on demand."""

    name: NonEmptyStr
    color: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{6}$")]  # hex without "#"
    description: str = ""


class Issue(Model):
    """The parts of a GitHub issue this project reads."""

    number: Annotated[int, Field(ge=1)]
    title: str
    body: str = ""
    state: IssueState = "open"
    labels: list[str] = []
    created_at: Timestamp
    url: str | None = None


class DigestDocument(Model):
    """A rendered weekly digest: issue title plus markdown body."""

    week_start: date  # the Monday (local time) the digest is for
    title: NonEmptyStr
    body: str


class DigestOutcome(Model):
    """What publishing a digest did.

    ``created``: a new issue was opened; ``exists``: an issue with the same
    title was already there, nothing was changed; ``dry_run``: nothing was
    written, ``document`` holds what would have been posted.
    """

    action: Literal["created", "exists", "dry_run"]
    document: DigestDocument
    issue_number: int | None = None
    closed_issues: list[int] = []  # previous digest issues closed by this call
