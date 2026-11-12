# custom-isl-colloquium

ISL Colloquium (Stanford Information Systems Laboratory), recorded 2026-11-11
(Pacific time) through the polite client.

| File | URL | What it is |
|---|---|---|
| `talks-3b771d84.html` | `https://isl.stanford.edu/talks/` | the home page of the colloquium: every talk since January 2020 as a hidden card, 152 cards |
| `talks-talk-schedule-fall-2026-thomas-cou-4db3e1c0.html` | `https://isl.stanford.edu/talks/talk-schedule/fall-2026-thomas-courtade` | the detail page of the talk of 2026-11-12; the server redirects to the same path with a trailing slash (`final_url` in the manifest) |

## What was replaced

- `talks-3b771d84.html`: the two `mailto:` addresses of the organizers in the
  "About the ISL Colloquium" box were replaced by `redacted@example.edu`
  (personal addresses). The names stay. Nothing else was changed; the body
  is two bytes longer than the response.

## Layout

- Static Astro site (`<meta name="generator" content="Astro v6.3.3">`). No
  feed, no JSON-LD, no script data. No robots.txt (HTTP 404).
- Every talk is a `div.talk-template` (hidden with `style="display: none;"`;
  a script shows the next upcoming one) inside `#next-talk-container`,
  newest first. The attribute `data-talk-date` is the start as a naive local
  time (`2026-12-03T16:00:00`).
- Inside it, `a.talk-card` links the detail page
  (`/talks/talk-schedule/<quarter>-<year>-<speaker>`), and its body holds
  - `h3.talk-card__title`: the title;
  - `p.talk-card__speaker`: `<strong>Guillaume Derval</strong> <U+2022>
    Visiting postdoctoral scholar, ...`: the name in `strong`, then a bullet
    (U+2022) and the affiliation;
  - `p.talk-card__meta`: `Thursday, December 3, 2026 at 4:00 PM <U+2022>
    Packard 202`: date with year, start time, bullet, room;
  - `div.talk-card__slides-icon[data-slides-url]` on talks with slides.
- There is no end time anywhere. Talks are Thursdays at 4 pm (4:30 pm in
  2020) in Packard 202.
- The detail page repeats title (`h1.talk-detail__title`), speaker
  (`div.talk-detail__speaker`), affiliation (`div.talk-detail__affiliation`)
  and meta line (`div.talk-detail__meta`), and adds the abstract and the bio
  in `div.talk-detail__content` (`h3#abstract`, `h3#bio`, each followed by
  paragraphs).
- 3 talks were upcoming when this was recorded: 2026-11-12, 2026-11-19 and
  2026-12-03.

## What the source entry reads from it

Checked offline with the `html_list` adapter and the entry in
`config/sources.d/stanford-extra.yaml`; a test of this source can pin these
values:

- one request, `https://isl.stanford.edu/talks/` (the detail page is not
  read), and 152 drafts;
- 2026-11-12 16:00 (naive local time) with no end, `Packard 202`, speaker
  `Thomas Courtade` with affiliation `Associate Professor, UC Berkeley`,
  title `A galaxy of information inequalities, with applications`, `url`
  `https://isl.stanford.edu/talks/talk-schedule/fall-2026-thomas-courtade`;
- the newest draft is 2026-12-03 16:00 (`Guillaume Derval`).
