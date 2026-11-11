# bayarea-kipac

Recorded 2026-11-11 from kipac.stanford.edu (Stanford Sites template, `su-event-*`;
robots.txt Crawl-delay: 30; /jsonapi/node/hs_event is 404).

| file | request | what |
|---|---|---|
| `events-all-rss-a0cd73b7.xml` | `/events/all/rss` | the feed: the next 10 items only |
| `events-upcoming-events-67a837f4.html` | `/events/upcoming-events` | list page 0: 20 cards, 2026-11-12 to 2026-12-11 |
| `events-upcoming-events-ce5f7424.html` | `?page=1` | list page 1: 20 cards, 2026-12-11 to 2027-02-25 |
| `events-upcoming-events-cfb570e9.html` | `?page=2` | list page 2: 20 cards, 2027-02-25 to 2027-05-13 |
| `events-all-rss-85397722.xml` | `/events/all/rss?page=1` | NOT read by the source: byte-identical to the feed, so the feed has no pages |

- Feed: title is unescaped `<h2><a>` markup, `<pubDate>` is the start,
  `<description>` is the location, `<guid>` a UUID.
- List cards: `article.su-event-list-item`; type in `.su-event-type a`; title `h3 a`;
  speakers in `.event-list-item__subheadline` ("Name (Affiliation)", several joined by
  " / ", and "(GSFC / UMD)" has a slash inside the brackets); location in
  `.views-field-su-event-location` and again in `.event-list_item__dek`; start and end
  in `time[datetime]` (date only for the all-day workshop of Dec 2-4).
- Pager: `.js-pager__items a` "Load More Events" with href `?page=1` on page 0,
  `?page=2` on page 1 and `?page=3` on page 2. With the window of 2026-11-11 (to
  2027-03-12) page 2 is the last one needed: its last card starts on 2027-05-13.
- The detail path carries the series: /events/kipac-seminar/, /events/kipac-tea-talk/,
  /events/astrophysics-colloquium/; /events/other/ holds social events (patio time,
  Friendsgiving, holiday party, bonfire, BBQ).
- Untitled talks carry only the series as title ("KIPAC Seminar", "Astrophysics
  Colloquium"); others repeat it in front ("KIPAC Tea Talk: ...").
- The AP/Physics Colloquium card of Dec 1 links off the site to physics.stanford.edu
  (`su-link--external`).
- Not recorded: the "Subscribe to the KIPAC calendar" ICS at
  https://s3df.slac.stanford.edu/data/kipac/calendar/events.ics is served as
  application/octet-stream, which the HTTP client refuses as binary.
