"""The command line: ``seminars <command>`` (a Typer application).

Commands, as the workflows use them (``seminars.contracts`` lists them too)::

    seminars run [--offline] [--source ID ...] [--no-llm] [--llm PROVIDER]
                 [--dry-run] [--skip-if-done-today]
    seminars llm-smoke
    seminars sources list
    seminars sources check [ID ...]
    seminars add-source URL
    seminars validate
    seminars digest [--dry-run]
    seminars build-site

Exit status: 0 success (a skipped run included), 1 failure, 2 usage error,
3 an output check failed (the secret scan or the charset rule of data/):
nothing of that run may be committed or deployed.

Every command works on the project that contains the current directory
and reads the environment merged over the project's optional ``.env``
file: the LLM credentials, GITHUB_TOKEN, GITHUB_REPOSITORY and
GITHUB_REPOSITORY_OWNER. No value of it is ever printed.

What the commands print is printable ASCII written by this project. Text
that came from a site (titles, error details) appears only after
``seminars.textrules.ascii_printable``. The one exception is the markdown
of ``digest --dry-run``, which is printed as it would be posted (its
headings are bilingual), without control characters, and always encoded as
UTF-8, whatever encoding stdout has (a redirected stdout on Windows is
cp1252, for example, which cannot hold Chinese text).

``--offline`` serves every source from its recordings in tests/fixtures/
and takes the time of the run from them. It sends nothing over the
network: no GitHub issue, and no LLM unless ``--llm`` names a provider.
It writes into the project only while data/ holds no live run: once
data/last_run.json records a live run, an offline run works in a scratch
directory as ``--dry-run`` does and says so, because fixture events and a
time stamp from the recordings would replace the live state (the stored
events would count as missing, and the last change would move back). The
other way round, a live run that finds an offline run in
data/last_run.json starts from no stored state, so that no fixture event
is ever published, and prints a warning that says so.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Annotated, Final, NoReturn

import typer

from seminars.config import (
    ENV_LLM_PROVIDERS,
    AppConfig,
    ConfigError,
    find_project_root,
    load_config,
    parse_provider_list,
    read_env,
)
from seminars.contracts import (
    ContractNotImplementedError,
    DigestPublisher,
    IcsFeedWriter,
    IcsValidator,
    LLMProber,
    SiteDataWriter,
    SourceProber,
)
from seminars.github import repository_owner
from seminars.http import HttpError
from seminars.models import LLMProviderResult, RunReport, SourceRunResult
from seminars.pipeline import services as wiring
from seminars.pipeline.checks import (
    EXIT_OUTPUT_CHECK,
    CheckResult,
    advice,
    check_tree,
    describe,
)
from seminars.pipeline.clock import ClockError, current_time, run_time
from seminars.pipeline.collect import collect_sources
from seminars.pipeline.normalize import NormalizeContext
from seminars.pipeline.publish import assemble_site, validate_project
from seminars.pipeline.run import (
    RunError,
    RunOptions,
    SourceSelectionError,
    already_done_today,
    exit_status,
    output_target,
    run_pipeline,
    select_sources,
)
from seminars.pipeline.store import StoreError, load_state
from seminars.textrules import ascii_printable

logger = logging.getLogger(__name__)

OFFLINE_OVER_LIVE: Final = (
    "note: data/ holds a live run; this offline run works in a scratch directory"
    " and changes nothing"
)

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2

NOT_PUBLISHABLE: Final = "nothing of this run may be committed or deployed"
_CONTROL: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

app = typer.Typer(
    name="seminars",
    help="Collect academic seminars and publish calendar feeds, a site and a digest.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
sources_app = typer.Typer(
    help="Look at the configured sources.",
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
app.add_typer(sources_app, name="sources")


@dataclass(frozen=True)
class Project:
    """The configuration of the project and the environment of the command."""

    config: AppConfig
    env: Mapping[str, str]


# --- plumbing ------------------------------------------------------------------------------


def say(line: str = "") -> None:
    """Print one line of output (ASCII written by this project)."""
    typer.echo(line)


def say_utf8(text: str) -> None:
    """Print ``text`` and a line break as UTF-8 bytes, whatever the encoding of stdout."""
    typer.echo(text.encode("utf-8"))


def complain(line: str) -> None:
    """Print one line to stderr."""
    typer.echo(ascii_printable(line), err=True)


def stop(code: int, message: str | None = None) -> NoReturn:
    if message:
        complain(message)
    raise typer.Exit(code)


@contextmanager
def failures() -> Iterator[None]:
    """Turn the expected errors of a command into a message and exit status 1."""
    try:
        yield
    except (ConfigError, StoreError, ClockError, RunError, ContractNotImplementedError) as error:
        stop(EXIT_FAILED, f"error: {error}")


def open_project(env_overrides: Mapping[str, str] | None = None) -> Project:
    root = find_project_root()
    env = {**read_env(root), **(env_overrides or {})}
    return Project(config=load_config(root, env), env=env)


@app.callback()
def main(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Log progress, not only problems.")
    ] = False,
) -> None:
    """Collect academic seminars and publish calendar feeds, a site and a digest."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


