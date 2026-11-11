# custom-rain-seminar

Stanford RAIN/OR Seminar (Recent advances in AI, Incentives, and Operations
Research), recorded 2026-11-11 through the polite client. Nothing was trimmed.

| File | URL | What it is |
|---|---|---|
| `index-9ec536ca.html` | `https://rain.stanford.edu/` | the home page (Jekyll, same template family as mlsys.stanford.edu): 11 talks listed as upcoming, 10 of them still to come |

## What was looked at and not recorded

`https://rain.stanford.edu/feed.xml` (linked with `rel="alternate"`) is an
Atom feed without entries: 570 bytes, `updated` 2026-11-10. The talks come
from the HTML.

## Layout

- One list, `ol.talk-list`, after the `h2` "Upcoming Talks", oldest first.
- One talk is `ol.talk-list > li.talk`:
  - `h3.talk-presenter`: the speaker's name.
  - `div.talk-affiliation`: the affiliation.
  - `time.talk-date`: the attribute `datetime` is the date in ISO form
    (`2026-11-10`); the text is `Nov 10, 2026`.
  - `div.talk-logistics`: three `span` children. The first is the time,
    `4:30-5:30 PM PT` written with an en dash (U+2013); the second is a
    middle dot; the third is the room (`Y2E2 101`, or `TBD`).
  - `div.talk-title`: the title; missing while it is not announced (9 of 11).
  - `details`: `summary` "Talk details", then the abstract and `Bio:`;
    missing for talks without a title.
- No link to a page about a talk.
- The list still shows a talk the day after it took place (the first row is
  dated the day before the recording).
- The page embeds a Google Calendar of the series. Its feed is on
  calendar.google.com, whose robots.txt disallows every path, so it was not
  requested.
- Earlier talks are under `/archive/` (not requested).
