"""The hand-over of stored uids does not depend on the order of the candidates.

``seminars.pipeline.dedup.adopt_stored_uids`` takes its steps over the
candidates in an order fixed by their values, so any order of one run's
candidates gives every candidate the same uid. The stored events and the
candidates are drawn from a few titles, speakers, starts and day keys of
one source, so that the steps compete for the same stored events.
"""

from __future__ import annotations

import random
from datetime import timedelta
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from seminars.models import Candidate, Event
from test_pipeline_dedup_adopt import START, WEEK, adopt, day_key, fresh, people, stored

TITLES = ("TBA", "Quantum codes", "RESCHEDULED: Quantum codes", "Lecture I: Random matrices")
SPEAKERS = ((), ("Ann Smith",), ("Ann Smith",), ("Bob Jones",))
STARTS = (START, START + timedelta(hours=1), START - timedelta(hours=4), START + WEEK)
WORDS = ("ann-smith", "quantum-codes", "t1600", "ann-smith:t1200", "ann-smith:t1600")


@st.composite
def talk(draw: st.DrawFn) -> dict[str, Any]:
    start = draw(st.sampled_from(STARTS))
    return {
        "title": draw(st.sampled_from(TITLES)),
        "speakers": people(*draw(st.sampled_from(SPEAKERS))),
        "start": start,
        "source_ids": [day_key(start, draw(st.sampled_from(WORDS)))],
    }


@st.composite
def runs(draw: st.DrawFn) -> tuple[list[Candidate], list[Event]]:
    earlier = [
        stored(f"s{n}", **values)
        for n, values in enumerate(draw(st.lists(talk(), min_size=1, max_size=3)))
    ]
    known = [event.uid for event in earlier]
    candidates: list[Candidate] = []
    for n, values in enumerate(draw(st.lists(talk(), min_size=2, max_size=4))):
        uid = draw(st.sampled_from([f"c{n}", *known]))
        candidates.append(fresh(uid, **values))
    uids = [candidate.event.uid for candidate in candidates]
    unique = [c for position, c in enumerate(candidates) if c.event.uid not in uids[:position]]
    return unique, earlier


@given(runs(), st.randoms(use_true_random=False))
@settings(max_examples=300, deadline=None)
def test_every_order_of_the_candidates_gives_them_the_same_uids(
    run: tuple[list[Candidate], list[Event]], shuffler: random.Random
) -> None:
    candidates, earlier = run
    expected = dict(zip((c.event.uid for c in candidates), adopt(candidates, earlier), strict=True))
    for order in (candidates[::-1], shuffler.sample(candidates, len(candidates))):
        found = dict(zip((c.event.uid for c in order), adopt(order, earlier), strict=True))
        assert found == expected
