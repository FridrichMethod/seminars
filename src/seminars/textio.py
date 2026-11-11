"""UTF-8 text file helpers shared by every module that touches the disk.

All project files are UTF-8 without BOM and use LF line endings, so reads and
writes go through these two functions instead of bare ``open`` calls.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Final

_BOM: Final = chr(0xFEFF)


def normalize_body(text: str) -> str:
    """Bring the text of a response body into its canonical form.

    Leading byte order marks are removed, CRLF and CR become LF and the text
    ends with a newline. This is the form of ``FetchedDocument.text`` and of
    every recorded fixture file, so parsers must accept LF-only input, which
    matters for ICS files (the standard asks for CRLF). The function is
    idempotent.
    """
    cleaned = text.lstrip(_BOM).replace("\r\n", "\n").replace("\r", "\n")
    return cleaned if cleaned.endswith("\n") else cleaned + "\n"


def read_text(path: Path) -> str:
    """Read ``path`` as UTF-8 without newline translation."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_text_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` as UTF-8 without newline translation, atomically.

    The content goes to a temporary file in the same directory first and is
    then moved into place, so a crash never leaves a half-written file behind.
    Missing parent directories are created.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with temp_path.open("x", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
