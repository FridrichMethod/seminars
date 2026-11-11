# bayarea-slac-colloquium

Recorded 2026-11-11 from colloquium.slac.stanford.edu (Drupal 10, SLAC theme; no
Crawl-delay in robots.txt).

- `upcoming-events-*.html`: the list. Each event is a `div.c-card` inside
  `div.c-view__row`: title and link in `h3.c-card__title a`, speaker in
  `.slac-speaker .speaker-name`, affiliation in `.slac-speaker .speaker-affiliation`,
  abstract teaser in `.c-field--name-field-body`, and the date only as text in
  `.c-card__date` ("Monday, November 16, 2026", a middle dot, then
  "4:00 p.m." an en dash "5:00 p.m." two no-break spaces and "PT").
  No machine-readable time on the list.
- `events-*.html`: the five detail pages linked from the list. Each one embeds an
  "iCal" link whose href is a `data:text/calendar` URI holding one VEVENT with UID,
  SUMMARY, DTSTART/DTEND (TZID=America/Los_Angeles) and DESCRIPTION. That is the most
  reliable time source. There is no location anywhere on these pages.
- Every detail page also holds, below the event, a "more events" block
  (`div.c-view--pre-filtered-content-lists`) with the cards of 3 OTHER events. Those
  cards use the list classes (`div.c-card`, `.c-card__date`,
  `.c-field--name-field-body`), so a detail selector must ignore matches inside that
  block. The page's own fields: title `.c-page-title h1`, speaker line
  `.c-page-title .c-lede` (on 2 of the 5 pages), abstract
  `.event-detail__content .c-wysiwyg`; the iCal link is outside the block.
- "No SLAC Colloquium 11/23" is a placeholder for a week without a talk, not an event.
- "Talk by Phil Bucksbaum" / "Talk by Thomas Feurer" carry "Info coming soon!" (TBA).
- The node slug date (2026-10-22-...) is the creation date, not the event date.
