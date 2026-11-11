# custom-mlsys-seminar

Stanford MLSys Seminar, recorded 2026-11-11 through the polite client. Nothing
was trimmed.

| File | URL | What it is |
|---|---|---|
| `index-2e532cd7.html` | `https://mlsys.stanford.edu/` | the home page (Jekyll): four talk lists with 88 rows, 87 of them talks |

## What was looked at and not recorded

`https://mlsys.stanford.edu/feed.xml` (the feed the page links with
`rel="alternate"`) is an Atom feed without a single entry: 561 bytes, only the
feed header, `updated` 2025-10-27. It is of no use, so the talks come from the
HTML.

## Layout

- Four `div.talk-list` blocks, each after an `h2`: `2025 "Hidden Layer"
  Series` (2 talks, newest first), `Systems for Machine Learning Limited
  Series` (7 rows, oldest first), `Foundation Models Limited Series` (13,
  oldest first), `Previous Talks` (66, newest first).
- One talk is `div.talk-list > div.talk`:
  - `div.talk-date`: `M 10/20/25`, `W 1/11/23`, `5/19/22`. An optional weekday
    letter, then month/day/two-digit year, with or without leading zeros.
    Empty for the one row that is not a talk (`Thanksgiving`).
  - `div.talk-presenter`: `Tim Shi (OpenAI)`; the older list has names only,
    several speakers as `Xinyu Hu and Olcay Cirit`.
  - `a.talk-title-link`: the title. Its `href` is the YouTube recording or
    livestream, not a page about the talk.
  - `details`: `summary` "Abstract", the abstract, then `Bio:` and a
    `Livestream Link`.
- No time of day and no room anywhere in a row. The times of the two limited
  series are written in the prose at the top of the page.
- The newest talk is from 2025-10-20; the page had no upcoming talk when it
  was recorded.
