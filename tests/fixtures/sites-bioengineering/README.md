# sites-bioengineering

Stanford Bioengineering, https://bioengineering.stanford.edu. Stanford Sites standard
template (Drupal 11, CSS classes `su-event-*`). Recorded on 2026-11-11.

| file | request | what |
|---|---|---|
| `events-all-rss.xml` | `/events/all/rss` | the feed: 4 upcoming events |
| `events.html` | `/events` | the list page: the same 4 events as cards |
| `detail-localist-redirect.html` | the `<link>` of the second feed item | redirected to events.stanford.edu (`final_url` in the manifest) |

## The template

JSON:API is closed on the sites of this template: `/jsonapi/node/stanford_event`
answers HTTP 502 (tried on cheme.stanford.edu). What an adapter can use:

**The feed** (`/events/all/rss`; `/events/rss` returns the same bytes) lists the
upcoming events only, soonest first. An `<item>` has

- `<title>` with unescaped markup inside: `<h2><a href="/events/..." hreflang="en">Title</a></h2>`.
  A standard RSS parser reads an empty title; take the text of the inner `<a>`.
  Characters such as `&` are escaped once (`&amp;`).
- `<link>`: the address of the event on the site.
- `<pubDate>`: the START of the event, with the offset of that day.
- `<guid isPermaLink="false">`: a UUID, the best native id.
- `<description>`: empty, or the "dek" of the event (see sites-cheme, sites-icme).

There is no end time, no location, no speaker and no abstract in the feed.

**The list page** (`/events`) shows the same events in the same order as cards:
`div.view.stanford-events article.su-event-list-item`. In a card:

- `h3 a`: the title and the link. The link is a path on the site for an event the
  site hosts itself, and an absolute URL with the class `su-link--external` for an
  event taken from somewhere else. It is NOT the `<link>` of the feed in that case.
- `.su-event-type a`: the type, for example `Lecture/Presentation/Talk`.
- `time[datetime]`: the first is the start, the second (when there is one) the end,
  both with offset. A value without a time of day is an all-day event.
- `.views-field-su-event-location`: the location, often only the building.
- `.event-list-item__subheadline` and `.event-list_item__dek` (sic) on some sites.

A card and a feed item belong together when the title and the start agree; on all six
sites the order is the same too.

**The detail page** of an event the site hosts itself is described in
`tests/fixtures/sites-cheme/README.md`. For an event taken from somewhere else the
address in the feed only redirects to the other site.

## This site

- All 4 events were imported from events.stanford.edu (Stanford Energy Seminar, a PhD
  defense, a conference): every card links there.
- `detail-localist-redirect.html` shows what the feed link of such an event does: the
  site answers with a redirect to `https://events.stanford.edu/event/...`, and the
  body is the Localist page of the event, not a page of this site. An adapter that
  stays on the hosts of its source must not follow the feed link of a card with an
  external link; the Localist source has the same event with all its fields.
- One card (the defense) has no end time.

## Changed by hand

In `detail-localist-redirect.html`, three values that belong to the visitor session or
to the calendar site were replaced by `REDACTED`: the Google Maps key in the address of
the embedded map (`key=`), the content of the `csrf-token` meta element and the value
of the hidden `authenticity_token` input. Two files were renamed after recording
(`events.html`, `detail-localist-redirect.html`). Nothing else was changed.
