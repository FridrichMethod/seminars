"""robots.txt parsing and matching (RFC 9309). Pure functions, no I/O.

Differences from ``urllib.robotparser`` that matter here: the longest
matching rule wins (not the first one), ``*`` and ``$`` are understood in
path patterns, and ``Crawl-delay`` accepts fractional seconds. A byte order
mark at the start of the file is ignored, so that it cannot hide the first
group (which would silently drop its ``Disallow`` and ``Crawl-delay`` lines).

The file and the paths it is matched against come from the site, so matching
must not be able to stall the process: a pattern is matched in time
proportional to the length of the path, however many ``*`` it has. It is
never turned into a regular expression, which can backtrack without bound.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final
from urllib.parse import unquote

WILDCARD_AGENT: Final = "*"
ROBOTS_PATH: Final = "/robots.txt"
_BOM: Final = chr(0xFEFF)


@dataclass(frozen=True)
class RobotsRule:
    """One ``Allow`` or ``Disallow`` line."""

    allow: bool
    pattern: str  # percent-decoded path pattern, never empty

    def matches(self, path: str) -> bool:
        """Whether the pattern matches ``path`` from its start.

        ``*`` stands for any run of characters, a ``$`` at the very end of
        the pattern for the end of the path; everything else is literal.
        Without the ``$`` the pattern only has to match a prefix.
        """
        anchored = self.pattern.endswith("$")
        first, *rest = (self.pattern[:-1] if anchored else self.pattern).split("*")
        if not path.startswith(first):
            return False
        position = len(first)
        if not rest:
            return not anchored or position == len(path)
        # Taking the leftmost place for every middle part leaves the most room for the rest.
        for part in rest[:-1]:
            found = path.find(part, position)
            if found < 0:
                return False
            position = found + len(part)
        last = rest[-1]
        if anchored:
            return len(path) - len(last) >= position and path.endswith(last)
        return path.find(last, position) >= 0


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
    for raw_line in text.removeprefix(_BOM).splitlines():
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