# --- run -----------------------------------------------------------------------------------


def _source_line(result: SourceRunResult) -> str:
    detail = f"{result.error_kind}: {result.error}" if result.status == "failed" else ""
    counts = f"{result.events} events, {result.requests} requests, {result.duration_seconds:g}s"
    return ascii_printable(f"  {result.status:<6} {result.source_id}: {counts} {detail}".rstrip())


def report_lines(report: RunReport) -> list[str]:
    """What ``seminars run`` prints about ``report``."""
    counts = report.counts
    mode = f"{report.mode}{', dry run' if report.dry_run else ''}"
    lines = [
        f"run: {mode} at {report.started_at.isoformat()}",
        f"sources: {counts.sources_ok} ok, {counts.sources_empty} empty, "
        f"{counts.sources_failed} failed",
        *(_source_line(result) for result in report.sources if result.status != "ok"),
        f"events: {counts.events} stored, {counts.added} new, {counts.changed} changed, "
        f"{counts.missing} missing, {counts.removed} removed, {counts.archived} archived, "
        f"{counts.cancelled} cancelled",
        f"merged: {counts.merged}, excluded: {counts.excluded}, picks: {counts.picks}",
        _llm_line(report),
        *(ascii_printable(f"warning: {warning}") for warning in report.warnings),
    ]
    checks = CheckResult(tuple(report.secret_findings), tuple(report.charset_findings))
    if not checks.ok:
        lines.append(f"OUTPUT CHECK FAILED: {NOT_PUBLISHABLE}")
        lines += [f"  {line}" for line in describe(checks)]
        lines += [f"  note: {line}" for line in advice(checks)]
    lines.append(f"result: {_verdict(report)}")
    return lines


def _llm_line(report: RunReport) -> str:
    llm = report.llm
    if llm.stored_answers_only:
        return f"llm: no provider, {llm.cache_hits} stored answers used"
    if not llm.enabled:
        return "llm: off (rules only)"
    return ascii_printable(
        f"llm: {llm.provider or 'no answer'}, {llm.calls} calls, {llm.cache_hits} cached"
    )


def _verdict(report: RunReport) -> str:
    status = exit_status(report)
    if status == EXIT_OUTPUT_CHECK:
        return f"output check failed (exit status {status})"
    if status == EXIT_OK:
        return "ok"
    if report.sources and all(result.status == "failed" for result in report.sources):
        return "failed: every source failed"
    return "failed: see the warnings"


def _llm_env(llm: str | None, no_llm: bool) -> dict[str, str]:
    if llm is None:
        return {}
    if no_llm:
        raise typer.BadParameter("--llm and --no-llm exclude each other", param_hint="--llm")
    try:
        parse_provider_list(llm)
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="--llm") from error
    return {ENV_LLM_PROVIDERS: llm}


@app.command()
def run(
    offline: Annotated[
        bool, typer.Option("--offline", help="Read the recorded fixtures instead of the network.")
    ] = False,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", metavar="ID", help="Run only this source (repeatable)."),
    ] = None,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Classify by rules only.")] = False,
    llm: Annotated[
        str | None,
        typer.Option(
            "--llm", metavar="PROVIDER", help="Use this provider chain (comma separated)."
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Do everything in a scratch directory; change nothing."),
    ] = False,
    skip_if_done_today: Annotated[
        bool,
        typer.Option(
            "--skip-if-done-today", help="Exit at once when a run of this mode succeeded today."
        ),
    ] = False,
) -> None:
    """Fetch every source, update data/ and write the feeds and the site data."""
    overrides = _llm_env(llm, no_llm)
    with failures():
        project = open_project(overrides)
        config = project.config
        now = run_time(offline=offline, fixtures_dir=config.paths.fixtures_dir)
        options = RunOptions(offline=offline, dry_run=dry_run, source_ids=tuple(source or ()))
        if skip_if_done_today and _done_today(config, options, now):
            say(f"skipped: today's {options.mode} run already succeeded")
            return
        options = _protect_live_state(config, options)
        report = _run(project, options, now, use_llm=not no_llm and (not offline or bool(llm)))
    for line in report_lines(report):
        say(line)
    if exit_status(report) != EXIT_OK:
        raise typer.Exit(exit_status(report))


