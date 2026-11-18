"""Meetings among the drafts: the type a title gives, and the window a meeting is kept in.

A meeting is a conference or a long workshop (``seminars.models.is_meeting_span``).
Calendars often type a symposium as a talk ("Lecture/Presentation/Talk", or
the default type of a department's source), so normalization looks at the
title too:

* an event of type ``seminar``, ``lecture`` or ``other`` becomes a
  ``conference`` when its title names one (``CONFERENCE_WORDS``: symposium,
  conference, summit, convening, congress, retreat) and a ``workshop`` when
  it names one (``WORKSHOP_WORDS``: workshop, boot camp, a summer, winter or
  spring school), but only when it is all day or lasts at least
  ``MEETING_MIN_TIMED_HOURS`` hours. "Symposium Lecture" at 4 pm stays a
  lecture. A colloquium, a defense or a type the source states as
  ``conference`` or ``workshop`` is never changed, and nothing is ever
  turned from a meeting into a talk;
* a conference or a workshop with times of day that lasts more than
  ``MAX_TIMED_LENGTH``, whose end the length rule of
  ``seminars.pipeline.times`` drops, keeps its days as an all-day event
  instead (``meeting_over_days``) when its hours are those of a meeting:
  its end's time of day lies at least ``MEETING_MIN_TIMED_HOURS`` hours
  after its start's, or both are midnight. A summer school from 9:00 on a
  Monday to 17:00 on the Friday of the next week runs over those twelve
  days, it is no talk of an hour on its first morning; the title rule
  applies to such an event as to one of a day. A weekly "Workshop" from
  noon to 1 pm that a source lists from its first to its last week is a
  series of talks, whatever its type, and an event of any other type or
  with other hours loses its end, as the length rule says;
* a source with ``meeting_horizon`` keeps its meetings that start up to
  ``meetings.days_ahead`` ahead (``Settings.meeting_window_at``); its talks,
  and every event of the other sources, keep the window of the run;
* an event counts as inside a window while it has not ended before the
  window starts: a workshop of three weeks that began a month ago is still
  kept, and an event without an end is judged by its start.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Final

from seminars.models import MEETING_MIN_TIMED_HOURS, EventType, SourceConfig, TimeWindow
from seminars.pipeline.text import fold
from seminars.pipeline.times import MAX_TIMED_LENGTH, EventTimes

CONFERENCE_WORDS: Final = (
    "symposium",
    "symposia",
    "symposiums",
    "conference",
    "conferences",
    "summit",
    "convening",
    "congress",
    "retreat",
)
WORKSHOP_WORDS: Final = (
    "workshop",
    "workshops",
    "boot camp",
    "bootcamp",
    "summer school",
    "winter school",
    "spring school",
)
UPGRADABLE_TYPES: Final[frozenset[str]] = frozenset({"seminar", "lecture", "other"})
MEETING_TYPES: Final[frozenset[str]] = frozenset({"conference", "workshop"})


def _pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(re.escape(word) for word in words) + r")\b")


_CONFERENCE: Final = _pattern(CONFERENCE_WORDS)
_WORKSHOP: Final = _pattern(WORKSHOP_WORDS)


def _long_enough(times: EventTimes) -> bool:
    if times.all_day:
        return True
    if times.end is None:
        return False
    return (times.end - times.start).total_seconds() >= MEETING_MIN_TIMED_HOURS * 3600


def meeting_type(event_type: EventType, title: str | None, times: EventTimes) -> EventType:
    """``event_type``, or the meeting type that ``title`` names (see the module docstring)."""
    if event_type not in UPGRADABLE_TYPES or not title or not _long_enough(times):
        return event_type
    words = fold(title)
    if _CONFERENCE.search(words):
        return "conference"
    if _WORKSHOP.search(words):
        return "workshop"
    return event_type


def has_meeting_hours(start: datetime, end: datetime) -> bool:
    """Whether a span over several days has the hours of a meeting on each of them.

    Its end's time of day lies at least ``MEETING_MIN_TIMED_HOURS`` hours
    after its start's (9:00 to 17:00), or both are midnight: whole days.
    The times of day are compared as the clock shows them.
    """
    first, last = start.time(), end.time()
    if first == last == time.min:
        return True
    gap = datetime.combine(date.min, last) - datetime.combine(date.min, first)
    return gap >= timedelta(hours=MEETING_MIN_TIMED_HOURS)


def meeting_over_days(
    event_type: EventType, title: str | None, days: EventTimes, *, start: datetime, end: datetime
) -> EventType | None:
    """The meeting type of a timed event of more than a week that is kept over its days.

    ``start`` and ``end`` are the times of day the event was listed with,
    and ``days`` the same event read as all day (its first to its last
    local day). The result is the type of the event, after the title rule
    of ``meeting_type``, when that is a conference or a workshop, the days
    span more than ``MAX_TIMED_LENGTH`` and the hours are those of a
    meeting (``has_meeting_hours``); otherwise ``None``, and the event
    keeps its times of day without an end. The days are counted by their
    dates: a week with the day the clocks go back lasts an hour more and is
    still a week.
    """
    if days.end is None or days.end.date() - days.start.date() <= MAX_TIMED_LENGTH:
        return None
    if not has_meeting_hours(start, end):
        return None
    typed = meeting_type(event_type, title, days)
    return typed if typed in MEETING_TYPES else None


def has_not_ended_before(start: datetime, end: datetime | None, moment: datetime) -> bool:
    """Whether an event with this start and (exclusive) end still runs at or after ``moment``."""
    return end > moment if end is not None else start >= moment


def in_window(
    times: EventTimes,
    *,
    meeting: bool,
    source: SourceConfig,
    window: TimeWindow,
    meeting_window: TimeWindow,
) -> bool:
    """Whether an event with ``times`` belongs to the run (see the module docstring)."""
    if not has_not_ended_before(times.start, times.end, window.start):
        return False
    if times.start < window.end:
        return True
    return source.meeting_horizon and meeting and times.start < meeting_window.end
