# sites-systemx

Stanford SystemX Alliance (Electrical Engineering), https://systemx.stanford.edu.
Recorded on 2026-11-12 (Pacific time) through the polite client.

| file | request | what |
|---|---|---|
| `events-all-rss.xml` | `/events/all/rss` | the feed: 2 upcoming events |
| `events.html` | `/events` | the list page: the same 2 events as cards, and 20 past events |
| `detail-seminar.html` | `/events/seminars/computing-encrypted-data-concept-reality` | the detail page of the seminar of 2026-11-12 |
| `detail-workshop.html` | `/events/workshop/systemx-physical-intelligence-summit` | the detail page of the summit of 2026-12-03 |

The bodies were renamed after recording (the manifest names the files above).

## The template

Stanford Sites standard template (Drupal 11, CSS classes `su-event-*`). JSON:API is
closed on these sites. `tests/fixtures/sites-bioengineering/README.md` describes the
feed, the cards of the list page and the detail page, and how the three fit together;
the selectors are in `params` of the source in `config/sources.d/stanford-sites.yaml`.

## This site

- The 2 events: the SystemX Seminar "Computing on encrypted data: from concept to
  reality" on Thursday 2026-11-12, 4:30 to 5:30 pm in STLC 114 (type `Seminars`), and
  the SystemX Physical Intelligence Summit on 2026-12-03, 9 am to 6 pm at the Frances
  C. Arrillaga Alumni Center (type `Workshop`).
- The title of a seminar is the talk title. The speaker is named only in the body of
  the detail page, under the heading `Speaker Bio:` (Ingrid Verbauwhede, KU Leuven, in
  the image `alt` and the first sentence); the template has no speaker field, so the
  draft has no speaker. The body starts with `Abstract/Description:` and becomes the
  abstract.
- The list page also has a "Past Events" block (`past-events-list-block`, 20 cards);
  the item selector leaves it out.
- Cards have an address and no alternative location.

Nothing was changed in the bodies. The pages show no personal address or number.
