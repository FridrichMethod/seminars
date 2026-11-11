# sites-chemh

Stanford ChEM-H, https://chemh.stanford.edu. Recorded on 2026-11-11.

| file | request | what |
|---|---|---|
| `events-all-rss.xml` | `/events/all/rss` | the feed: 4 upcoming events |
| `events.html` | `/events` | the list page: the same 4 events as cards |

## The template

Stanford Sites standard template (Drupal 11, CSS classes `su-event-*`). JSON:API is
closed on these sites. `tests/fixtures/sites-bioengineering/README.md` describes the
feed, the cards of the list page and the detail page, and how the three fit together;
the selectors are in `params` of the source in `config/sources.d/stanford-sites.yaml`.

## This site

- Every event is only a pointer to a page somewhere else. The cards link to
  chemsysbio.stanford.edu (twice), med.stanford.edu and a PDF poster on
  mcusercontent.com. The feed links point at the site, but they redirect: fetching
  `/events/breaking-bacterial-resistance-chemical-dysregulation` ended on
  chemsysbio.stanford.edu. No detail page was recorded for that reason.
- The cards have a title, a start and an end. They have no type, no location and no
  speaker.
- The list is the view display `view stanford-events cards` (component
  `chem_h_subtheme:event_card`), not `list-page` as on the other sites. The item
  selector `div.view.stanford-events article.su-event-list-item` covers both.

Nothing was changed in the bodies.
