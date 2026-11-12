"""One run of the pipeline: from the sources to the files below data/ and public/.

The stages, in order (``seminars.contracts`` has the same list from the
point of view of the contracts)::

    sources   fetch, parse, LLM fallback, normalize     seminars.pipeline.collect
              and the size of each listing against its history
    filter    EventFilter.filter                        rules of another package
    merge     adopt stored uids, de-duplicate           seminars.pipeline.dedup
    classify  FieldTagger, LLMFieldTagger               seminars.pipeline.tagging
    scope     EventFilter.drop_out_of_scope
    picks     PicksScorer.score
    persist   compare with the stored events            seminars.pipeline.diff
              (a source counts as healthy only when it was read in full and
              delivered an event in the window; ``healthy_sources``)
    health    update_health (history and issues)
    write     data files, then ICS feeds and site data  seminars.pipeline.store
    check     secret scan and charset rule              seminars.pipeline.checks

What a run guarantees:

* One source cannot fail or stall the run. A run fails only when every
  source failed (exit status 1) or an output check found something (exit
  status 3).
* No LLM is no reason for a run to fail. An error of the optional LLM
  parts (the tagger, its usage, the counters and the pruning of the
  cache) becomes a warning of the report, whatever kind of error it is.
* The time of the run is the ``now`` it is given, for every stage.
  ``timer`` only measures how long things take.
* The files the pipeline writes itself are checked before they are
  written. A finding means that none of them is written, apart from the
  two records of the run, which say what happened. Whatever else was
  written below data/ and public/ is checked on disk afterwards. After a
  finding nothing of the run may be committed or deployed: the report
  says ``ok: false``, lists the findings and says so in a warning, and
  the command line prints them and exits with status 3.
* The two records of the run (data/run_report.json, data/last_run.json)
  are checked too, before they are written. When they hold something the
  check finds (a warning, an error or an excluded title can quote a
  site or an LLM), that becomes a finding as well, and the records are
  written without any of that text: errors, warnings (but for
  ``LIVE_OVER_OFFLINE``), excluded events and the details of the LLM
  providers are left out.
* A live run never builds on fixture data. When data/last_run.json
  records an offline run, everything below data/ came from the recorded
  fixtures, so the live run starts from no stored events, no uid map and
  no health history, says so in a warning (``LIVE_OVER_OFFLINE``), and
  removes the files of that state (``state_files``) before it writes its
  own, even when the output check keeps it from writing them. The next
  live run builds on what this one stored.
* A dry run does all of this in a scratch directory that is thrown away:
  nothing below the project changes, no issue is touched, and the LLM
  works on a copy of its cache. Its report is returned, not stored.
"""

from __future__ import annotations

import logging
import math
import shutil
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from seminars.config import AppConfig, ProjectPaths
from seminars.contracts import IssueClient, LLMCache, PicksScorer, SecretScanner
from seminars.models import (
    Candidate,
    Event,
    ExcludedEvent,
    HealthFile,
    LastRun,
    LLMUsage,
    RunCounts,
    RunMode,
    RunReport,
    SourceConfig,
    SourceRunResult,
)
from seminars.pipeline.checks import (
    EXIT_OUTPUT_CHECK,
    CheckResult,
    check_texts,
    check_tree,
    sync_check_issue,
)
from seminars.pipeline.collect import Timer, collect_sources, compare_with_history
from seminars.pipeline.dedup import adopt_stored_uids, dedupe, separate_listings
from seminars.pipeline.diff import Reconciled, reconcile
from seminars.pipeline.ids import canonical_url
from seminars.pipeline.normalize import NormalizeContext
from seminars.pipeline.services import Services, redaction
from seminars.pipeline.store import (
    State,
    data_files,
    load_state,
    remove_files,
    report_files,
    state_files,
    write_files,
)
from seminars.pipeline.tagging import tag_candidates

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1

NOT_PUBLISHABLE = "output check failed: nothing of this run may be committed or deployed"
REPORT_WITHHELD = (
    "the run report held text that the output check found; errors, warnings, excluded "
    "events and LLM details were left out of it"
)
LIVE_OVER_OFFLINE = (
    "data/ held the result of an offline run, made from the recorded fixtures; this live run "
    "started from no stored events, uid map or health history"
)


class RunError(Exception):
    """A run cannot start: the message says why in plain ASCII."""


class SourceSelectionError(RunError):
    """``--source`` names a source that does not exist or is disabled."""


