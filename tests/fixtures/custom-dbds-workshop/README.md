# custom-dbds-workshop

The Workshop in Biomedical Data Science (BMDS 280) of the Stanford Department of
Biomedical Data Science, https://dbds.stanford.edu/weekly-seminar/. Recorded on
2026-11-12 (Pacific time) through the polite client.

| file | request | what |
|---|---|---|
| `weekly-seminar.html` | `/weekly-seminar/` | the page of the workshop with the schedule of the quarter |

The body was renamed after recording (the manifest names the file above).

## Layout

- WordPress with a page builder (WPBakery). The heading of the page says when and
  where every talk takes place: `Thursdays 1:30 <en dash> 2:50PM Li Ka Shing Center
  (Room: LKSC 120)`. Below a description of the workshop, `Fall 2026 Schedule` has one
  column per talk (`div.wpb_column`), five to a row: a figure with the speaker's
  photo, captioned with the name, and a text block (`div.wpb_text_column`) whose one
  paragraph reads `<strong>Date:</strong> November 12, 2026<br><strong>Speaker:</strong>
  Lucas Janson<br><strong>Title:</strong> <a href=".../Flier-....pdf">Chiseling: ...</a>`.
  The title links to a PDF flyer when there is one.
- 10 talks, 2026-09-24 to 2026-12-03, every Thursday except Thanksgiving. The titles
  of the last two (Marinka Zitnik 2026-11-19, Rafael Irizarry 2026-12-03) are
  `Coming soon`.
- No talk has a page of its own; a draft has no url and its uid falls back to the
  day and the speaker.
- JSON-LD has only WebPage, BreadcrumbList, WebSite and Organization. The site's
  iCalendar feed (`/events/?ical=1`) lists a forum and a retreat, no talk of the
  workshop; `/wp-json/wp/v2/pages/9094` is this page as JSON with the same HTML.

## What the source entry reads from it

10 drafts from one request, each with the date, the speaker and the title (the last
two keep `Coming soon`, which normalization reads as TBA), from 13:30 to 14:50 naive
local time (`default_start_time`, `default_end_time`). The room is in the heading of
the page, outside the talk, so every draft has the location `LKSC 120 (Li Ka Shing
Center)` from `default_location` of the source entry.

## What was replaced

- The personal e-mail address of the staff contact for speaker suggestions (in the
  `mailto:` link and its text) by `redacted@example.edu`; the name stays.
- Two WordPress nonces (`"nonce":"..."` in the script data `lpData` and
  `pendariPublicationList`) by `REDACTED`.

The shared addresses of the department (`dbds-operations@`, `dbds-admissions@`) and
its fax number in the footer were kept.
