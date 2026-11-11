"""Contracts of the secret scanner and the source format probe."""

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


class SourceProber(Protocol):
    """``seminars.sources.probe:probe_source`` (backs ``seminars add-source``)."""

    def __call__(self, http: HttpClient, url: str) -> ProbeResult:
        """Fetch ``url`` once, recognise its format cheaply and suggest a YAML entry."""
        ...
