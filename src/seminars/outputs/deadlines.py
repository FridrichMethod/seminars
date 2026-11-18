"""What a deadline says in a calendar and in the digest: the texts of the feed ``deadlines.ics``.

The deadlines (``seminars.models.Deadline``, data/deadlines.json) are the
milestones of the registry of meetings kept by hand. ``seminars.outputs.ics``
writes them as the feed ``deadlines.ics`` next to the event feeds; it is not
one of FEED_IDS, so neither the event feeds nor the download of selected
talks know it. This module makes the VEVENT of one deadline, and the
digest (``seminars.outputs.digest``) takes its words from here as well.

One VEVENT per deadline, all day on the date the page states:

* ``UID`` is ``<uid>@<uid_domain>``, as for an event; ``SEQUENCE`` is the
  counter of the deadline, and ``DTSTAMP`` and ``LAST-MODIFIED`` are its
  ``last_changed``, so the bytes depend on the deadline alone;
* ``DTSTART`` is the date (a DATE value) and ``DTEND`` the day after;
* ``SUMMARY`` is ``<edition name>: <label>``, followed by what the page
  states about the hour in brackets: the time and the zone as written,
  ``(23:59 AoE)``, a zone alone, ``(AoE)``, and for a milestone that comes
  at the latest on its day ``(by this day)`` or ``(by 23:59 PT)``;
* ``DESCRIPTION`` names the edition, its days and its place, the date
  with the time, the zone and the qualifier, the advice to check the
  organizer's page with the address of the page that states the date
  (``verified_url``, the page of the last check), and the day the facts
  were verified;
* ``URL`` is the organizer's page of the edition (``url``), which can be
  another page than the one that states the date;
* ``TRANSP:TRANSPARENT`` and ``X-MICROSOFT-CDO-BUSYSTATUS:FREE``: a deadline
  marks no day as busy. There is no alarm, which calendar programs ignore
  in a subscribed calendar anyway, and no location, since a deadline does
  not happen at the venue.

Every text of a deadline is printable ASCII on one line (the registry
requires it), and every label comes from the English locale (keys below
``deadlines.``), as the labels of the event feeds do.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, timedelta
from typing import Final

import icalendar

from seminars.i18n import Locale
from seminars.models import DEADLINES_FEED, Deadline

DEADLINES_FILE: Final = f"{DEADLINES_FEED}.ics"
"""The file name of the feed of the deadlines in the directory of the feeds."""

FEED_NAMES: Final = "deadlines.feed"
"""Locale prefix of the name and the description of the feed (``.name``, ``.description``)."""

EMPTY_YEAR: Final = 1970
"""Year of the time zone definition of a feed without any deadline."""

_ONE_DAY: Final = timedelta(days=1)


def clock(deadline: Deadline) -> str:
    """The time and the zone as the page writes them: ``23:59 AoE``, ``AoE``, or nothing."""
    return " ".join(part for part in (deadline.time, deadline.zone) if part)


def stated(deadline: Deadline, locale: Locale) -> str:
    """What the page states about the hour: ``23:59 AoE``, ``by this day AoE``; may be empty."""
    hour = clock(deadline)
    if deadline.qualifier != "by":
        return hour
    if deadline.time is not None:
        return locale.t("deadlines.when.by", when=hour)
    day = locale.t("deadlines.when.by_day")
    return f"{day} {deadline.zone}" if deadline.zone else day


def summary(deadline: Deadline, locale: Locale) -> str:
    """``<edition name>: <label>``, with what the page states about the hour in brackets."""
    head = f"{deadline.edition_short}: {deadline.label}"
    hour = stated(deadline, locale)
    return f"{head} ({hour})" if hour else head


def _day_line(deadline: Deadline, locale: Locale) -> str:
    """``<label>: 2027-02-26 23:59 AoE``, or ``<label>: by 2027-02-01`` for the qualifier by."""
    when = " ".join(part for part in (deadline.date.isoformat(), clock(deadline)) if part)
    if deadline.qualifier == "by":
        when = locale.t("deadlines.when.by", when=when)
    return f"{deadline.label}: {when}"


def _edition_line(deadline: Deadline, locale: Locale) -> str:
    start, end = deadline.edition_start.isoformat(), deadline.edition_end.isoformat()
    if start == end:
        return locale.t("deadlines.event.day", day=start, location=deadline.location)
    return locale.t("deadlines.event.days", start=start, end=end, location=deadline.location)


def description(deadline: Deadline, locale: Locale) -> str:
    """The edition and its days, the deadline, the advice to check the page, the check."""
    blocks = [
        [deadline.edition_title, _edition_line(deadline, locale)],
        [_day_line(deadline, locale)],
        [
            locale.t("deadlines.event.check", url=deadline.verified_url),
            locale.t("deadlines.event.verified", date=deadline.verified_on.isoformat()),
        ],
    ]
    return "\n\n".join("\n".join(block) for block in blocks)


def deadline_vevent(deadline: Deadline, *, uid_domain: str, locale: Locale) -> icalendar.Event:
    """The VEVENT of one deadline (see the module docstring)."""
    component = icalendar.Event()
    stamp = deadline.last_changed.astimezone(UTC)
    component.add("uid", f"{deadline.uid}@{uid_domain}")
    component.add("dtstamp", stamp)
    component.add("last-modified", stamp)
    component.add("sequence", deadline.sequence)
    component.add("dtstart", deadline.date)
    component.add("dtend", deadline.date + _ONE_DAY)
    component.add("summary", summary(deadline, locale))
    component.add("description", description(deadline, locale))
    component.add("url", deadline.url)
    component.add("transp", "TRANSPARENT")
    component.add("x-microsoft-cdo-busystatus", "FREE")
    return component


def deadline_years(deadlines: Sequence[Deadline]) -> tuple[int, int]:
    """The first and the last year of the dates of ``deadlines`` (``EMPTY_YEAR`` for none)."""
    years = [deadline.date.year for deadline in deadlines]
    return (min(years), max(years)) if years else (EMPTY_YEAR, EMPTY_YEAR)
