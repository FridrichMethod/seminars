"""Keeping meeting codes, e-mail addresses and phone numbers out of what the LLM layer caches.

data/llm_cache/ is committed to a public repository. Normalization takes
meeting credentials, e-mail addresses and telephone numbers out of every
text field of an event (``seminars.pipeline.privacy``), but it never sees
the cache, and an answer of the page extractor quotes the page: a talk
announcement often gives the Zoom passcode, the address of the host or a
phone number. The page extractor therefore runs the same scrubber over the
page text and over every text value of an answer before the answer is
cached, and checks every entry it reads (``seminars.sources.generic.llm_page``).

Normalization reads a text field before it scrubs it: it removes markup,
decodes HTML entities (``html.unescape``), cleans the characters
(``seminars.textrules.clean_text``: no-break spaces become spaces, soft
hyphens and zero width characters go) and collapses white space: a line
field (a title, a name) into one line, a block field (an abstract) in each
of its lines, without the empty ones. The text the model gets is not read
that way: a feed or a plain text document is passed on as it is, and a page
can show an entity. An address written ``jane.doe&#64;example.edu``, with a
zero width space in it or with a run of spaces around the at sign, or a
passcode on a line of its own after a few empty ones, gets past the
scrubber as it is, while normalization finds it later. So ``PrivacyFilter``

* scrubs text the way normalization reads a block field (``clean``, over
  ``readable``): entities decoded, up to ``MAX_DECODING_PASSES`` times for
  text that was escaped more than once, the characters cleaned, the white
  space of each line collapsed and empty lines dropped. Markup is not
  removed here, because normalization removes only tags with a known HTML
  name and "<x, y>" in a title about mathematics stays;
* tells whether a text still hides private data from the scrubber
  (``hides``): in any form normalization may read it in, as it is, decoded,
  with its tags taken out (``jane.doe<b></b>@example.edu``), and each of
  them also as one line. Such a text is never cached.

``project_scrubber`` returns the scrubber of normalization through its
contract key (``private_scrubber``, a ``PrivateTextScrubber``), as
``project_secret_filter`` does for the secret scanner: the LLM layer never
imports the pipeline. Everything else here uses the standard library and
``seminars.textrules`` only.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from seminars.contracts import PrivateTextScrubber, load_implementation
from seminars.textrules import clean_text

type Scrubber = Callable[[str], str]
"""Text in, the text without private data out; applied to its own result, it changes nothing."""

MAX_DECODING_PASSES: Final = 3
"""How often entities are decoded at most; text escaped more often is checked by ``hides``."""

_TAG: Final = re.compile(r"<[^<>]{0,512}>")
"""Anything that reads as a tag; normalization removes the ones with a known HTML name."""

_TAG_JOINTS: Final = ("", "\n")
"""What a removed tag leaves behind: nothing (an inline tag) or a line break (a block tag)."""


def readable(text: str) -> str:
    """``text`` as normalization reads a block field before it scrubs it.

    Entities are decoded, the characters cleaned, the white space of every
    line collapsed to single spaces and empty lines dropped. The time is
    linear in the length of ``text``.
    """
    current = text
    for _ in range(MAX_DECODING_PASSES):
        decoded = html.unescape(current) if "&" in current else current
        if decoded == current:
            break
        current = decoded
    lines = (" ".join(line.split()) for line in clean_text(current).split("\n"))
    return "\n".join(line for line in lines if line)


def _one_line(text: str) -> str:
    """``text`` with every run of white space, line breaks included, as one space."""
    return " ".join(text.split())


def _forms(text: str) -> frozenset[str]:
    """``text`` in the forms normalization may read it in.

    As it is, decoded, without tags, and each of them also as one line (a
    line field collapses line breaks too).
    """
    forms = {text, readable(text)}
    for form in tuple(forms):
        if "<" in form:
            forms.update(readable(_TAG.sub(joint, form)) for joint in _TAG_JOINTS)
    return frozenset(forms | {_one_line(form) for form in forms})


@dataclass(frozen=True)
class PrivacyFilter:
    """The scrubber of normalization, applied to text the way normalization reads it."""

    scrub: Scrubber

    def clean(self, text: str) -> str:
        """``text`` read as normalization reads it, without private data."""
        return self.scrub(readable(text))

    def hides(self, text: str) -> bool:
        """Whether some form of ``text`` that normalization may read shows private data."""
        return any(self.scrub(form) != form for form in _forms(text))


def project_scrubber() -> Scrubber:
    """The scrubber normalization applies to every text field of an event."""
    scrub: PrivateTextScrubber = load_implementation("private_scrubber")
    return scrub


def project_privacy_filter() -> PrivacyFilter:
    """The filter over ``project_scrubber``."""
    return PrivacyFilter(project_scrubber())
