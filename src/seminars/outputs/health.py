"""Health tracking: the run history in data/health.json and the issues that follow from it.

``update_health`` does two things for every run:

1. It appends the outcome of each source and of each LLM provider of the
   run report to the history (at most ``HEALTH_HISTORY_LIMIT`` runs each).
2. With an issue client, it keeps one GitHub issue per broken source
   (label ``source-broken``) and per failing LLM provider (label
   ``llm-auth``) in step with that history: opened when the trouble has
   lasted long enough, updated by every further run, closed when it is
   over.

What a run of a source counts as: ``failed`` when it failed, ``empty`` when
normalization accepted none of its drafts, and ``ok`` otherwise. That is
counted before the time window is applied: a seminar page that works but
has no talk in the window (a series between terms) is healthy, not empty.
Drafts that normalization rejected do not count, so a source whose every
draft is broken is empty. A source whose entry says ``allow_empty: true``
(a list that is often empty between events, ``allows_empty``) is healthy
when it parsed no draft at all without failing: its adapter found the page
it knows and nothing on it. Its adapter is the one to tell a page that has
changed apart from a page without items, by failing (``ParseError``). The
run report keeps the status of the run itself (``ok`` only with an event
in the window), which is what the stored events of the source are judged
by.

When a source counts as broken (``settings.health``):

* its last ``source_failure_threshold`` runs all failed, or
* it was healthy at some time and its last ``source_empty_threshold``
  runs all came back empty, or
* its last runs, as many as the larger of the two thresholds, were all
  failed or empty and at least one of them failed, or
* its last ``source_failure_threshold`` runs all delivered events but none
  of them was read in full (``SourceRunResult.read_in_full``: pages that
  could not be fetched, a listing far smaller than usual). While that
  lasts, the stored events the source does not deliver are kept, so it
  needs a look as much as a failure does.

The issue is closed by the first run in which the source delivers events
again and was read in full, and also when the source is disabled or
removed from the configuration. A run that delivered events but was not
read in full neither closes an issue nor, before enough of them follow
each other, opens one. A listing that stays far smaller than usual is in
the end taken at its new size (``SourceRunResult.resized_from``, see
``seminars.pipeline.collect``): that run counts as read in full and
closes the issue with a comment of its own (``SOURCE_RESIZED``), which
says that the stored events the source does not list count as missing
from then on. Only the sources of the run are judged, so a run
limited with ``--source`` leaves the others alone.

An LLM provider is failing when it failed in ``llm_failure_threshold`` runs
in a row; runs in which it was not used are skipped, because a provider
further down the chain is only asked when the ones before it fail. The
issue says which repository secret to replace and with which command. It
is closed when the provider answers again; when it was not asked in a run
in which another provider of the chain answered (an earlier one works
again, so nothing shows whether this one would); and when it is no longer
in the provider chain of a run (its credential is gone, LLM_PROVIDERS
names other providers, or the run has no LLM at all). A run whose LLM was
on but whose chain is unknown (its usage could not be read) closes
nothing. A provider that fails again after its issue was closed gets a new
one at once: the failures before are still counted.

Issue bodies are plain ASCII markdown. The only text in them that this
project did not write is the error of the last run, which is escaped and
set as an indented code block, where markdown and mentions are inert.

Issues are best effort. When the GitHub API fails, the problem is logged
and the history is still returned: a GitHub outage must not fail the
daily run.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import datetime
from typing import Final, Literal

from seminars.config import PROVIDER_CREDENTIAL_ENV, AppConfig, HealthSettings
from seminars.contracts import LABEL_LLM_AUTH, LABEL_SOURCE_BROKEN, IssueClient
from seminars.models import (
    HEALTH_HISTORY_LIMIT,
    HealthFile,
    Issue,
    IssueLabel,
    LLMHealthEntry,
    LLMProviderHealth,
    LLMUsage,
    RunReport,
    SourceConfig,
    SourceHealth,
    SourceHealthEntry,
    SourceRunResult,
    SourceStatus,
)
from seminars.textrules import ascii_printable

logger = logging.getLogger(__name__)

SOURCE_ISSUE_PREFIX: Final = "Source broken: "
LLM_ISSUE_PREFIX: Final = "LLM provider failing: "
RECENT_RUNS: Final = 10
MAX_ERROR_CHARS: Final = 500
CREDENTIAL_SCRIPT: Final = "scripts/set-llm-credential.sh"
CREDENTIAL_DOCS: Final = "docs/LLM_CREDENTIALS.md"
SOURCE_DOCS: Final = "docs/ADDING_A_SOURCE.md"

SourceVerdict = Literal["ok", "failing", "empty", "incomplete", "unsettled"]
"""``unsettled``: not delivering in full, but not for long enough to call it broken."""

SOURCE_BACK: Final = "The source delivers events again."
SOURCE_COMPLETE_AGAIN: Final = "The source is read in full again."
SOURCE_GONE: Final = "The source was disabled or removed."
SOURCE_RESIZED: Final = (
    "The listing has stayed far smaller than usual for so long that its smaller size is now "
    "taken as the usual one. From this run on, the stored events it does not list count as "
    "missing, and they are deleted when they stay missing. If the smaller listing is not "
    "right, fix the source now."
)
INCOMPLETE_PHRASE: Final = "delivered events but was not read in full"

_KIND_HINTS: Final[dict[str, str]] = {
    "auth": "The credential is missing, invalid or expired. Replace it.",
    "quota": "The subscription allowance or the prepaid credit is used up.",
    "rate_limit": "The provider is throttling requests. This can pass on its own.",
    "timeout": "The provider did not answer in time. This can pass on its own.",
    "network": "The provider could not be reached or its command line tool did not start.",
    "invalid_output": "The provider answered, but not with usable data.",
    "other": "The provider failed for a reason it did not name.",
}


def source_issue_title(source_id: str) -> str:
    return f"{SOURCE_ISSUE_PREFIX}{source_id}"


def llm_issue_title(provider: str) -> str:
    return f"{LLM_ISSUE_PREFIX}{provider}"


# --- history -------------------------------------------------------------------------------


def allows_empty(source: SourceConfig) -> bool:
    """Whether a run of ``source`` that parsed nothing found a page that works.

    That is what ``params.allow_empty: true`` says (see the module docstring).
    """
    return source.params.get("allow_empty") is True


def health_status(result: SourceRunResult, *, allow_empty: bool = False) -> SourceStatus:
    """What one run of a source counts as for health (see the module docstring)."""
    if result.status == "empty" and result.drafts > result.invalid:
        return "ok"
    if result.status == "empty" and allow_empty and result.drafts == 0:
        return "ok"
    return result.status


def record_run(
    health: HealthFile, report: RunReport, *, allow_empty: Collection[str] = ()
) -> HealthFile:
    """``health`` with the sources and LLM providers of ``report`` appended to the history.

    ``allow_empty`` holds the ids of the sources for which ``allows_empty``
    is true.
    """
    for result in report.sources:
        entry = SourceHealthEntry(
            at=report.started_at,
            status=health_status(result, allow_empty=result.source_id in allow_empty),
            events=result.events,
            accepted=None if result.status == "failed" else result.accepted,
            skipped_pages=result.skipped_pages,
            shrank_from=result.shrank_from,
            resized_from=result.resized_from,
            error=result.error,
        )
        health = health.with_source_entry(result.source_id, entry)
    for provider in report.llm.providers:
        llm_entry = LLMHealthEntry(
            at=report.started_at,
            status=provider.status,
            calls=provider.calls,
            failure_kind=provider.failure_kind,
            error=provider.error,
        )
        health = health.with_llm_entry(provider.provider, llm_entry)
    return health


def partial_reads(history: SourceHealth) -> int:
    """Most recent consecutive runs that delivered events but were not read in full."""
    count = 0
    for entry in reversed(history.history):
        if entry.status != "ok" or entry.read_in_full:
            break
        count += 1
    return count


def source_verdict(history: SourceHealth, settings: HealthSettings) -> SourceVerdict:
    """What the history of a source says about it (see the module docstring)."""
    last = history.last
    if last is None or (last.status == "ok" and last.read_in_full):
        return "ok"
    if last.status == "ok":
        partial = partial_reads(history)
        return "incomplete" if partial >= settings.source_failure_threshold else "unsettled"
    bad = history.trailing("failed", "empty")
    failed = history.trailing("failed")
    empty = history.trailing("empty")
    if failed >= settings.source_failure_threshold:
        return "failing"
    if history.last_success_at is not None and empty >= settings.source_empty_threshold:
        return "empty"
    mixed = max(settings.source_failure_threshold, settings.source_empty_threshold)
    recent = history.history[-bad:]
    if bad >= mixed and any(entry.status == "failed" for entry in recent):
        return "failing"
    return "unsettled"


def llm_failures(history: LLMProviderHealth) -> int:
    """Consecutive runs in which the provider failed, runs without a call skipped."""
    count = 0
    for entry in reversed(history.history):
        if entry.status == "ok":
            break
        if entry.status == "failed":
            count += 1
    return count


def llm_recovered(history: LLMProviderHealth) -> bool:
    """Whether the most recent run that used the provider got an answer from it."""
    used = [entry for entry in history.history if entry.status != "unused"]
    return bool(used) and used[-1].status == "ok"


# --- issue bodies --------------------------------------------------------------------------


def _moment(moment: datetime | None) -> str:
    return "never" if moment is None else moment.strftime("%Y-%m-%d %H:%M UTC")


def _code_block(text: str | None) -> str:
    """One escaped line as an indented code block; nothing in it is markdown."""
    line = ascii_printable(text or "no error message")[:MAX_ERROR_CHARS]
    return f"    {line}"


def _source_runs(history: SourceHealth) -> list[str]:
    rows = ["| Run | Status | Events |", "|---|---|---|"]
    for entry in reversed(history.history[-RECENT_RUNS:]):
        rows.append(f"| {_moment(entry.at)} | {entry.status} | {entry.events} |")
    return rows


def source_issue_body(
    source: SourceConfig | None, source_id: str, history: SourceHealth, verdict: SourceVerdict
) -> str:
    """Markdown body of the ``source-broken`` issue of one source."""
    if verdict == "incomplete":
        runs, what = partial_reads(history), INCOMPLETE_PHRASE
    elif verdict == "failing":
        runs, what = history.trailing("failed", "empty"), "failed or delivered nothing"
    else:
        runs, what = history.trailing("failed", "empty"), "parsed no events"
    last = history.last
    lines = [
        f"The source `{source_id}` has {what} in its last {runs} runs.",
        "",
        f"- Last healthy run: {_moment(history.last_success_at)}",
    ]
    if verdict == "incomplete" and last is not None:
        lines += _shortfall(last)
    if source is not None:
        page = ascii_printable(source.homepage or source.all_urls[0])
        lines += [f"- Name: {ascii_printable(source.name)}", f"- Adapter: `{source.adapter}`"]
        lines += [f"- Page: <{page}>"]
    if last is not None and last.status == "failed":
        lines += ["", "Error of the last run:", "", _code_block(last.error)]
    lines += ["", "Recent runs, newest first:", "", *_source_runs(history)]
    ending = "is read in full again" if verdict == "incomplete" else "delivers events again"
    lines += [
        "",
        "What to check: open the page, compare it with the recorded fixture of the source "
        f"and adjust the entry in `config/sources.d/` or its adapter ({SOURCE_DOCS}). "
        "A source that no longer exists can be disabled with `enabled: false`.",
        "",
        f"This issue is updated by every run and closed automatically when the source {ending}.",
    ]
    return "\n".join(lines) + "\n"


def _shortfall(last: SourceHealthEntry) -> list[str]:
    """What the last run of a source that was not read in full missed."""
    lines = []
    if last.skipped_pages:
        lines.append(f"- Last run: {last.skipped_pages} page(s) could not be fetched")
    if last.shrank_from is not None:
        lines.append(
            f"- Last run: {last.accepted or 0} usable draft(s) where it usually has "
            f"{last.shrank_from}"
        )
    lines += [
        "",
        "While the source is not read in full, the events it does not deliver are kept: "
        "they are not counted as missing, and the events it lists are updated as usual.",
    ]
    if last.shrank_from is not None:
        lines += [
            "",
            "A listing that stays this small is in the end taken at its new size, once the "
            f"history of the source (its last {HEALTH_HISTORY_LIMIT} runs) no longer holds "
            "enough full reads from before. From then on the events it does not list count "
            "as missing, and this issue is closed with a comment that says so.",
        ]
    return lines


def _credential_lines(provider: str) -> list[str]:
    secret = next((name for key, name in PROVIDER_CREDENTIAL_ENV.items() if key == provider), None)
    if secret is None:
        return [
            f"- `{provider}` has no repository secret. It is only used on a local machine, "
            "with the login of its own command line tool.",
            f"- Check the local state: `{CREDENTIAL_SCRIPT} {provider}`",
        ]
    lines = [
        f"- Credential: repository secret `{secret}`",
        f"- Replace it: `{CREDENTIAL_SCRIPT} {provider}` (reads the value without showing it; "
        f"`--from-file PATH` takes it from a file), which runs `gh secret set {secret}`",
    ]
    if provider == "claude-cli":
        lines.insert(1, "- Create a new token first: `claude setup-token`")
    return lines


def llm_issue_body(provider: str, history: LLMProviderHealth) -> str:
    """Markdown body of the ``llm-auth`` issue of one provider."""
    failed = [entry for entry in history.history if entry.status == "failed"]
    last = failed[-1] if failed else None
    kind = (last.failure_kind if last else None) or "other"
    lines = [
        f"The LLM provider `{provider}` failed in {llm_failures(history)} runs in a row "
        f"(kind of the last failure: {kind}).",
        "",
        _KIND_HINTS.get(kind, _KIND_HINTS["other"]),
        "",
        f"- Last run with an answer: {_moment(history.last_success_at)}",
        f"- Last failure: {_moment(last.at if last else None)}",
        "",
        "Error of the last failure:",
        "",
        _code_block(last.error if last else None),
        "",
        "How to fix it:",
        "",
        *_credential_lines(provider),
        "- Then check: run the `update` workflow by hand with mode `llm-smoke`",
        f"- Details: {CREDENTIAL_DOCS}",
        "",
        "The pipeline goes on without this provider: it uses the next one of the chain, or, "
        "when none is left, the answers stored by earlier runs and the rules. This issue is "
        "updated by every run that asks the provider, and closed automatically when the "
        "provider answers again, when another provider of the chain answers instead, or when "
        "the provider leaves the chain.",
    ]
    return "\n".join(lines) + "\n"


# --- issues --------------------------------------------------------------------------------


def _open_issues(issues: IssueClient, label: IssueLabel, prefix: str) -> dict[str, Issue]:
    """Open issues with ``label`` whose title starts with ``prefix``, by title (newest wins)."""
    found = issues.find_by_label(label.name, state="open")
    return {issue.title: issue for issue in reversed(found) if issue.title.startswith(prefix)}


def _report(
    issues: IssueClient, label: IssueLabel, existing: Issue | None, title: str, body: str
) -> None:
    """Open the issue ``title`` or bring its body up to date."""
    if existing is None:
        issues.ensure_label(label)
        issues.open(title, body, labels=[label.name])
    elif existing.body != body:
        issues.update(existing.number, body=body)


def sync_source_issues(
    issues: IssueClient, health: HealthFile, report: RunReport, config: AppConfig
) -> None:
    """Open, update or close the ``source-broken`` issues after ``report`` was recorded."""
    existing = _open_issues(issues, LABEL_SOURCE_BROKEN, SOURCE_ISSUE_PREFIX)
    sources = {source.id: source for source in config.sources}
    for source_id in sorted({result.source_id for result in report.sources}):
        history = health.sources[source_id]
        verdict = source_verdict(history, config.settings.health)
        title = source_issue_title(source_id)
        if verdict in ("failing", "empty", "incomplete"):
            body = source_issue_body(sources.get(source_id), source_id, history, verdict)
            _report(issues, LABEL_SOURCE_BROKEN, existing.get(title), title, body)
        elif verdict == "ok" and title in existing:
            issues.close(existing[title].number, comment=_closing(existing[title], history))
    enabled = {source_issue_title(source.id) for source in config.enabled_sources}
    for title, issue in sorted(existing.items()):
        if title not in enabled:
            issues.close(issue.number, comment=SOURCE_GONE)


def _closing(issue: Issue, history: SourceHealth) -> str:
    """The comment that closes ``issue``, by the run that closes it and what it was opened for."""
    last = history.last
    if last is not None and last.resized_from is not None:
        return SOURCE_RESIZED
    return SOURCE_COMPLETE_AGAIN if INCOMPLETE_PHRASE in issue.body else SOURCE_BACK


LLM_ANSWERS_AGAIN: Final = "The provider answers again."
LLM_NOT_ASKED: Final = (
    "Another provider of the chain answered in this run, so this one was not asked. "
    "A new issue is opened when it fails again."
)
LLM_NOT_IN_CHAIN: Final = "The provider is no longer in the provider chain."


def sync_llm_issues(
    issues: IssueClient, health: HealthFile, usage: LLMUsage, settings: HealthSettings
) -> None:
    """Open, update or close the ``llm-auth`` issues after a run (see the module docstring)."""
    existing = _open_issues(issues, LABEL_LLM_AUTH, LLM_ISSUE_PREFIX)
    statuses = {result.provider: result.status for result in usage.providers}
    answered = "ok" in statuses.values()
    for provider, status in sorted(statuses.items()):
        history = health.llm[provider]
        title = llm_issue_title(provider)
        issue = existing.get(title)
        if status == "unused" and answered and issue is not None:
            issues.close(issue.number, comment=LLM_NOT_ASKED)
        elif llm_failures(history) >= settings.llm_failure_threshold:
            _report(issues, LABEL_LLM_AUTH, issue, title, llm_issue_body(provider, history))
        elif llm_recovered(history) and issue is not None:
            issues.close(issue.number, comment=LLM_ANSWERS_AGAIN)
    if usage.enabled and not usage.providers:
        return  # the chain of the run is not known
    chain = {llm_issue_title(provider) for provider in statuses}
    for title, issue in sorted(existing.items()):
        if title not in chain:
            issues.close(issue.number, comment=LLM_NOT_IN_CHAIN)


def update_health(
    health: HealthFile,
    report: RunReport,
    issues: IssueClient | None,
    *,
    config: AppConfig,
) -> HealthFile:
    """Append the run to the history and open, update or close health issues.

    Nothing is written to disk here; the caller stores the returned
    history. Without an issue client, and for a dry run, no issue is
    touched. A failure of the issue client is logged and does not change
    the result.
    """
    quiet = {source.id for source in config.sources if allows_empty(source)}
    updated = record_run(health, report, allow_empty=quiet)
    if issues is None or report.dry_run:
        return updated
    try:
        sync_source_issues(issues, updated, report, config)
    except Exception as error:  # the API of GitHub is not allowed to fail the run
        logger.warning("source health issues were not updated (%s)", type(error).__name__)
    try:
        sync_llm_issues(issues, updated, report.llm, config.settings.health)
    except Exception as error:  # as above
        logger.warning("LLM health issues were not updated (%s)", type(error).__name__)
    return updated
