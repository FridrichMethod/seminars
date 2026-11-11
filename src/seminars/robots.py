"""robots.txt parsing and matching (RFC 9309). Pure functions, no I/O.

Differences from ``urllib.robotparser`` that matter here: the longest
matching rule wins (not the first one), ``*`` and ``$`` are understood in
path patterns, and ``Crawl-delay`` accepts fractional seconds.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Final
from urllib.parse import unquote

WILDCARD_AGENT: Final = "*"
ROBOTS_PATH: Final = "/robots.txt"


@dataclass(frozen=True)
class RobotsRule:
    """One ``Allow`` or ``Disallow`` line."""

    allow: bool
    pattern: str  # percent-decoded path pattern, never empty

    def matches(self, path: str) -> bool:
        return _compile(self.pattern).match(path) is not None


@dataclass(frozen=True)
class RobotsPolicy:
    """The rules that apply to one crawler on one origin."""

    rules: tuple[RobotsRule, ...] = ()
    crawl_delay: float | None = None  # seconds between requests, when the site asks for it

    def allows(self, path: str) -> bool:
        """Whether ``path`` (path plus optional ``?query``) may be fetched.

        The longest matching pattern decides; ``Allow`` wins a tie; no match
        means allowed. /robots.txt itself is always allowed.
        """
        target = unquote(path) or "/"
        if target == ROBOTS_PATH:
            return True
        matching = [rule for rule in self.rules if rule.matches(target)]
        if not matching:
            return True
        return max(matching, key=lambda rule: (len(rule.pattern), rule.allow)).allow


ALLOW_ALL: Final = RobotsPolicy()
DISALLOW_ALL: Final = RobotsPolicy(rules=(RobotsRule(allow=False, pattern="/"),))


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""), re.DOTALL)


@dataclass
class _Group:
    agents: list[str]
    rules: list[RobotsRule]
    crawl_delay: float | None = None


def _parse_delay(value: str) -> float | None:
    try:
        delay = float(value)
    except ValueError:
        return None
    return delay if math.isfinite(delay) and delay >= 0 else None


def _parse_groups(text: str) -> list[_Group]:
    groups: list[_Group] = []
    current: _Group | None = None
    collecting_agents = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if current is None or not collecting_agents:
                current = _Group(agents=[], rules=[])
                groups.append(current)
            current.agents.append(value.lower())
            collecting_agents = True
        elif key in ("allow", "disallow", "crawl-delay"):
            collecting_agents = False
            if current is None:
                continue
            if key == "crawl-delay":
                if current.crawl_delay is None:
                    current.crawl_delay = _parse_delay(value)
            elif value:
                current.rules.append(RobotsRule(allow=key == "allow", pattern=unquote(value)))
    return groups


def parse_robots(text: str, agent_token: str) -> RobotsPolicy:
    """Build the policy for the crawler named ``agent_token`` (e.g. ``seminars-bot``).

    Groups naming the token exactly (case-insensitive) apply; when there is
    none, the ``*`` groups apply. Several applicable groups are merged. The
    ``Crawl-delay`` of the ``*`` groups is used as a fallback when the
    crawler's own group does not state one.
    """
    token = agent_token.lower()
    groups = _parse_groups(text)
    specific = [group for group in groups if token in group.agents]
    wildcard = [group for group in groups if WILDCARD_AGENT in group.agents]
    selected = specific or wildcard
    rules = tuple(rule for group in selected for rule in group.rules)
    delays = [
        group.crawl_delay for group in (*selected, *wildcard) if group.crawl_delay is not None
    ]
    return RobotsPolicy(rules=rules, crawl_delay=delays[0] if delays else None)
