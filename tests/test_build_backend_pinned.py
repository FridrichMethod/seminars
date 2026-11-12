"""The build backend runs at a version fixed in the repository, like every locked package.

uv.lock pins and hashes every dependency, and each of them comes as a
wheel, so installing them runs no code. Building the project itself (the
editable install of ``uv sync``) does: the backend of [build-system] and
what it imports. ``[tool.uv] build-constraint-dependencies`` fixes their
versions, and uv.lock records the same constraints, so ``uv sync --locked``
fails when the two disagree.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, Final

ROOT: Final = Path(__file__).resolve().parents[1]
PIN: Final = re.compile(r"([A-Za-z0-9._-]+)==([0-9][0-9A-Za-z.]*)")


def toml(name: str) -> dict[str, Any]:
    return tomllib.loads((ROOT / name).read_text(encoding="utf-8"))


def constraints() -> dict[str, str]:
    pins: dict[str, str] = {}
    for entry in toml("pyproject.toml")["tool"]["uv"]["build-constraint-dependencies"]:
        match = PIN.fullmatch(entry)
        assert match, f"not an exact pin: {entry!r}"
        pins[match.group(1).lower()] = match.group(2)
    return pins


def test_every_build_requirement_is_pinned_to_one_version() -> None:
    requires = toml("pyproject.toml")["build-system"]["requires"]
    names = {re.split(r"[<>=!~\[; ]", entry, maxsplit=1)[0].lower() for entry in requires}
    assert names == {"hatchling"}
    assert names <= set(constraints())


def test_what_the_backend_imports_is_pinned_as_well() -> None:
    # The dependencies of hatchling 1.33.0, from its metadata.
    expected = {"packaging", "pathspec", "pluggy", "tomlkit", "trove-classifiers"}
    assert set(constraints()) == {"hatchling", *expected}


def test_the_lock_file_records_the_same_constraints() -> None:
    recorded = toml("uv.lock")["manifest"]["build-constraints"]
    assert {entry["name"]: entry["specifier"] for entry in recorded} == {
        name: f"=={version}" for name, version in constraints().items()
    }
