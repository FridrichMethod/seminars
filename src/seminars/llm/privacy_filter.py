"""Keeping meeting codes, e-mail addresses and phone numbers out of what the LLM layer caches.

data/llm_cache/ is committed to a public repository. Normalization takes
meeting credentials, e-mail addresses and telephone numbers out of every
text field of an event (``seminars.pipeline.privacy``), but it never sees
the cache, and an answer of the page extractor quotes the page: a talk
announcement often gives the Zoom passcode, the address of the host or a
phone number. The page extractor therefore runs the scrubber of
normalization over every text value of an answer before the answer is
cached, and over every entry it reads (``seminars.sources.generic.llm_page``),
so the cache and the events follow one set of rules.

``project_scrubber`` returns that scrubber,
``seminars.pipeline.privacy.scrub_private``. Packages normally reach an
implementation of another package through a contract key
(``seminars.contracts.load_implementation``, as ``project_secret_filter``
does for the secret scanner), but the contracts have no key for the
scrubber yet. So it is imported by name, and this module is the only place
of the LLM layer that imports the pipeline; with a key, the function body
becomes that lookup and nothing else changes.
"""

from __future__ import annotations

from collections.abc import Callable

from seminars.pipeline.privacy import scrub_private

type Scrubber = Callable[[str], str]
"""Text in, the text without private data out; applied to its own result, it changes nothing."""


def project_scrubber() -> Scrubber:
    """The scrubber normalization applies to every text field of an event."""
    return scrub_private
