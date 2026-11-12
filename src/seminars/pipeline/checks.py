"""The output checks: nothing leaves a run that holds a credential or breaks the charset rule.

data/ is committed and public/ is deployed, so both are public. Two checks
stand between a run and a commit:

* the secret scan (``SecretScanner``, see ``seminars.pipeline.secret_scan``);
* the charset rule of data/ (``seminars.textrules``): no emoji, no
  decorative or invisible character, no control character.

They run twice. ``check_texts`` looks at the files the pipeline is about to
write, before they are written: a finding there means that none of them is
written. ``check_tree`` then looks at everything below data/ and public/ as
it is on disk, which covers what other parts wrote (the LLM cache, the
feeds, the site data).

Any finding makes the run fail with exit status 3, and nothing of that run
may be committed or deployed. A finding names a file, a line and a rule
and never the text that was found, so it can be printed and put into an
issue. ``sync_check_issue`` keeps one issue open while the check fails.

One kind of finding does not go away by itself: an entry of the LLM cache
that a newer rule of the scanner flags. The LLM layer checks an entry only
when it reads it again, and an entry for text that no run reads any more
(a page that changed or is gone) stays until it ages out, up to
``LLMConfig.cache_ttl_days`` later. ``advice`` says what to do then:
delete the file. The command line and the issue show it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from seminars.config import ProjectPaths
from seminars.contracts import IssueClient, SecretScanner
from seminars.models import SecretFinding
from seminars.textrules import ascii_printable, data_charset_problems, data_tree_problems

logger = logging.getLogger(__name__)

CHECK_ISSUE_TITLE: Final = "Output check failed: the last run was not published"
EXIT_OUTPUT_CHECK: Final = 3
"""Exit status of a command whose output check found something."""

MAX_LISTED: Final = 50

CACHE_FINDING_ADVICE: Final = (
    "A finding below data/llm_cache/ is an answer of the LLM that was cached before the "
    "checks knew its shape, for text that no run has read since, so no run replaces it: "
    "delete that file and commit (a run that reads the text again asks the LLM again)."
)
"""What to do about a finding in the LLM cache (see the module)."""

_CACHE_PREFIX: Final = ProjectPaths(Path()).llm_cache_dir.as_posix() + "/"


@dataclass(frozen=True)
class CheckResult:
    """Findings of the output checks; no finding means the output may be published."""

    secret_findings: tuple[SecretFinding, ...] = ()
    charset_findings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.secret_findings and not self.charset_findings

    def plus(self, other: CheckResult) -> CheckResult:
        """Both results in one, without repeats, in a stable order."""
        secrets = {(f.origin, f.line, f.rule): f for f in self.secret_findings}
        secrets.update({(f.origin, f.line, f.rule): f for f in other.secret_findings})
        charset = sorted({*self.charset_findings, *other.charset_findings})
        return CheckResult(tuple(secrets[key] for key in sorted(secrets)), tuple(charset))


def _relative(path: Path, root: Path) -> str:
    """``path`` as text relative to ``root`` when it lies below it; printable in any case."""
    try:
        return ascii_printable(path.relative_to(root).as_posix())
    except ValueError:
        return ascii_printable(path.as_posix())


def check_texts(files: Mapping[Path, str], scanner: SecretScanner, *, root: Path) -> CheckResult:
    """Check the text of files before they are written; paths are reported relative to ``root``.

    The charset rule is applied to the files below ``<root>/data``.
    """
    data_dir = ProjectPaths(root).data_dir
    secrets: list[SecretFinding] = []
    charset: list[str] = []
    for path, text in sorted(files.items()):
        name = _relative(path, root)
        secrets.extend(scanner.scan_text(text, origin=name))
        if path.is_relative_to(data_dir):
            charset.extend(f"{name}:{problem}" for problem in data_charset_problems(text))
    return CheckResult(tuple(secrets), tuple(charset))


def check_tree(paths: ProjectPaths, scanner: SecretScanner) -> CheckResult:
    """Check everything below data/ and public/ of ``paths`` as it is on disk.

    A directory that does not exist has nothing to check; it is not passed
    to the scanner, whose contract says nothing about missing paths.
    """
    present = [path for path in (paths.data_dir, paths.public_dir) if path.exists()]
    found = scanner.scan_paths(present)
    secrets = [
        finding.evolve(origin=_relative(Path(finding.origin), paths.root)) for finding in found
    ]
    return CheckResult(tuple(secrets), tuple(data_tree_problems(paths.data_dir)))


def describe(result: CheckResult) -> list[str]:
    """One printable line per finding, for the command line and the issue."""
    lines = [
        f"secret scan: {finding.origin}:{finding.line} (rule {finding.rule})"
        for finding in result.secret_findings
    ]
    return [
        *lines,
        *(f"charset: {ascii_printable(problem)}" for problem in result.charset_findings),
    ]


def advice(result: CheckResult) -> list[str]:
    """What to do about the findings beyond the general advice; for the command line and issue."""
    origins = [
        *(finding.origin for finding in result.secret_findings),
        *result.charset_findings,
    ]
    return [CACHE_FINDING_ADVICE] if any(o.startswith(_CACHE_PREFIX) for o in origins) else []


def check_issue_body(result: CheckResult) -> str:
    """Markdown body of the issue that reports a failed output check."""
    lines = describe(result)
    listed = [f"    {line}" for line in lines[:MAX_LISTED]]
    if len(lines) > MAX_LISTED:
        listed.append(f"    ... and {len(lines) - MAX_LISTED} more")
    text = [
        "The output check of the last run found something, so nothing of that run was "
        "committed or deployed. The published data is the one of the run before.",
        "",
        "Findings (file, line and rule; the text itself is never shown):",
        "",
        *listed,
        "",
        "What to do: a finding of the secret scan means that something that looks like a "
        "credential reached the output. Find out where it came from; if it is a real "
        "credential, replace it. A charset finding names the character that must not be "
        "stored. Run `seminars run --dry-run` to see whether the problem is still there.",
        "",
        *(line for text in advice(result) for line in (text, "")),
        "This issue is updated by every run and closed automatically when a run passes.",
    ]
    return "\n".join(text) + "\n"


def sync_check_issue(issues: IssueClient | None, result: CheckResult) -> None:
    """Open, update or close the issue about a failed output check. Never raises."""
    if issues is None:
        return
    try:
        existing = issues.find_by_title(CHECK_ISSUE_TITLE, state="open")
        if result.ok:
            if existing is not None:
                issues.close(existing.number, comment="The output check passes again.")
            return
        body = check_issue_body(result)
        if existing is None:
            issues.open(CHECK_ISSUE_TITLE, body)
        elif existing.body != body:
            issues.update(existing.number, body=body)
    except Exception as error:  # the API of GitHub is not allowed to change the result of a run
        logger.warning("the output check issue was not updated (%s)", type(error).__name__)