def _protect_live_state(config: AppConfig, options: RunOptions) -> RunOptions:
    """``options``, made a dry run when an offline run would replace the state of a live one."""
    if not options.offline or options.dry_run:
        return options
    last_run = load_state(config.paths).last_run
    if last_run is None or last_run.mode != "live":
        return options
    say(OFFLINE_OVER_LIVE)
    return replace(options, dry_run=True)


def _done_today(config: AppConfig, options: RunOptions, now: datetime) -> bool:
    last_run = load_state(config.paths).last_run
    return already_done_today(
        last_run, now=now, timezone=config.settings.timezone, mode=options.mode
    )


def _run(project: Project, options: RunOptions, now: datetime, *, use_llm: bool) -> RunReport:
    config = project.config
    try:
        select_sources(config, options.source_ids)
    except SourceSelectionError as error:
        raise typer.BadParameter(str(error), param_hint="--source") from error
    with output_target(config.paths, dry_run=options.dry_run) as target:
        services = wiring.build_services(
            config,
            project.env,
            now=now,
            offline=options.offline,
            use_llm=use_llm,
            with_issues=not (options.offline or options.dry_run),
            llm_cache_dir=target.llm_cache_dir if options.dry_run else None,
        )
        try:
            return run_pipeline(
                config,
                services,
                now=now,
                options=options,
                target=target,
                timer=time.monotonic,
            )
        finally:
            services.close()


# --- llm-smoke -----------------------------------------------------------------------------


def provider_line(result: LLMProviderResult) -> str:
    """One line of ``seminars llm-smoke`` about one provider.

    A failed provider ends with the kind of failure and its message, an
    unused one with its first warning: the reason the probe gives for it
    ("not configured: CLAUDE_CODE_OAUTH_TOKEN is not set"), which names a
    variable and never its value.
    """
    model = f" (model {result.model})" if result.model else ""
    if result.status == "failed":
        detail = f"{result.failure_kind or 'other'}: {result.error or 'no detail'}"
        return ascii_printable(f"{result.provider}: failed{model}: {detail}")
    if result.status == "unused" and result.warnings:
        return ascii_printable(f"{result.provider}: unused{model}: {result.warnings[0]}")
    return ascii_printable(f"{result.provider}: {result.status}{model}")


@app.command("llm-smoke")
def llm_smoke() -> None:
    """Send one cheap request to every configured LLM provider and print the outcome.

    Exit status 1 when a provider failed or none answered.
    """
    with failures():
        project = open_project()
        probe: LLMProber = wiring.load("llm_probe")
        results = probe(project.config.llm, project.env)
    for result in results:
        say(provider_line(result))
    if not results:
        say("no LLM provider is configured")
    if not any(result.status == "ok" for result in results) or any(
        result.status == "failed" for result in results
    ):
        raise typer.Exit(EXIT_FAILED)


# --- sources -------------------------------------------------------------------------------


@sources_app.command("list")
def sources_list() -> None:
    """List every configured source: id, group, adapter, state and address."""
    with failures():
        config = open_project().config
    for source in config.sources:
        state = "enabled" if source.enabled else "disabled"
        address = source.url or (source.urls[0] if source.urls else "")
        say(ascii_printable(f"{source.id}  {source.group}  {source.adapter}  {state}  {address}"))
    say(f"{len(config.enabled_sources)} of {len(config.sources)} sources enabled")


@sources_app.command("check")
def sources_check(
    source_ids: Annotated[
        list[str] | None, typer.Argument(metavar="[ID]...", help="Sources to check (all).")
    ] = None,
) -> None:
    """Fetch and parse sources live; print events, requests, time and errors of each.

    Nothing is written. Exit status 1 when a source failed.
    """
    with failures():
        project = open_project()
        config = project.config
        try:
            chosen = select_sources(config, source_ids or ())
        except SourceSelectionError as error:
            raise typer.BadParameter(str(error), param_hint="ID") from error
        now = current_time()
        redact, is_secret = wiring.redaction(wiring.build_scanner(project.env))
        context = NormalizeContext(
            settings=config.settings,
            now=now,
            window=config.settings.window_at(now),
            redact=redact,
            is_secret=is_secret,
        )
        with wiring.live_client(config, project.env) as client:
            outcomes = collect_sources(
                chosen,
                lambda source: client.ignoring_robots_on(source.ignore_robots),
                context=context,
                extractor=None,
                timer=time.monotonic,
            )
    results = [outcome.result for outcome in outcomes]
    for result in results:
        say(_source_line(result))
    if any(result.status == "failed" for result in results):
        raise typer.Exit(EXIT_FAILED)


