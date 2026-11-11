# custom-theory-lunch

Stanford CS Theory Lunch, recorded 2026-11-11 through the polite client.

| File | URL | What it is |
|---|---|---|
| `theory-lunch-b7dbb193.html` | `https://theory.stanford.edu/theory_lunch/` | the page of the weekly talk series: 4 upcoming and 7 past talks of Fall 2026 |

## What was trimmed

One line was removed from the "Logistics" section: the paragraph that starts
with `Contact:` and gives the name and the e-mail address of the organiser.
It is not needed to read the talks. The response was 14252 bytes, the
fixture is 14076 bytes.

## Layout

- No feed, no JSON-LD. Small hand-written HTML with meaningful class names.
- Two lists, `ol.talks`, each after an `h2`: `Upcoming Talks (Fall 2026)`
  (oldest first) and `Past Talks (Fall 2026)` (newest first).
- One talk is `ol.talks > li`:
  - `p.talk-speaker`: `<b>Jesse Goodman</b> (UC Berkeley)`. The name is in
    `b`, the affiliation in parentheses after it. Two speakers are written
    inside one `b`: `Rahul Saha and Alan Li`.
  - `p.talk-title`: the title in `i`; `Title TBD` when it is not known yet.
  - `p.talk-meta`: `November 12, 2026 <middle dot> Gates 403`: the date with the
    year, a middle dot (U+00B7), the room. The room is in `b` when it is not
    the usual one.
  - `p.talk-abstract`: the abstract; missing for talks without a title.
- No time of day in a row and no link to a page about the talk. The time is
  in the "Logistics" section: `Thursdays, typically 12:00pm to 1:00pm. Lunch
  is served at 12:00pm; the talk starts at 12:30pm.`
- Talks of earlier quarters are in `archives/index.html` (not requested).
