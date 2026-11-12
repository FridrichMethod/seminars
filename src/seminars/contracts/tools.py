"""Contracts of the secret scanner, the private text scrubber and the source format probe."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

from seminars.httpbase import HttpClient
from seminars.models import (
    ProbeResult,
    SecretFinding,
)


@runtime_checkable
class SecretScanner(Protocol):
    """Finds credential-like strings before anything is written to data/ or public/."""

    def scan_text(self, text: str, *, origin: str) -> list[SecretFinding]: ...

    def scan_paths(self, paths: Iterable[Path]) -> list[SecretFinding]:
        """Scan files; directories are scanned recursively."""
        ...


class SecretScannerFactory(Protocol):
    """``seminars.pipeline.secret_scan:build_secret_scanner``."""

    def __call__(self) -> SecretScanner: ...


class PrivateTextScrubber(Protocol):
    """``seminars.pipeline.privacy:scrub_private``.

    Text in, the text without meeting credentials, e-mail addresses and
    telephone numbers out (each replaced by a marker such as ``[removed]``).
    Applied to its own result it changes nothing, and its time is linear in
    the length of the text. It scrubs the text as it is given; a caller that
    holds text which normalization has not read yet (entities, odd
    characters, runs of white space) prepares it first, the way
    ``seminars.llm.privacy_filter.readable`` does.
    """

    def __call__(self, text: str) -> str: ...


class SourceProber(Protocol):
    """``seminars.sources.probe:probe_source`` (backs ``seminars add-source``)."""

    def __call__(self, http: HttpClient, url: str) -> ProbeResult:
        """Fetch ``url`` once, recognise its format cheaply and suggest a YAML entry."""
        ...
