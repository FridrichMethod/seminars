# custom-theory-seminar

Stanford Theory Seminar page, recorded 2026-11-11 through the polite client.
Nothing was trimmed. This directory documents a finding; it is not enough to
write an adapter, because the page holds no dated events.

| File | URL | What it is |
|---|---|---|
| `seminar-c62d0751.html` | `https://theory.stanford.edu/seminar/` | the page of the seminar: one sentence and an embedded Google Calendar |

## How the page was found

`https://theory.stanford.edu/` is a 143 byte page that forwards to
`main/index.shtml` with a `meta refresh`. That page lists the recurring
events of the theory group and links this one as "Stanford Theory Seminar",
with the remark that it "happens occasionally as of 2022".

## What the page is

- The page says: "The Stanford Theory Seminar is not happening regularly at
  the moment, but please join our mailing list and/or check out the calendar
  below for updates on sporadic seminars."
- "Upcoming Theory Events" is an `iframe` that embeds four public Google
  Calendars (the `src` parameters of its address). One of the four is the
  calendar that rain.stanford.edu embeds for the RAIN/OR seminar.
- The iCalendar feeds of those calendars are on calendar.google.com. The
  robots.txt of that host is `Allow: /$` and `Disallow: /`, so the polite
  client refuses every feed address. Nothing was fetched from it.
- No feed, no JSON-LD, no list of talks in the HTML. The archive pages linked
  in the navigation (`2019-20.html`, `archive.html`) were not requested.

The talk series of the theory group that publish a schedule in HTML are
recorded as `custom-theory-lunch` and `custom-rain-seminar`.
