"""Locale files (locales/en.json, locales/zh-CN.json): format and Python loader.

The same two files serve the static site (loaded by JavaScript) and the
Python outputs (ICS calendar names, the weekly digest). Format:

* nested JSON objects whose leaves are strings; a message is addressed by its
  dotted path, for example ``digest.sections.picks``;
* placeholders are ``{name}`` with lower-case names; nothing else is special;
* both files have exactly the same keys, and each message has the same
  placeholders in both languages;
* ``en.json`` is pure ASCII; ``zh-CN.json`` may contain Chinese text but no
  emoji or decorative characters.

Namespaces:

* ``fields.<field id>``: display name of each of the seven fields;
* ``feeds.<feed id>.name`` / ``.description``: the nine ICS feeds. The
  English name is the calendar name inside the ICS file;
* ``digest.*``: every string of the weekly digest. ``digest.issue_title`` is
  used in English only as the GitHub issue title (it must stay stable so
  that a week is never posted twice); headings inside the body are bilingual,
  English first (see ``bilingual``);
* ``site.*``: everything else the web site shows.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from seminars.textio import read_text

LOCALES: Final = ("en", "zh-CN")
DEFAULT_LOCALE: Final = "en"
BILINGUAL_SEPARATOR: Final = " / "

_PLACEHOLDER: Final = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def flatten(tree: Mapping[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten nested locale objects into ``{dotted key: message}``."""
    messages: dict[str, str] = {}
    for name, value in tree.items():
        key = f"{prefix}{name}"
        if isinstance(value, Mapping):
            messages.update(flatten(value, f"{key}."))
        elif isinstance(value, str):
            messages[key] = value
        else:
            raise ValueError(f"locale key {key!r} must be a string or an object")
    return messages


def placeholders(message: str) -> set[str]:
    """Names of the ``{placeholders}`` in a message."""
    return set(_PLACEHOLDER.findall(message))


class Locale:
    """The messages of one language."""

    def __init__(self, lang: str, messages: Mapping[str, str]) -> None:
        self.lang = lang
        self._messages = dict(messages)

    @classmethod
    def load(cls, lang: str, locales_dir: Path) -> Locale:
        """Read ``<locales_dir>/<lang>.json``."""
        tree = json.loads(read_text(locales_dir / f"{lang}.json"))
        if not isinstance(tree, dict):
            raise ValueError(f"locale file for {lang!r} must contain a JSON object")
        return cls(lang, flatten(tree))

    def keys(self) -> list[str]:
        """All dotted keys, sorted."""
        return sorted(self._messages)

    def __contains__(self, key: object) -> bool:
        return key in self._messages

    def t(self, key: str, /, **params: object) -> str:
        """The message for ``key`` with its placeholders filled in.

        A missing key or a missing placeholder value raises ``KeyError``.
        """
        try:
            message = self._messages[key]
        except KeyError:
            raise KeyError(f"locale {self.lang!r} has no key {key!r}") from None

        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in params:
                raise KeyError(f"locale key {key!r} needs a value for {{{name}}}")
            return str(params[name])

        return _PLACEHOLDER.sub(substitute, message)


def load_locales(locales_dir: Path) -> dict[str, Locale]:
    """Load every language in LOCALES, keyed by language code."""
    return {lang: Locale.load(lang, locales_dir) for lang in LOCALES}


def bilingual(key: str, primary: Locale, secondary: Locale, /, **params: object) -> str:
    """Both languages of one message, primary first: ``Picks / <translation>``.

    When both languages have the same text it is given once.
    """
    first = primary.t(key, **params)
    second = secondary.t(key, **params)
    return first if first == second else f"{first}{BILINGUAL_SEPARATOR}{second}"
