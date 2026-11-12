# custom-csb-events

Events of the Stanford Department of Chemical and Systems Biology, recorded
through the polite client: the list and the Garfield post on 2026-11-11, the
Sieber post on 2026-11-12 (Pacific time).

| File | URL | What it is |
|---|---|---|
| `events-bebd0f3c.html` | `https://chemsysbio.stanford.edu/events/` | the archive of the WordPress category "Events": the 10 newest posts |
| `events-join-us-csb-special-seminar-open-abb211c4.html` | `https://chemsysbio.stanford.edu/events/join-us-csb-special-seminar-open-discussion-david-garfield-phd-monday-november-16-2026-1100-am-1245-pm-munzer/` | the post of the special seminar of 2026-11-16 |
| `events-save-the-date-csb-special-seminar-a33d4935.html` | `https://chemsysbio.stanford.edu/events/save-the-date-csb-special-seminar-stephan-sieber-phd-tum-munich-december-3-2026-1230-pm-clark-auditorium/` | the post of the special seminar of 2026-12-03 |

Nothing was trimmed. The Sieber post names the host and the contact of the
seminar with their personal e-mail addresses (`Host: Matthew Bogyo, PhD
(...)`, `Contact: Yoli Cervantes (...)`); both addresses were replaced by
`redacted@example.edu` and the names kept. Nothing else was replaced. Every
page shows the department's office telephone number (`Phone: 650.723.9785`,
three times in the footer, once per building address). It is the shared
number of the department, not a personal one, and was kept. Every page also
carries the values of Cloudflare's page beacon for this one response
(`window.__CF$cv$params` with `r`, the ray id, and `t`, the time of the
request, and `ray=` in the address of its script). They identify the
response, are no credential and were kept as received.

## Layout

- WordPress. The head links the category feed
  (`/category/events/feed/`) and `/wp-json/wp/v2/categories/30`. Both were
  looked at and not recorded: the feed and `/wp-json/wp/v2/posts?categories=30`
  hold the same 10 posts with their publication time; no event field exists
  (`acf` is empty). No JSON-LD.
- The list is `div.secondary-content-main > article`, newest post first, no
  pagination link. An article has an `img` (the flyer), an `h2 a` (title and
  link) and `a.news-cta` ("Read More").
- The post title is the whole announcement, for example
  `JOIN US! CSB Special Seminar &#038; Open Discussion, David Garfield, PhD,
  Monday, November 16, 2026, 11:00 am &#8211; 12:45 pm, Munzer!`. Date
  (sometimes without the year), time (`11:00 am &#8211; 12:45 pm`,
  `12:30 pm`, `1pm`, `12 pm`) and room are parts of it, separated by commas.
- The 10 posts (2026-08-21 to 2026-12-11): two special seminars (2026-11-16,
  2026-12-03), two PhD defenses (2026-09-15, 2026-09-23), five social events
  (holiday party, Friendsgiving, two lab happy hours, a reception) and one
  news item without a date (the new PhD cohort).
- Links point to `/events/<slug>/` or `/uncategorized/<slug>/`, depending on
  the first category of the post.
- The post page has the announcement as text. The Garfield post uses `div`
  elements: speaker in `b`, affiliation in `i`, then date, time and room,
  `Talk Title:`, `Abstract:` and `Speaker Bio:`. The Sieber post uses
  paragraphs with `<br>`: "The Department of Chemical and Systems Biology
  Presents a Special Seminar", "Prof. Dr. Stephan A. Sieber", "Department of
  Chemistry", "Technical University Munich (TUM)"; then date, time
  (`12:30 PM <en dash> 1:30 PM`) and room; `Talk Title:`; host and contact.
  No JSON-LD.
- A post's own heading can change after the list was recorded: the Sieber
  post, whose address still says `save-the-date-...`, is headed `JOIN US! CSB
  Special Seminar, Stephan Sieber, PhD, December 3, 2026, 12:30 pm, Clark
  Auditorium!`, while the list recording of 2026-11-11 shows its earlier
  title. The source reads the title from the list only.

## What the source entry reads from it

Checked offline with the `html_list` adapter and the entry in
`config/sources.d/stanford-extra.yaml`; a test of this source can pin these
values:

- three requests (the list and the posts of the two seminars in the run
  window) and 4 drafts; no social post and not the news item without a date;
- 2026-11-16 11:00 to 12:45 (naive local time), `Munzer`, speaker
  `David Garfield`, title `CSB Special Seminar & Open Discussion, David
  Garfield, PhD`, event type `seminar`, `url` the post of the seminar;
- 2026-12-03 12:30 with no end, `Clark Auditorium`, speaker `Stephan Sieber`
  with the affiliation `Department of Chemistry, Technical University Munich
  (TUM)` from the lines under the name in the post;
- the two defenses with event type `defense`: 2026-09-15 12:00,
  `Clark S360 or via Zoom`, `Alex Van Elgort`; 2026-09-23 13:00,
  `ChEM-H E153`, `Rebecca Rodell` (both titles have no year; the year is
  the one closest to the recorded fetch).

The ChEM-H list (`tests/fixtures/sites-chemh/events.html`) links the same
two seminar posts, with the talk titles instead of the announcement.
