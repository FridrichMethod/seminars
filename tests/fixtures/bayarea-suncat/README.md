# bayarea-suncat

Recorded 2026-11-11 from suncat.stanford.edu, the SUNCAT Center for Interface Science
and Catalysis (a SLAC-Stanford institute, linked from the SLAC seminar index as
"SUNCAT Center for Interface Science and Catalysis Events"). Drupal 11 with a theme of
its own, not the Stanford Sites template; robots.txt sets no Crawl-delay.

| file | request | what |
|---|---|---|
| `events-fb92e9c2.html` | `/events` | "Upcoming Events": 7 rows |
| `events-seminars-f87b9193.html` | `/events/seminars` | NOT read by the source: the list of the tag "Seminars", empty |
| `events-dr-hyeonjung-jung-85bd8375.html` | `/events/dr-hyeonjung-jung` | detail page of the first row |
| `events-dr-ruchika-mahajan-9f08bd1a.html` | `/events/dr-ruchika-mahajan` | detail page of the second row |

- Rows: `main div.views-row`; title and link in `h5 a`; the date as text only in
  `.views-field-field-events-date .field-content` ("Friday, November 20, 2026 -
  10:00am-11:00am (PT)"; a multi-day event reads "Monday, August 2, 2027 - 10:00am -
  Friday, August 6, 2027 - 02:00pm"); the room in
  `.views-field-field-events-location .field-content`; a short line in `em`.
- 6 of the 7 rows are Friday 10:00 talks whose title is only the speaker's name
  ("Dr. Hyeonjung Jung"). Their detail pages have no talk title, no abstract and the
  tag "Group meetings" (`.field--name-field-events-tag .field__item`): internal group
  meetings, not public seminars. The 7th row is the 2027 SUNCAT Summer Institute
  (August 2027, outside a 120-day window).
- The sidebar names the tag lists Seminars (`/events/seminars`), Group Meetings and
  SUNCAT Virtual Events; the Seminars list shows no event at present.
- Not recorded: `/events/all/rss` is 404. No feed link, no JSON-LD, no ICS link.