@dataclass(frozen=True)
class RunOptions:
    """How a run was asked for on the command line."""

    offline: bool = False
    dry_run: bool = False
    source_ids: tuple[str, ...] = ()

    @property
    def mode(self) -> RunMode:
        return "offline" if self.offline else "live"


@dataclass(frozen=True)
class _Gathered:
    results: tuple[SourceRunResult, ...]
    candidates: tuple[Candidate, ...]
    invalid: tuple[ExcludedEvent, ...]


@dataclass(frozen=True)
class _Processed:
    events: tuple[Event, ...]
    excluded: tuple[ExcludedEvent, ...]
    excluded_uids: frozenset[str]
    absorbed: Mapping[str, str]
    aliases: Mapping[str, str]
    merged: int
    warnings: tuple[str, ...]


def starting_state(stored: State, mode: RunMode) -> tuple[State, bool]:
    """The state a run of ``mode`` builds on, and whether it had to start from nothing.

    A live run over the result of an offline run starts from an empty state
    (see the module docstring); every other run uses what is stored.
    """
    if mode == "live" and stored.last_run is not None and stored.last_run.mode == "offline":
        return State(), True
    return stored, False


def select_sources(config: AppConfig, source_ids: Sequence[str] = ()) -> tuple[SourceConfig, ...]:
    """The sources of a run: every enabled one, or the enabled ones named in ``source_ids``."""
    enabled = {source.id: source for source in config.enabled_sources}
    if not source_ids:
        if not enabled:
            raise RunError("no source is configured and enabled (config/sources.d/*.yaml)")
        return tuple(enabled.values())
    known = {source.id for source in config.sources}
    unknown = sorted(set(source_ids) - known)
    if unknown:
        raise SourceSelectionError(f"unknown source id(s): {', '.join(unknown)}")
    disabled = sorted(set(source_ids) - set(enabled))
    if disabled:
        raise SourceSelectionError(f"disabled source(s): {', '.join(disabled)}")
    wanted = set(source_ids)
    return tuple(source for source in enabled.values() if source.id in wanted)


def already_done_today(
    last_run: LastRun | None, *, now: datetime, timezone: str, mode: RunMode
) -> bool:
    """Whether a run of the same mode already succeeded on the local day of ``now``."""
    if last_run is None or not last_run.ok or last_run.mode != mode:
        return False
    zone = ZoneInfo(timezone)
    return last_run.started_at.astimezone(zone).date() == now.astimezone(zone).date()


@contextmanager
def output_target(paths: ProjectPaths, *, dry_run: bool) -> Iterator[ProjectPaths]:
    """Where a run writes: the project itself, or a scratch tree for a dry run.

    The scratch tree starts with a copy of data/llm_cache, so a dry run
    uses the cached answers without changing the cache, and is removed
    when the block ends.
    """
    if not dry_run:
        yield paths
        return
    with tempfile.TemporaryDirectory(prefix="seminars-dry-run-") as directory:
        scratch = ProjectPaths(Path(directory))
        if paths.llm_cache_dir.is_dir():
            shutil.copytree(paths.llm_cache_dir, scratch.llm_cache_dir)
        yield scratch


def listing_urls(sources: Sequence[SourceConfig]) -> frozenset[str]:
    """The addresses the sources are fetched from; never the detail page of one talk."""
    addresses = [url for source in sources for url in source.all_urls]
    addresses += [source.homepage for source in sources if source.homepage]
    return frozenset(canonical_url(address) for address in addresses)


def _gather(
    config: AppConfig,
    services: Services,
    sources: Sequence[SourceConfig],
    state: State,
    now: datetime,
    timer: Timer,
) -> _Gathered:
    redact, is_secret = redaction(services.secret_scanner)
    context = NormalizeContext(
        settings=config.settings,
        now=now,
        window=config.settings.window_at(now),
        uid_map=state.uid_map,
        redact=redact,
        is_secret=is_secret,
    )
    outcomes = collect_sources(
        sources, services.http_for, context=context, extractor=services.page_extractor, timer=timer
    )
    # Before anything else looks at the uids: two talks of one source that
    # the uid map holds under one uid get a uid each.
    candidates = separate_listings([c for outcome in outcomes for c in outcome.candidates])
    history = state.health.sources
    return _Gathered(
        results=tuple(
            compare_with_history(outcome.result, history.get(outcome.result.source_id))
            for outcome in outcomes
        ),
        candidates=tuple(candidates),
        invalid=tuple(item for outcome in outcomes for item in outcome.invalid),
    )


