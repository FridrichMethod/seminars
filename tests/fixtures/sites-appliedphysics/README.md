# sites-appliedphysics

Stanford Applied Physics, https://appliedphysics.stanford.edu. Recorded on 2026-11-11.
This site is NOT a source in `config/sources.d/`: it uses neither of the two templates
of the department sites and lists no upcoming events.

| file | request | what |
|---|---|---|
| `events-upcoming-events.html` | `/events/upcoming-events` | the page "Upcoming Events": empty |
| `events-archive.html` | `/events/archive` | the page "Events Archive": 5 old events |

## Structure

- Drupal 11 with a theme of its own (`themes/custom/physics`, block ids
  `block-physics-*`). No `hs-event-*` and no `su-event-*` classes, no JSON-LD, no feed
  link. `/jsonapi/node/hs_event` and `/events/all/rss` answered 404 when
  they were tried on 2026-11-10.
- `/events/upcoming-events` is a basic page whose content area is empty: no view is
  rendered at all, so there is no markup of an upcoming event to describe.
- `/events/archive` renders the view `div.view.events-archive` with one
  `div.views-row` per event: `.views-field-title a` (title, link to `/events/<slug>`),
  `.views-field-field-event-description` (teaser as HTML) and
  `.views-field-field-location`. The rows show NO date. The 5 events are from 2020 to
  2024 (`Meet the Professor`, `Applied Physics 483 Optics & Electronics Seminar`,
  `SystemX Alliance`).
- The Applied Physics/Physics Colloquium is not on this site. It is on
  physics.stanford.edu (`sites-physics`) and on events.stanford.edu.

One file was renamed after recording (`events-upcoming-events.html`). Nothing was
changed in the bodies.

## Status of this directory

No source reads this directory: it documents what the site looks like, nothing
more. Should the site ever become a source, it is recorded again under the id of
that source (a source of the custom group has an id starting with `custom-`), and
this directory is removed.
