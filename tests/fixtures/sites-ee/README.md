# sites-ee

Stanford Electrical Engineering, https://ee.stanford.edu. Recorded on 2026-11-11.
This site is NOT a source in `config/sources.d/`: it uses neither of the two templates
of the department sites and lists no events at present.

| file | request | what |
|---|---|---|
| `events.html` | `/events` | the events calendar: an empty view |
| `research-seminars.html` | `/research/seminars` | a hand-written list of seminar series with links to other sites |

## Structure

- Drupal 10 with a theme of its own (`ee_basic`, Bootstrap classes). No `hs-event-*`
  and no `su-event-*` classes, no JSON-LD, no feed link. `/jsonapi/node/hs_event` and
  `/events/all/rss` answered 404 when
  they were tried on 2026-11-10.
- `/events` has a menu of event types (`nav.menu--event-types`: "All" and "Academic
  Dates") and one view container, `div.views-element-container`, whose inner
  `div.js-view-dom-id-...` is EMPTY: the view has no rows. There is no markup of an
  event to describe.
- `/research/seminars` is a basic page. It names the seminar series of the department
  in prose and links each one to the site that runs it (Information Theory Forum, ISL
  Colloquium, Q-FARM, SmartGrid Seminar, Computer Systems Colloquium EE380, SCIEN,
  SystemX, HCI Seminar, Materials Science Colloquium, Ginzton Lab, and others). It has
  no dates and no speakers; it is a directory, not a calendar.

One file was renamed after recording (`events.html`). Nothing was changed in the
bodies.

## Status of this directory

No source reads this directory: it documents what the site looks like, nothing
more. Should the site ever become a source, it is recorded again under the id of
that source (a source of the custom group has an id starting with `custom-`), and
this directory is removed.