def healthy_sources(results: Sequence[SourceRunResult]) -> frozenset[str]:
    """The sources whose missing events count: read in full, with an event in the window."""
    return frozenset(r.source_id for r in results if r.status == "ok" and r.read_in_full)


def partial_reads(results: Sequence[SourceRunResult]) -> list[str]:
    """One warning of the run report per source that delivered events but was not read in full."""
    return [
        f"source {result.source_id} was not read in full; the events it did not deliver are "
        "not counted as missing"
        for result in results
        if result.status != "failed" and not result.read_in_full
    ]


def _scored(event: Event, scorer: PicksScorer) -> Event:
    result = scorer.score(event)
    score = result.score if math.isfinite(result.score) else 0.0
    return event.evolve(picks_score=score, picks_reasons=result.reasons)


def _process(
    config: AppConfig, services: Services, gathered: _Gathered, state: State
) -> _Processed:
    """Filter, merge, classify, scope and score the candidates of all sources."""
    stored = {event.uid: event for event in state.events}
    filtered = services.event_filter.filter(gathered.candidates)
    listing = listing_urls(config.sources)
    adopted = adopt_stored_uids(
        filtered.kept, state.events, settings=config.settings.dedup, listing_urls=listing
    )
    seeded = [
        candidate.evolve(
            event=candidate.event.evolve(first_seen=stored[candidate.event.uid].first_seen)
        )
        if candidate.event.uid in stored
        else candidate
        for candidate in adopted
    ]
    deduped = dedupe(
        seeded,
        settings=config.settings.dedup,
        aggregators={source.id for source in config.sources if source.aggregator},
        listing_urls=listing,
    )
    renamed = {
        before.event.uid: after.event.uid
        for before, after in zip(filtered.kept, adopted, strict=True)
    }
    uid_of = {key: c.event.uid for c in gathered.candidates for key in c.event.source_ids}
    uid_of.update(deduped.aliases)

    def final(uid: str) -> str:
        current = renamed.get(uid, uid)
        return deduped.absorbed.get(current, current)

    tagged = tag_candidates(
        deduped.candidates,
        tagger=services.field_tagger,
        llm_tagger=services.llm_tagger,
        uncertain={final(uid) for uid in filtered.uncertain},
        previous=stored,
    )
    scoped = services.event_filter.drop_out_of_scope(tagged.events)
    left_out = [*filtered.excluded, *tagged.excluded, *scoped.excluded]
    return _Processed(
        events=tuple(_scored(event, services.picks_scorer) for event in scoped.kept),
        excluded=(*gathered.invalid, *left_out),
        excluded_uids=frozenset(
            uid_of[key] for item in left_out for key in item.source_ids if key in uid_of
        ),
        absorbed={**{uid: final(uid) for uid in renamed if final(uid) != uid}, **deduped.absorbed},
        aliases=deduped.aliases,
        merged=len(filtered.kept) - len(deduped.candidates),
        warnings=tagged.warnings,
    )


def _counts(
    gathered: _Gathered, processed: _Processed, reconciled: Reconciled, threshold: float
) -> RunCounts:
    statuses = [result.status for result in gathered.results]
    events = reconciled.events
    return RunCounts(
        sources=len(statuses),
        sources_ok=statuses.count("ok"),
        sources_empty=statuses.count("empty"),
        sources_failed=statuses.count("failed"),
        drafts=sum(result.drafts for result in gathered.results),
        excluded=len(processed.excluded),
        merged=processed.merged,
        events=len(events),
        added=reconciled.added,
        changed=reconciled.changed,
        missing=reconciled.missing,
        removed=reconciled.removed,
        cancelled=sum(event.status == "cancelled" for event in events),
        archived=len(reconciled.archived),
        picks=sum(event.is_pick(threshold) for event in events),
    )


def _llm_failure(what: str, error: Exception) -> str:
    """The warning about a failed call of the optional LLM parts; logs the type only."""
    logger.warning("%s failed (%s)", what, type(error).__name__)
    return f"{what} failed ({type(error).__name__})"


