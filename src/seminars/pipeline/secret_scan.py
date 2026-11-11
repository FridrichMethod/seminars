"""The secret scanner: finds credential-shaped text before it is published.

Everything below data/ and public/ is public. The scanner is the last check
between a run and a commit: ``seminars run`` scans what it is about to
write and what was written, and any finding ends the run with exit status
3 (``seminars.pipeline.checks``).

What counts as a credential:

* GitHub tokens (``ghp_``, ``gho_``, ``ghu_``, ``ghs_``, ``ghr_``,
  ``github_pat_``);
* Anthropic API keys and OAuth tokens (``sk-ant-``);
* OpenAI keys (``sk-`` followed by a long run of letters and digits,
  ``sk-proj-``, ``sk-svcacct-``, ``sk-admin-``);
* ``Bearer`` followed by a long token;
* JSON web tokens (three base64url parts, the first two starting ``eyJ``);
* the first line of a private key block;
* ``refresh_token``, ``access_token``, ``id_token`` or ``client_secret``
  with a value;
* the exact value of every credential the process was started with
  (``known_credentials``), whatever it looks like.

A finding names the place and the rule, never the text. The same rules
back ``redact``, which normalization applies to every text taken from a
page: a token that a page shows (a talk about web security, a pasted
command) is replaced before it is stored, so page content cannot stop the
daily run. What is left for the scan to find is a real leak, for example
in an answer of an LLM.

The patterns in this file are written so that they do not match
themselves, and tests build their samples by concatenation: the scanner
can be pointed at the repository.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path
from typing import Final

from seminars.config import PROVIDER_CREDENTIAL_ENV
from seminars.models import SecretFinding

REDACTED: Final = "[redacted]"
"""What ``redact`` puts in the place of a credential-shaped string."""

KNOWN_CREDENTIAL_RULE: Final = "known-credential"
MIN_KNOWN_LENGTH: Final = 8
"""Shorter values of credential variables are ignored: they would match ordinary text."""

ENV_GITHUB_TOKEN: Final = "GITHUB_TOKEN"

_ESCAPED: Final = r"(?<=\\[nrtbf])|(?<=\\u[0-9A-Fa-f]{4})"
_NOT_AFTER_WORD: Final = rf"(?:(?<![A-Za-z0-9_-])|{_ESCAPED})"
"""Start of a generic pattern: not inside a word, but right after an escape such as ``\\n``.

JSON and other serialized text write a line break or a no-break space as an
escape (backslash ``n``, backslash ``u00a0``), so a token that starts a line
of an abstract follows a letter or a digit in the file that holds it.
"""

RULES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    # Distinctive prefixes need no guard: they are found even when glued to a word.
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("github-token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai-key", re.compile(r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{16,}")),
    ("openai-key", re.compile(_NOT_AFTER_WORD + r"sk-[A-Za-z0-9]{20,}")),
    ("bearer-token", re.compile(rf"(?:\b|{_ESCAPED})(?i:bearer)\s+[A-Za-z0-9._~+/=-]{{20,}}")),
    (
        "jwt",
        re.compile(
            _NOT_AFTER_WORD + r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
        ),
    ),
    ("private-key", re.compile(r"-{5}BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-{5}")),
    (
        "token-keyword",
        re.compile(
            rf"(?:\b|{_ESCAPED})(?i:(?:refresh_token|access_token|id_token|client_secret)\b)"
            r"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{12,}"
        ),
    ),
)
"""Rule name and pattern, in the order they are applied."""


def known_credentials(env: Mapping[str, str]) -> list[str]:
    """The credentials the process holds: values of the provider variables and GITHUB_TOKEN.

    Values are stripped (a token read from a file can end in a newline),
    blank ones are left out and the result is sorted. Pass it to
    ``PatternSecretScanner.knowing``; never log it.
    """
    names = [name for name in PROVIDER_CREDENTIAL_ENV.values() if name is not None]
    values = {env.get(name, "").strip() for name in [*names, ENV_GITHUB_TOKEN]}
    return sorted(value for value in values if value)


class PatternSecretScanner:
    """``SecretScanner`` over the rules of this module plus exact known values."""

    def __init__(self, known: Collection[str] = ()) -> None:
        self._known = tuple(
            sorted({value.strip() for value in known if len(value.strip()) >= MIN_KNOWN_LENGTH})
        )

    def __repr__(self) -> str:
        return f"PatternSecretScanner(rules={len(RULES)}, known={len(self._known)})"

    def knowing(self, values: Iterable[str]) -> PatternSecretScanner:
        """A scanner that also reports the exact ``values`` (rule ``known-credential``)."""
        return PatternSecretScanner([*self._known, *values])

    def _spans(self, text: str) -> list[tuple[int, int, str]]:
        """Every match as ``(start, end, rule)``."""
        spans = [
            (match.start(), match.end(), rule)
            for rule, pattern in RULES
            for match in pattern.finditer(text)
        ]
        for value in self._known:
            start = text.find(value)
            while start != -1:
                spans.append((start, start + len(value), KNOWN_CREDENTIAL_RULE))
                start = text.find(value, start + len(value))
        return spans

    def has_secret(self, text: str) -> bool:
        """Whether ``text`` contains anything a scan would report."""
        return bool(self._spans(text))

    def scan_text(self, text: str, *, origin: str) -> list[SecretFinding]:
        places = {(text.count("\n", 0, start) + 1, rule) for start, _, rule in self._spans(text)}
        return [SecretFinding(origin=origin, line=line, rule=rule) for line, rule in sorted(places)]

    def scan_paths(self, paths: Iterable[Path]) -> list[SecretFinding]:
        """Scan files; directories are scanned recursively. A path that does not exist is skipped.

        Files are read as UTF-8 with undecodable bytes replaced, so a file
        that is not text is still searched. The origin of a finding is the
        path of its file as text.
        """
        findings: list[SecretFinding] = []
        for path in paths:
            files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
            for file in files:
                if not file.is_file():
                    continue
                text = file.read_bytes().decode("utf-8", errors="replace")
                findings.extend(self.scan_text(text, origin=str(file)))
        return findings

    def redact(self, text: str) -> str:
        """``text`` with every credential-shaped part replaced by ``REDACTED``.

        Overlapping matches are replaced as one. The result has no findings
        and the function is idempotent.
        """
        spans = sorted(self._spans(text))
        if not spans:
            return text
        pieces: list[str] = []
        position = 0
        for start, end, _ in spans:
            if start >= position:
                pieces.append(text[position:start])
                pieces.append(REDACTED)
            position = max(position, end)
        pieces.append(text[position:])
        return "".join(pieces)


def build_secret_scanner() -> PatternSecretScanner:
    """The scanner of the project (contract ``secret_scanner``)."""
    return PatternSecretScanner()
