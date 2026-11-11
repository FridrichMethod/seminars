# sites-icme

Stanford Institute for Computational and Mathematical Engineering,
https://icme.stanford.edu. Recorded on 2026-11-11.

| file | request | what |
|---|---|---|
| `events-all-rss.xml` | `/events/all/rss` | the feed: 2 events |
| `events.html` | `/events` | the list page: the same 2 events as cards |
| `detail-native-schedule.html` | `/events/research-symposium-seminars/first-year-seminar-series-2026-cme-300` | the detail page of the seminar series, with its schedule |

## The template

Stanford Sites standard template (Drupal 11, CSS classes `su-event-*`). JSON:API is
closed on these sites. `tests/fixtures/sites-bioengineering/README.md` describes the
feed, the cards of the list page and the detail page, and how the three fit together;
the selectors are in `params` of the source in `config/sources.d/stanford-sites.yaml`.

## This site

- The seminar series is ONE event that spans the quarter: `First Year Seminar Series
  2026 (CME 300)`, 2026-09-22 to 2026-12-01, without a time of day. In the feed its
  `<pubDate>` is midnight of the first day (already in the past, it is listed because
  the event is still running); on the card both `<time datetime>` values are dates
  only, and the card shows a second stacked date (`su-event-end-month`,
  `su-event-end-date`).
- The location of the card is a postal address block (`.views-field-su-event-address`,
  `Stanford Campus`), not `.views-field-su-event-location`.
- The sessions are on the detail page, in the block `div.stanford-events-schedule`:
  an `<h3>` with the date (`Tuesday, September 22, 2026`), then one
  `.su-event-schedule` with `.su-event-schedule__time` (`1:30pm - 2:45pm PDT`),
  `.su-event-schedule__headline` (`Seminar Session 1`) and
  `.su-event-schedule__description` (the speakers as free text, with links to their
  profiles). 11 dates, 11 items; two headlines read `No class - ...`.
- `data-end-date` on the event section is the end of the last day.
- The second event is a one-day symposium in May 2027 (start and end with a time).
- The source `sites-icme` is disabled in the configuration until an adapter can
  turn the schedule into one event per session; without that it would publish one
  all-day event of ten weeks.

## Changed by hand

The body of the detail page names a contact person with an e-mail address. The address
was replaced by `redacted@example.edu` in both places (link target and link text). One
file was renamed after recording (`detail-native-schedule.html`). Nothing else was
changed.
