"""The probe request: the cheapest request that shows whether a provider works.

The provider chain sends it once in a run that has sent no other request
(``ProviderChain.probe``), and ``seminars llm-smoke`` sends it to every
configured provider (``seminars.llm.resolve.probe_providers``). Its answer
is a closed vocabulary, so every backend may give it, the tag-only one too.
"""

from __future__ import annotations

from typing import Final

from seminars.models import Model

PROBE_SYSTEM: Final = 'This is a connection check. Answer with the JSON object {"ok": true}.'
PROBE_USER: Final = "ping"
PROBE_MAX_TOKENS: Final = 512
"""Room for the probe answer; models that think before they answer need more than the answer."""


class ProbeAnswer(Model):
    """The answer to the probe request."""

    ok: bool
