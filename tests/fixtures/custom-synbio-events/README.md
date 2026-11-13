# custom-synbio-events

Stanford Synthetic Biology events, recorded 2026-11-11 through the polite
client.

| File | URL | What it is |
|---|---|---|
| `events-4f9cce76.html` | `https://sb.stanford.edu/events` | the events page (a Google Sites page): 3 event blocks under Upcoming Events (2 of them still to come) and 91 under Past Events; 95 events, because one block holds two |

## What was trimmed and replaced

The response was 548240 bytes, over the 500 KB limit for a fixture. Two cuts
brought it to 478858 bytes and one replacement to 478863; everything else is
as the site sent it.

1. All 12 `<script>` elements were removed (62850 bytes). They hold the
   configuration and the start-up code of Google Sites and no event data. One
   of them carried the browser API key and the OAuth client id of Google Sites
   itself, which have no place in this repository.
2. One past event block was removed whole, from its anchor
   `<div id="h.6xaaqtygq2fx">` up to the anchor of the next block: "From
   Poster to Pitch: Crafting Your 30-Second Science Story" (July 28, 2026, a
   workshop on science communication). Its `Where:` line linked a Zoom meeting
   with the passcode in the address (`?pwd=...`). The live page had 95 event
   blocks, the fixture has 94.
3. One personal e-mail address was replaced by `redacted@mit.edu`. In the
   heading `Seminar with Domitilla Delvecchio (MIT)` the name links to the
   speaker's faculty page, whose address ends in her own e-mail address
   (`https://meche.mit.edu/people/faculty/<address>`). The name, the rest of
   the link and the shared address of Stanford Synthetic Biology
   (`syntheticbiology@stanford.edu`) stay.

Expect Zoom links on the live page: a `Where:` line can link a Zoom meeting,
and the address can carry a passcode. Tests for that case need a made-up link.
A speaker's link can hold a personal e-mail address, which is replaced before
a new recording is committed.

## Layout

- No feed, no JSON-LD, no embedded data. Free text typed into Google Sites.
- Class names are the generated names of Google Sites (`tyJCtd`, `zfr3Q`,
  `C9DxTc`). They mean nothing and can change with a new version of Google
  Sites; the element structure below is the safer thing to rely on.
- The events are in `section div.tyJCtd` containers (22 on the page; the past
  events are all in one). A container is a flat run of siblings: an empty
  anchor `div` with `id="h.<key>"`, a heading (`h1` or `h2`, both occur, with
  `id="h.<key>_l"`), then `p` elements up to the next anchor.
- A heading whose text is `Upcoming Events` or `Past Events` starts a group;
  `Events` is the page title. The last heading, `Stanford|Synthetic Biology`
  (`id="h.tto67zm2iuk9_l"`, under `Past Events`), is the footer: no line
  follows it and it is not an event. Every other heading is the name of an
  event.
  `https://sb.stanford.edu/events#h.<key>` leads to it, which makes `<key>` a
  usable native id.
- 54 of the 94 event headings contain links (`a.XqQF9c`). Most of them wrap
  the name of a speaker and lead to the speaker's own page
  (profiles.stanford.edu, a lab site); the linked text is then the cleanest
  form of the name. A link can also wrap the whole heading and lead to a page
  about the event (events.stanford.edu, once), or wrap a company name.
- Text is split over many `span` elements, sometimes in the middle of a
  number. Join the text of a `p` without a separator and then collapse white
  space; joining with a space gives `1 0 :00 a m - 4 : 0 0 pm`.
- The lines of a block are labelled by hand:
  - `When: 12:00 pm - 1:00 pm on Tuesday, November 10, 2026` (every event
    block; 95 lines in 94 blocks, see the Expo below).
    Variants in the fixture: `12pm - 1pm on ...`, `12:00-1:00pm on ...`,
    `3:00 - 4:00pm` with an en dash, a start time only (`5:30pm on ...`),
    no time (`When: Friday, September 12, 2025`), two days
    (`Thursday, June 22 to Friday, June 23, 2023`), abbreviated months
    (`Nov 19, 2024`, `Sept 24 2024`), ordinals (`April 21st, 2026`), a missing
    comma before the year, a trailing remark
    (`... April 15, 2024 (Refreshments served at 4pm)`) and a misspelt month
    (`Ocober 13, 2026`).
  - `Where: Shriram Tea Room`.
  - the talk title, under changing labels: `Title:`, `Topic:`, `Talk Title:`,
    `Talk title:`, `Talk:`, `Seminar title:`. `TBA` and `TBD` occur. Some
    blocks have no title line at all.
  - any other `p` is description (`Lunch provided`, an abstract, a programme).
- The speaker is only in the heading: `SB.Talk with Dr. Sean Wang`,
  `SB Talk by Dr. Maya Arce, Science Fellow @ Arc Institute`,
  `SB.Talk with Samuel King (Hie Lab)`. 55 headings start with `SB.Talk`,
  `SB Talk` or `SB.Talks` followed by `with` or `by`.
- The block `2025 Stanford Synthetic Biology Expo` (`h.mm237rpco6n6`,
  September 12, 2025) holds a second event: after the programme of the Expo
  (`12:00 - 1:00 PM Poster Setup` with an en dash, a list of flash talks)
  comes the paragraph
  `Meet the Stanford iGEM Team / Undergraduate Research Talk`, then
  `When: 12:00 pm on Tuesday, July 29, 2025` and `Where: Shriram Tea Room`.
  A second `When:` line in a block therefore starts another event, named by
  the paragraph before it.
- The two-day line `When: Thursday, June 22 to Friday, June 23, 2023` also
  fits a pattern for single days, with `Thursday, June 22 to` as the time;
  a time part that holds a month or a weekday is a range of days.
- In two lines the weekday does not fit the date (`Monday, May 15, 2025` was
  a Thursday, `Tuesday, April 15, 2024` a Monday).
- An event stays under `Upcoming Events` for a while after it took place (the
  first upcoming block is dated the day before the recording).
