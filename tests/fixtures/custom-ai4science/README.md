# custom-ai4science

AI + Data for Science, the weekly seminar of the Stanford courses CS 292,
EE 292R, PSYCH 292R and STATS 282 (listed on the Marlowe site and by HAI),
recorded 2026-11-11 (Pacific time) through the polite client.

| File | URL | What it is |
|---|---|---|
| `schedule-html-75a6c8b4.html` | `https://aida4sci.stanford.edu/schedule.html` | the organizers' schedule of the academic year 2026-27: 30 Wednesdays from 2026-09-23 to 2027-06-02 |

Nothing was trimmed or replaced. The page has no e-mail address or telephone
number (the only `mailto:` is inside a script).

## Layout

- Quarto site (`<meta name="generator" content="quarto-1.11.9">`). No feed
  link, no JSON-LD, no script data. "Subscribe" is a weekly e-mail.
- The title block says `Speakers and topics <middle dot> Wednesdays
  4:30<en dash>5:30 pm <middle dot> CoDA E160`; the intro repeats it. No card
  has a time of its own.
- One `section.section-head` per quarter (`#fall-2026`, `#winter-2027`,
  `#spring-2027`), each with a `div.quarto-listing` of `div.talk-card`.
- The `id` of a card is its date (`2026-11-11`). `span.talk-date` has the
  date without a year (`November 11`).
- A card with a speaker: `div.talk-meta` with `span.talk-date` and
  `span.talk-room` (`CoDA E160`), then `h2.talk-speaker` (the name, linked to
  the speaker's own page), `div.talk-affiliation`, `div.talk-title`, and
  `div.talk-abstract` when there is one.
- A talk in another room has no `span.talk-room` but a `p.room-notice`:
  `Different room this week: <a>Packard 101</a>, not CoDA E160.` (2026-09-23
  and 2026-10-07).
- A talk without a title yet has `div.talk-title.talk-title-tba` with
  `Title and abstract to be announced` and no abstract (the 8 winter talks
  and 2027-05-12).
- An open date is `div.talk-card.talk-tba` with only `div.talk-held`:
  `<span class="talk-date">February 3</span> <span
  class="talk-tba-label">Speaker to be announced</span>` (11 cards).
- Cards have no page of their own. The Marlowe page
  (`https://marlowe-research.stanford.edu/events/ai4science/`, not recorded)
  links each of its cards to `schedule.html#<date>`.
- 19 cards are talks: 10 in autumn with title and abstract (2026-09-23 to
  2026-12-02; none on 2026-11-25), 8 in winter, 1 in spring. Speaker names
  contain non-ASCII letters (`<L with stroke>ukasz D<e with ogonek>bowski`).

## What the source entry reads from it

Checked offline with the `html_list` adapter and the entry in
`config/sources.d/stanford-extra.yaml`; a test of this source can pin these
values:

- one request, `https://aida4sci.stanford.edu/schedule.html`, and 19 drafts
  (the 11 open dates are skipped);
- 2026-11-11 16:30 to 17:30 (naive local time), `CoDA E160`, speaker
  `Alex Pentland` with affiliation `HAI, Stanford University`, title
  `Detecting coordinated behavior across multiple fully encrypted domains`,
  an abstract, no `url`;
- 2026-09-23 and 2026-10-07 in `Packard 101` (the room notice);
- 9 drafts without a title: the 8 winter talks (2027-01-06 to 2027-03-03)
  and 2027-05-12 (`Mohsen Bayati`), each with speaker and the default times.
