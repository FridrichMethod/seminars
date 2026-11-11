# custom-nlp-seminar

Stanford NLP Seminar, recorded 2026-11-11 through the polite client. Nothing
was trimmed.

| File | URL | What it is |
|---|---|---|
| `seminar-019d54b1.html` | `https://nlp.stanford.edu/seminar/` | the schedule page: 25 tables, one per quarter, newest first (Spring 2026 back to Summer 2017), 221 data rows |
| `seminar-details-greta-tuckute-2026-shtml-b376d2f2.html` | `https://nlp.stanford.edu/seminar/details/greta_tuckute_2026.shtml` | the details page of the last row of the newest table (May 28) |

## Layout of the schedule page

- No feed, no JSON-LD, no embedded JSON. Plain HTML tables.
- One `table.p` per quarter. The nearest preceding sibling `h2` that has text
  carries everything the rows lack: the quarter and the year
  (`NLP Seminar Schedule - Spring 2026`, written with an em dash), the weekday
  and the time (`Thursday 11:00 am -- 12:00 noon`) and the venue
  (`Venue: Zoom & Gates 287 (for in-person speakers)`). Older headings (table
  index 13 and later) have no time and no venue.
- The page holds stray empty `h2` elements between the sections; skip headings
  without text.
- Every data row has exactly three `td` cells: date, speaker, title. The first
  row of each table has `th` cells instead.
  - date: month name (full or abbreviated) and day, optional ordinal suffix,
    NO year: `April 2nd`, `Jan 22nd`, `Jul 13`. The suffix can be wrong
    (`April 9nd`). The cell is empty in rows such as `ACL 2017 break`.
  - speaker: plain text, in recent tables inside a link to the speaker's page.
  - title: the title followed by `(details)` or `(details, slides)`; the
    `details` link is the detail page. It is absolute in recent rows and
    relative (`details/lkaiser.shtml`) in old ones. Rows without a link are
    notes (`Semester Break`, `Cancelled`).
- The year in a heading can be wrong: the table between `Fall 2025` and
  `Fall 2024` is headed `Winter 2024`, and `Spring 2022` appears twice. Parse
  the newest sections only, and check that the date falls on the weekday the
  heading names.
- When this was recorded the newest section was Spring 2026 (last talk May 28),
  so the page had no talk after the recording date.

## Layout of a details page

Inside `div.container.content`: `h2` is the title; `b > a` the speaker (link to
the speaker's page); the `i` after it the affiliation; then two text lines,
`Date: 11:00am - 12:00 noon PT, Thursday, May 28th` (no year either; the file
name ends in `_2026`) and `Venue: Room 287, Gates Computer Science Building`;
`h3` "Abstract" and `h3` "Bio", each followed by one `p`.
