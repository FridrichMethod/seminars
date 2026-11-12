"""Keeping credential-shaped text out of what the LLM layer caches.

data/llm_cache/ is committed, and it holds the answers of the model as they
came: normalization, which redacts credential-shaped text in every event,
never sees them. The secret scan that runs before anything is committed
covers the cache like the rest of data/. A page can show a token-shaped
string (a talk on web security, a pasted command line), and the page
extractor copies text from the page, which grounding then keeps. Cached, it
would end every run with exit status 3; nothing would be committed, so the
next run would ask again and write the same entry. What the LLM layer
caches is therefore checked first, and a value with a finding is never
cached.

``SecretFilter`` is that check. It runs the secret scanner of the project,
which ``project_secret_filter`` obtains through the contract
``secret_scanner`` (``seminars.contracts.load_implementation``), the way
every package reaches an implementation of another. A text shows a secret
when the scan finds anything in it as it is, or in the form JSON writes it:
the cache files are ASCII-only JSON, which writes line breaks and every
character outside ASCII as escapes, and the scan of data/ reads the files.
A finding is a yes or no here. Nothing of the text is logged or kept.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from seminars.contracts import SecretScanner, SecretScannerFactory, load_implementation
from seminars.models import dump_ascii_json

ORIGIN: Final = "llm answer"
"""The origin a scan of a value is given; a finding is only counted, never shown."""


@dataclass(frozen=True)
class SecretFilter:
    """Tells whether a value of an LLM answer may be cached; see the module docstring."""

    scanner: SecretScanner

    def _finds(self, *forms: str) -> bool:
        return any(self.scanner.scan_text(form, origin=ORIGIN) for form in forms)

    def shows(self, text: str) -> bool:
        """Whether ``text`` shows a secret, as it is or as JSON writes it."""
        return self._finds(text, json.dumps(text))

    def holds(self, value: Any) -> bool:
        """Whether plain JSON data shows a secret anywhere once it is written out.

        This looks at the whole document, as the cache writes it and as
        plain JSON text, so it also sees a match that runs across two values.
        """
        written = dump_ascii_json(value)
        return self._finds(written, json.dumps(value, ensure_ascii=False, sort_keys=True))


def project_secret_filter() -> SecretFilter:
    """The filter over the scanner the contract ``secret_scanner`` names.

    Raises ``ImportError`` (``ContractNotImplementedError``) when that
    scanner does not exist: without the scan, nothing may be cached.
    """
    factory: SecretScannerFactory = load_implementation("secret_scanner")
    return SecretFilter(factory())