@app.command("add-source")
def add_source(
    url: Annotated[str, typer.Argument(help="Address of an events page or feed.")],
) -> None:
    """Fetch URL once, recognise its format and print a suggested sources.d entry."""
    with failures():
        project = open_project()
        probe: SourceProber = wiring.load("source_probe")
        with wiring.live_client(project.config, project.env) as client:
            try:
                result = probe(client, url)
            except HttpError as error:
                stop(EXIT_FAILED, f"error: {error}")
    say(ascii_printable(f"format: {result.format} ({result.content_type or 'no content type'})"))
    for note in result.notes:
        say(ascii_printable(f"note: {note}"))
    say("suggested entry for config/sources.d/<group>.yaml:")
    for line in result.suggested_yaml.splitlines():
        say(ascii_printable(line))


# --- validate, digest, build-site ----------------------------------------------------------


def _optional_validator() -> IcsValidator | None:
    try:
        return wiring.load("ics_validator")
    except ContractNotImplementedError:
        return None


@app.command()
def validate() -> None:
    """Check data/ and public/: schemas, ICS feeds, secret scan and the charset rule.

    Exit status 3 when the secret scan or the charset rule finds something,
    1 for any other problem, a missing data/events.json, site data or feed
    included (run and build-site come first).
    """
    with failures():
        project = open_project()
        result = validate_project(
            project.config.paths,
            validator=_optional_validator(),
            scanner=wiring.build_scanner(project.env),
        )
    say(f"checked {len(result.checked)} files")
    for problem in result.problems:
        say(f"problem: {problem}")
    for line in describe(result.checks):
        say(line)
    for line in advice(result.checks):
        say(f"note: {line}")
    if not result.checks.ok:
        say(f"result: output check failed: {NOT_PUBLISHABLE}")
        raise typer.Exit(EXIT_OUTPUT_CHECK)
    if result.problems:
        say("result: failed")
        raise typer.Exit(EXIT_FAILED)
    say("result: ok")


def printable_markdown(text: str) -> str:
    """``text`` without control characters other than line breaks and tabs."""
    return _CONTROL.sub("", text)


@app.command()
def digest(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help=(
                "Print the issue title and the markdown (bilingual, UTF-8 whatever the"
                " terminal uses); open no issue."
            ),
        ),
    ] = False,
) -> None:
    """Open this week's digest issue (idempotent per week) and close the previous one.

    The dry run is the only output of this program that is not ASCII: the
    digest is bilingual, and it is written as UTF-8.
    """
    with failures():
        project = open_project()
        state = load_state(project.config.paths)
        publish: DigestPublisher = wiring.load("digest_publish")
        issues, warnings = (None, ()) if dry_run else wiring.build_issues(project.env)
        try:
            outcome = publish(
                state.events,
                state.health,
                issues,
                config=project.config,
                now=current_time(),
                owner=repository_owner(project.env),
                dry_run=dry_run,
                deadlines=state.deadlines,
            )
        finally:
            wiring.release_issues(issues)
    for warning in warnings:
        complain(f"warning: {warning}")
    if outcome.action == "dry_run":
        document = outcome.document
        say_utf8(printable_markdown(f"Issue title: {document.title}\n\n{document.body}").rstrip())
        return
    if outcome.action == "exists":
        say(f"digest: issue #{outcome.issue_number} of this week exists; nothing changed")
        return
    closed = ", ".join(f"#{number}" for number in outcome.closed_issues) or "none"
    say(f"digest: opened issue #{outcome.issue_number}; closed: {closed}")


@app.command("build-site")
def build_site() -> None:
    """Assemble public/ from site/, locales/ and the committed data.

    Exit status 3 when the secret scan finds something in the result, 1
    when a part of the site is missing.
    """
    with failures():
        project = open_project()
        config = project.config
        feeds: IcsFeedWriter = wiring.load("ics_feeds")
        site_data: SiteDataWriter = wiring.load("site_data")
        built = assemble_site(
            config, write_ics_feeds=feeds, write_site_data=site_data, fallback_time=current_time()
        )
        checks = check_tree(config.paths, wiring.build_scanner(project.env))
    say(f"public/: {len(built.files)} files")
    for part in built.missing:
        say(f"missing: {part}")
    for line in describe(checks):
        say(line)
    for line in advice(checks):
        say(f"note: {line}")
    if not checks.ok:
        say(f"result: output check failed: {NOT_PUBLISHABLE}")
        raise typer.Exit(EXIT_OUTPUT_CHECK)
    if built.missing:
        say("result: failed")
        raise typer.Exit(EXIT_FAILED)
    say("result: ok")
