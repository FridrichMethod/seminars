# sites-cheme

Stanford Chemical Engineering, https://cheme.stanford.edu. Recorded on 2026-11-11.

| file | request | what |
|---|---|---|
| `events-all-rss.xml` | `/events/all/rss` | the feed: 2 upcoming events |
| `events.html` | `/events` | the list page: the same 2 events as cards |
| `detail-native.html` | `/events/chemical-engineering-colloquium/amy-e-herr` | the detail page of an event the site hosts itself |

## The template

Stanford Sites standard template (Drupal 11, CSS classes `su-event-*`). JSON:API is
closed on these sites. `tests/fixtures/sites-bioengineering/README.md` describes the
feed, the cards of the list page and the detail page, and how the three fit together;
the selectors are in `params` of the source in `config/sources.d/stanford-sites.yaml`.

## The detail page

The event is `section.node-page--stanford-event`. Inside it:

- `data-end-date` on the section: the END of the event in Unix seconds (1794879000 is
  2026-11-16 5:30 pm in Stanford). It is the only machine-readable time on the page.
- `.su-event-type a`: the type, here the series (`Chemical Engineering Colloquium`).
- `.su-event-headline h1`: the title.
- `.su-event-subheadline` and `.su-event-dek`: two free lines below the title.
- `.su-event-date` (`Monday, November 16, 2026`) and `.su-event-time`
  (`4:30pm - 5:30pm PST`): text only.
- `.su-event-alt-location`: the location as free text; `.su-event-location` is used
  instead when the event has a postal address (see sites-icme).
- `.su-event-audience div`: who may come.
- `.su-event-body`: the description as HTML.
- A block `stanford-events-more-events-block` below the event holds cards of OTHER
  events (`article.su-event-card`); do not read them as part of this one.

There is no JSON-LD and no speaker field anywhere in this template.

## This site

- Chemical Engineering Colloquium: the title of the event is the NAME OF THE SPEAKER
  (`Amy E. Herr`), the subheadline is the affiliation and the dek is the title of the
  talk. The feed repeats the dek in `<description>`.
- The body has the speaker with a link, then paragraphs that start with bold
  `Title:`, `Abstract:` and `Bio:`.
- The location reads `(In person) Turing Auditorium`.
- The second event (`Three L.A.S.E.R. talks ...`) was imported from
  events.stanford.edu; its card links there.

One file was renamed after recording (`detail-native.html`). Nothing was changed in
the bodies.
