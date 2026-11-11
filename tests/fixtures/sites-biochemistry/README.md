# sites-biochemistry

Stanford Biochemistry, https://biochemistry.stanford.edu. Recorded on 2026-11-11.

| file | request | what |
|---|---|---|
| `events-all-rss.xml` | `/events/all/rss` | the feed: 4 upcoming events |
| `events.html` | `/events` | the list page: the same 4 events as cards |
| `detail-native.html` | `/events/seminar/frontiers-biology-steve-blacklow-harvard-molecular-mechanisms-signal-transduction` | the detail page of the second event |

## The template

Stanford Sites standard template (Drupal 11, CSS classes `su-event-*`). JSON:API is
closed on these sites. `tests/fixtures/sites-bioengineering/README.md` describes the
feed, the cards of the list page and the detail page, and how the three fit together;
the selectors are in `params` of the source in `config/sources.d/stanford-sites.yaml`.

## This site

- All 4 events are the weekly "Frontiers in Biology" seminar, entered on the site
  (type `Seminar`). The title holds the series, the speaker, the affiliation and the
  title of the talk: `Frontiers in Biology: Steve Blacklow, Harvard  | "Molecular
  mechanisms of signal transduction"`.
- The cards have a start and an end, but no location field. The room is only in the
  free-text subheadline: `11/18/26, @Clark Auditorium, 4PM`.
- The detail page has no body: no abstract, no location field. It has nothing that
  the card does not have, so fetching it gains nothing. `tests/fixtures/sites-cheme/README.md`
  describes the elements of a detail page.

One file was renamed after recording (`detail-native.html`). Nothing was changed in
the bodies.