def _llm_usage(services: Services) -> tuple[LLMUsage, list[str]]:
    """What the LLM did in this run, and warnings when that could not be read."""
    if services.llm is None:
        return LLMUsage(enabled=False), []
    warnings: list[str] = []
    try:
        usage = services.llm.usage()
    except Exception as error:  # the optional LLM must not fail the run
        usage = LLMUsage(enabled=True)
        warnings.append(_llm_failure("reading the LLM usage", error))
    hits = 0
    if services.llm_cache is not None:
        try:
            hits = services.llm_cache.stats().hits
        except Exception as error:  # as above
            warnings.append(_llm_failure("reading the LLM cache counters", error))
    return usage.evolve(cache_hits=hits), warnings


def _prune(cache: LLMCache | None) -> list[str]:
    """Drop the stale entries of the LLM cache; a failure is a warning."""
    if cache is None:
        return []
    try:
        cache.prune()
    except Exception as error:  # the optional LLM must not fail the run
        return [_llm_failure("pruning the LLM cache", error)]
    return []


def _retarget(files: Mapping[Path, str], source: Path, target: Path) -> dict[Path, str]:
    return {target / path.relative_to(source): text for path, text in files.items()}


def _write_outputs(
    config: AppConfig,
    services: Services,
    target: ProjectPaths,
    events: Sequence[Event],
    health: HealthFile,
    report: RunReport,
) -> list[str]:
    """Write the ICS feeds and the site data; a failure becomes a warning, not a crash."""
    try:
        services.write_ics_feeds(events, target.public_feeds_dir, config=config)
        services.write_site_data(
            events,
            health,
            report.to_last_run(),
            target.public_dir,
            config=config,
            generated_at=report.started_at,
        )
    except Exception as error:  # reported in the run report; the run counts as failed
        logger.error("writing the feeds and the site data failed (%s)", type(error).__name__)
        return [f"writing the feeds and the site data failed ({type(error).__name__})"]
    return []


@dataclass(frozen=True)
class _Published:
    """What writing the outputs of a run gave."""

    checks: CheckResult
    problems: tuple[str, ...] = ()  # outputs that are missing: the run failed
    warnings: tuple[str, ...] = ()


def _publish(
    config: AppConfig,
    services: Services,
    target: ProjectPaths,
    reconciled: Reconciled,
    processed: _Processed,
    state: State,
    health: HealthFile,
    report: RunReport,
    *,
    fresh: bool,
) -> _Published:
    """Check and write the data files, write the outputs, check what is on disk.

    With ``fresh`` the files of the stored state are removed first (see
    ``starting_state``).
    """
    if fresh:
        remove_files(state_files(target))
    uid_map = state.uid_map.with_aliases(processed.aliases)
    planned = data_files(
        config.paths,
        events=reconciled.events,
        uid_map=uid_map,
        health=health,
        archived=reconciled.archived,
    )
    files = _retarget(planned, config.paths.root, target.root)
    before = check_texts(files, services.secret_scanner, root=target.root)
    if not before.ok:
        return _Published(
            before, ("the data files were not written: the output check found something",)
        )
    write_files(files)
    warnings = _prune(services.llm_cache)
    problems = _write_outputs(config, services, target, reconciled.events, health, report)
    checks = before.plus(check_tree(target, services.secret_scanner))
    return _Published(checks, tuple(problems), tuple(warnings))


def _excluded_order(item: ExcludedEvent) -> tuple[str, str, tuple[str, ...], str, str, str]:
    """Sort key of the excluded list: the same events give the same list in any order."""
    start = item.start.astimezone(UTC).isoformat() if item.start else ""
    return item.reason, start, tuple(item.source_ids), item.title, str(item.url), item.detail or ""


def _first_report(
    services: Services,
    options: RunOptions,
    gathered: _Gathered,
    processed: _Processed,
    reconciled: Reconciled,
    *,
    now: datetime,
    finished_at: datetime,
    fresh: bool,
) -> RunReport:
    """The report of a run before its outputs are written (what health is updated with)."""
    usage, llm_warnings = _llm_usage(services)
    started_fresh = [LIVE_OVER_OFFLINE] if fresh else []
    return RunReport(
        started_at=now,
        finished_at=finished_at,
        mode=options.mode,
        dry_run=options.dry_run,
        ok=any(result.status != "failed" for result in gathered.results),
        sources=list(gathered.results),
        excluded=sorted(processed.excluded, key=_excluded_order),
        counts=_counts(gathered, processed, reconciled, services.picks_scorer.threshold),
        llm=usage,
        warnings=[
            *started_fresh,
            *services.warnings,
            *partial_reads(gathered.results),
            *processed.warnings,
            *llm_warnings,
        ],
    )


