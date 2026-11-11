# sites-cs

Stanford Computer Science, https://www.cs.stanford.edu (cs.stanford.edu redirects
there). Recorded on 2026-11-11.

| file | request | what |
|---|---|---|
| `events-all-rss.xml` | `/events/all/rss` | the feed: 9 upcoming events |
| `events.html` | `/events` | the list page: 9 upcoming and 20 past events as cards |

## The template

Stanford Sites standard template (Drupal 11, CSS classes `su-event-*`). JSON:API is
closed on these sites. `tests/fixtures/sites-bioengineering/README.md` describes the
feed, the cards of the list page and the detail page, and how the three fit together;
the selectors are in `params` of the source in `config/sources.d/stanford-sites.yaml`.

## This site

- The list page has TWO views. The upcoming events are in
  `div.view.stanford-events.list-page` (9 cards, same events and order as the feed).
  Below the heading "Past Events" a second view,
  `div.view.stanford-events.past-events-list-block`, holds 20 past events, newest
  first, with a "Load more items" link (`?page=1`). The item selector in the
  configuration leaves the past block out.
- 3 of the 9 upcoming events were imported from events.stanford.edu (HAI Seminar,
  a CNDS seminar); their cards link there.
- The other 6 are `CS Program Sheet Workshop & Advising` sessions of the type
  `CS Info Sessions` (one has no type). They are no talks.
- One card of the past block has a postal address instead of a location.

No detail page was recorded: the events the site hosts itself are no talks, and the
others redirect to events.stanford.edu. Nothing was changed in the bodies.
