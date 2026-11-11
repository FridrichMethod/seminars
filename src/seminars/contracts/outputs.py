"""Contracts of the outputs: ICS feeds, site data, weekly digest, health and GitHub issues."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from seminars.config import AppConfig
from seminars.models import (
    DigestDocument,
    DigestOutcome,
    Event,
    HealthFile,
    Issue,
    IssueLabel,
    IssueStateFilter,
    LastRun,
    RunReport,
)


class IcsFeedWriter(Protocol):
    """``seminars.outputs.ics:write_ics_feeds``."""

    def __call__(self, events: Sequence[Event], out_dir: Path, *, config: AppConfig) -> list[Path]:
        """Write ``<out_dir>/<feed id>.ics`` for all nine FEED_IDS; return the paths.

        Output is a pure function of the arguments: the same events give the
        same bytes.

        Golden files of this writer. A feed has CRLF line endings (RFC 5545)
        and can contain non-ASCII text, and a file below tests/golden/ can
        hold neither: the charset gate (scripts/check_ascii.py) accepts only
        printable ASCII with LF line endings there, and .gitattributes turns
        CRLF into LF on commit anyway. So:

        * golden ``.ics`` files are stored with LF line endings;
        * a test replaces CRLF with LF in the produced output before it
          compares the output with a golden file, and asserts the CRLF line
          endings of the real output on their own;
        * cases with non-ASCII text (accented names, CJK, symbols) are built
          in the test code, written with escapes, and are never stored in a
          golden file.
        """
        ...


class IcsValidator(Protocol):
    """``seminars.outputs.ics:validate_ics`` (backs ``seminars validate``)."""

    def __call__(self, text: str) -> list[str]:
        """Problems found in one ICS document; an empty list means valid."""
        ...


class SiteDataWriter(Protocol):
    """``seminars.outputs.site_data:write_site_data``."""

    def __call__(
        self,
        events: Sequence[Event],
        health: HealthFile,
        last_run: LastRun | None,
        out_dir: Path,
        *,
        config: AppConfig,
        generated_at: datetime,
    ) -> list[Path]:
        """Write ``<out_dir>/events.json`` (PublicEventsPayload) and
        ``<out_dir>/status.json`` (PublicStatusPayload) with ``dump_json``; return the paths."""
        ...


class DigestRenderer(Protocol):
    """``seminars.outputs.digest:render_digest``."""

    def __call__(
        self,
        events: Sequence[Event],
        health: HealthFile,
        *,
        config: AppConfig,
        week_start: date,
        previous_issue_at: datetime | None,
        owner: str | None = None,
    ) -> DigestDocument:
        """Render the digest for the week starting on ``week_start`` (a Monday).

        Events first seen after ``previous_issue_at`` form the "new" section;
        ``None`` means this is the first issue. ``owner`` (a GitHub login) is
        mentioned in the body so that GitHub sends a notification.
        """
        ...


class DigestPublisher(Protocol):
    """``seminars.outputs.digest:publish_digest`` (backs ``seminars digest``)."""

    def __call__(
        self,
        events: Sequence[Event],
        health: HealthFile,
        issues: IssueClient | None,
        *,
        config: AppConfig,
        now: datetime,
        owner: str | None = None,
        dry_run: bool = False,
    ) -> DigestOutcome:
        """Open this week's digest issue and close the previous one.

        "This week" is the Monday-to-Sunday week that contains ``now`` in the
        configured time zone; its Monday is the ``week_start`` of the renderer.
        Idempotent per week: if an issue with this week's title exists,
        nothing is changed. The creation time of the previous digest issue
        becomes ``previous_issue_at`` of the renderer and ``owner`` is passed
        on to it. With ``dry_run`` (or without an issue client) nothing is
        written.
        """
        ...


class HealthUpdater(Protocol):
    """``seminars.outputs.health:update_health``."""

    def __call__(
        self,
        health: HealthFile,
        report: RunReport,
        issues: IssueClient | None,
        *,
        config: AppConfig,
    ) -> HealthFile:
        """Append the run to the history and open, update or close health issues."""
        ...


# --- GitHub issues -------------------------------------------------------------------------

LABEL_DIGEST: Final = IssueLabel(name="digest", color="0e8a16", description="Weekly seminar digest")
LABEL_SOURCE_BROKEN: Final = IssueLabel(
    name="source-broken", color="d93f0b", description="A data source keeps failing"
)
LABEL_LLM_AUTH: Final = IssueLabel(
    name="llm-auth", color="fbca04", description="An LLM provider needs a new credential"
)
ISSUE_LABELS: Final = (LABEL_DIGEST, LABEL_SOURCE_BROKEN, LABEL_LLM_AUTH)


@runtime_checkable
class IssueClient(Protocol):
    """The GitHub issue operations used by health tracking and the digest."""

    def ensure_label(self, label: IssueLabel) -> None:
        """Create the label if the repository does not have it yet."""
        ...

    def find_by_label(self, label: str, *, state: IssueStateFilter = "open") -> list[Issue]:
        """Issues carrying ``label``, newest first."""
        ...

    def find_by_title(self, title: str, *, state: IssueStateFilter = "all") -> Issue | None:
        """The newest issue whose title equals ``title`` exactly."""
        ...

    def open(self, title: str, body: str, *, labels: Sequence[str] = ()) -> Issue: ...

    def update(self, number: int, *, title: str | None = None, body: str | None = None) -> Issue:
        """Change title and/or body of an issue; ``None`` leaves a part unchanged."""
        ...

    def close(self, number: int, *, comment: str | None = None) -> None:
        """Close an issue, optionally leaving a final comment."""
        ...


class IssueClientFactory(Protocol):
    """``seminars.github:build_issue_client``."""

    def __call__(self, env: Mapping[str, str]) -> IssueClient | None:
        """A client for GITHUB_REPOSITORY using GITHUB_TOKEN; ``None`` when either is unset."""
        ...