def _with_checks(report: RunReport, checks: CheckResult) -> RunReport:
    """``report`` with the findings of ``checks``; a finding makes it not publishable."""
    if checks.ok:
        return report
    return report.evolve(
        ok=False,
        secret_findings=list(checks.secret_findings),
        charset_findings=list(checks.charset_findings),
        warnings=[*report.warnings, NOT_PUBLISHABLE],
    )


def _final_report(report: RunReport, published: _Published, finished_at: datetime) -> RunReport:
    """The report of a run after its outputs were written and checked."""
    written = report.evolve(
        finished_at=finished_at,
        ok=report.ok and not published.problems,
        warnings=[*report.warnings, *published.warnings, *published.problems],
    )
    return _with_checks(written, published.checks)


def _findings(report: RunReport) -> CheckResult:
    return CheckResult(tuple(report.secret_findings), tuple(report.charset_findings))


def _without_free_text(report: RunReport) -> RunReport:
    """``report`` without any text that may quote a site or an LLM (see the module docstring).

    Of the warnings only ``LIVE_OVER_OFFLINE`` stays, a text of this
    module that quotes nothing; ``REPORT_WITHHELD`` takes the place of the
    rest. ``NOT_PUBLISHABLE`` comes back with the findings (``_with_checks``).
    """
    providers = [
        provider.evolve(model=None, error=None, warnings=[]) for provider in report.llm.providers
    ]
    kept = [warning for warning in report.warnings if warning == LIVE_OVER_OFFLINE]
    return report.evolve(
        sources=[result.evolve(error=None, warnings=[]) for result in report.sources],
        excluded=[],
        llm=report.llm.evolve(provider=None, providers=providers),
        warnings=[*kept, REPORT_WITHHELD],
    )


def _store_report(report: RunReport, target: ProjectPaths, scanner: SecretScanner) -> RunReport:
    """Check and write the two records of the run; returns the report that was written."""
    files = report_files(target, report)
    found = check_texts(files, scanner, root=target.root)
    if found.ok:
        write_files(files)
        return report
    logger.error("the run report failed the output check; its free text is left out")
    reduced = _with_checks(_without_free_text(report), _findings(report).plus(found))
    write_files(report_files(target, reduced))
    return reduced


def run_pipeline(
    config: AppConfig,
    services: Services,
    *,
    now: datetime,
    options: RunOptions | None = None,
    target: ProjectPaths | None = None,
    timer: Timer = time.monotonic,
) -> RunReport:
    """Run the pipeline once and return its report (see the module docstring).

    ``target`` is where the run writes (``output_target``); it defaults to
    the project, which is only right for a run that is not a dry run.
    Raises ``RunError`` when the run cannot start and ``StoreError`` when
    the stored state is damaged.
    """
    options = options or RunOptions()
    started = timer()

    def elapsed() -> datetime:
        return now + timedelta(seconds=max(timer() - started, 0.0))

    sources = select_sources(config, options.source_ids)
    destination = target or config.paths
    state, fresh = starting_state(load_state(config.paths), options.mode)
    if fresh:
        logger.warning("data/ holds an offline run; the live run starts from no stored state")
    gathered = _gather(config, services, sources, state, now, timer)
    processed = _process(config, services, gathered, state)
    reconciled = reconcile(
        state.events,
        processed.events,
        now=now,
        window=config.settings.window_at(now),
        healthy_sources=healthy_sources(gathered.results),
        enabled_sources={source.id for source in config.enabled_sources},
        delete_after_missing_runs=config.settings.events.delete_after_missing_runs,
        excluded_uids=processed.excluded_uids,
        absorbed=processed.absorbed,
    )
    issues: IssueClient | None = None if options.dry_run else services.issues
    report = _first_report(
        services,
        options,
        gathered,
        processed,
        reconciled,
        now=now,
        finished_at=elapsed(),
        fresh=fresh,
    )
    health = services.update_health(state.health, report, issues, config=config)
    published = _publish(
        config, services, destination, reconciled, processed, state, health, report, fresh=fresh
    )
    final = _store_report(
        _final_report(report, published, elapsed()), destination, services.secret_scanner
    )
    sync_check_issue(issues, _findings(final))
    return final


def exit_status(report: RunReport) -> int:
    """The exit status of ``seminars run`` for ``report``: 0, 1, or 3 for a failed output check."""
    if report.secret_findings or report.charset_findings:
        return EXIT_OUTPUT_CHECK
    return EXIT_OK if report.ok else EXIT_FAILED
