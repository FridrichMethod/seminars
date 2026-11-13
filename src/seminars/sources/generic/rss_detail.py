"""Detail pages of the generic adapter ``rss``: the ``detail_page`` params and what a page gives.

``seminars.sources.generic.rss`` describes the keys of ``detail_page`` (its
module docstring), decides which detail pages are fetched
(``detail_targets``) and completes a draft with the ``Detail`` of its page.
This module only reads one fetched page into a ``Detail``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator
from selectolax.lexbor import LexborHTMLParser, LexborNode

from seminars.models import FetchedDocument, Model, SpeakerDraft
from seminars.sources.generic.common import (
    Pattern,
    Selector,
    inner_html,
    node_lines,
    node_text,
    parse_day,
    require_groups,
    speakers_after_label,
    text_lines,
)

# --- params ------------------------------------------------------------------------------


class ScheduleParams(Model):
    date: Selector
    item: Selector
    time: Selector | None = None
    headline: Selector | None = None
    description: Selector | None = None


class DetailPageParams(Model):
    fetch: Literal["never", "native_only"] = "never"
    max_pages: Annotated[int, Field(ge=1, le=20)] = 6
    root: Selector | None = None
    exclude: Selector | None = None
    end_attribute: str | None = None
    type: Selector | None = None
    title: Selector | None = None
    subheadline: Selector | None = None
    dek: Selector | None = None
    date_text: Selector | None = None
    time_text: Selector | None = None
    location: Selector | None = None
    audience: Selector | None = None
    body: Selector | None = None
    speaker_label: Pattern | None = None
    speaker_pattern: Pattern | None = None
    schedule: ScheduleParams | None = None

    @model_validator(mode="after")
    def _check_speaker(self) -> DetailPageParams:
        if self.speaker_pattern is not None and self.speaker_label is None:
            raise ValueError("speaker_pattern needs detail_page.speaker_label")
        if self.speaker_label is not None and self.body is None:
            raise ValueError("speaker_label needs detail_page.body")
        require_groups(self.speaker_pattern, "speaker_pattern", "speaker")
        groups = set(self.speaker_pattern.groupindex) if self.speaker_pattern else set()
        if unknown := sorted(groups - {"speaker", "affiliation"}):
            raise ValueError(f"speaker_pattern has unknown groups: {', '.join(unknown)}")
        return self


# --- a detail page -----------------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    day: date | None
    time: str | None
    headline: str | None
    description: str | None


@dataclass(frozen=True)
class Detail:
    texts: Mapping[str, str]  # type, title, subheadline, dek, location, date_text, time_text
    end: datetime | None = None
    body: str | None = None
    audience: tuple[str, ...] = ()
    sessions: tuple[Session, ...] = ()
    speakers: tuple[SpeakerDraft, ...] = ()  # named in the body, after speaker_label


def read_detail(params: DetailPageParams, document: FetchedDocument, reference: date) -> Detail:
    tree = LexborHTMLParser(document.text)
    root = tree.css_first(params.root) if params.root else tree.body
    if root is None:
        return Detail(texts={})
    hidden: set[int] = set()  # the nodes inside matches of exclude, collected in one pass
    for excluded in root.css(params.exclude) if params.exclude else []:
        if excluded.mem_id != root.mem_id and excluded.mem_id not in hidden:
            hidden.update(node.mem_id for node in excluded.traverse())

    def matches(selector: str | None) -> list[LexborNode]:
        found = root.css(selector) if selector else []
        return [node for node in found if node.mem_id not in hidden]

    texts = {}
    for name in ("type", "title", "subheadline", "dek", "location", "date_text", "time_text"):
        found = matches(getattr(params, name))
        value = node_lines(found[0]) if found else None
        if value:
            texts[name] = value
    body = matches(params.body)
    audience = tuple(text for node in matches(params.audience) if (text := node_text(node)))
    speakers: list[SpeakerDraft] = []
    if params.speaker_label is not None and body:
        lines = text_lines(body[0])
        speakers = speakers_after_label(lines, params.speaker_label, params.speaker_pattern)
    return Detail(
        texts=texts,
        end=unix_time(root.attributes.get(params.end_attribute) if params.end_attribute else None),
        body=inner_html(body[0]) if body else None,
        audience=audience,
        sessions=read_sessions(params.schedule, root, matches, reference),
        speakers=tuple(speakers),
    )


def unix_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value or ""), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def read_sessions(
    params: ScheduleParams | None, root: LexborNode, matches: Any, reference: date
) -> tuple[Session, ...]:
    """Sessions of a schedule, each with the date of the heading before it."""
    if params is None:
        return ()
    headings = {node.mem_id for node in matches(params.date)}
    items = {node.mem_id for node in matches(params.item)}
    sessions = []
    day: date | None = None
    for node in root.traverse():
        if node.mem_id in headings:
            day = parse_day(node_text(node), None, reference)
        elif node.mem_id in items:

            def text(selector: str | None, scope: LexborNode = node) -> str | None:
                return node_text(scope.css_first(selector)) if selector else None

            sessions.append(
                Session(day, text(params.time), text(params.headline), text(params.description))
            )
    return tuple(sessions)
