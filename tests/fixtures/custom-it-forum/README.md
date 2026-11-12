# custom-it-forum

Information Theory Forum (Stanford Information Systems Laboratory), recorded
2026-11-11 (Pacific time) through the polite client. The source is disabled;
these recordings are for a later adapter.

| File | URL | What it is |
|---|---|---|
| `group-it-forum-talks-talks-1ebed354.html` | `https://web.stanford.edu/group/it-forum/talks/talks/` | the list of talks, one section per year from 2026 back to 2020 (53 talks), links to the years before |
| `group-it-forum-talks-talks-2026-emmanuel-610ffe38.html` | `https://web.stanford.edu/group/it-forum/talks/talks/2026/emmanuel-abbe/` | the page of the only talk of 2026 (2026-11-06) |

Nothing was trimmed or replaced; neither page has an e-mail address or a
telephone number. The home page (`/group/it-forum/talks/`, not recorded)
names the organizer with a personal e-mail address.

## Layout

- Hugo 0.55 site. No feed link, no JSON-LD. `/talks/talks/index.xml` exists
  (not linked, not recorded): 23 items from 2023 to 2025, every `pubDate` is
  `Thu, 03 Sep 2020 11:34:48 -0800`, and the talk of 2026 is missing.
- The list: `div._sections > div._section`, newest year first. A section
  starts with `a._title > h3` (the year, `2026`), then one `div._talk` per
  talk, oldest first within the year:
  `<a href=".../talks/talks/2026/emmanuel-abbe/"><p class="_row1">
  <span class="_date">06-Nov</span> <span class="_speaker">...</span>
  <span class="_affiliation">...</span></p><p class="_row2">Title</p></a>`.
  The date has no year and the row has no time and no room.
- The sections hold 1 talk (2026), 3 (2025), 11 (2024), 9 (2023), 18 (2022),
  8 (2021) and 3 (2020, linked as `/talks/talks/2020q4/`). Each year also has
  a page of its own (`/talks/talks/<year>/`); the years before 2020 are on
  `/talks/talks/previous-years/` and `/group/it-forum/past.html`.
- The talk page: `div._talk` with `h1` (title), `h3` (`Emmanuel Abb&eacute;
  &ndash; EPFL and Apple AIML`), a `p` with `Fri, 06-Nov-2026 / 2:30pm
  / Allen 101X` (weekday, date with year, start, room), then `h3#abstract`
  and `h3#bio`, each followed by a paragraph.
