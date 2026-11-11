# bayarea-biohub

Recorded 2026-11-11 from biohub.org (WordPress; robots.txt allows everything, no
Crawl-delay).

- `events-e78c38af.html` (/events/): upcoming events in `ul.content-type-events`, past
  ones in `ul.content-type-past_events`; each `li.multisite-query-loop-item` card has the
  region in `.wp-block-post-date` (for example "San Francisco Bay Area",
  "Other Locations"), the date as text in `.wp-block-heading-event-date`, the title in
  `h3.card-title`, a teaser in `p.card-excerpt` and the detail link in
  `.card-button a[href]`. No time of day on the list.
- `events-webinar-esmfold2-*.html`, `events-physics-of-life-2026-*.html`: detail pages.
  `.event-meta--date-time-location time[datetime]` gives start and end with offset;
  `.event-meta__value--location` the place ("Zoom" for the webinar).
- Detail pages carry the WordPress post id in `link[rel="shortlink"]`
  (`https://biohub.org/?p=52744`) and in the body class (`postid-52744`).
- JSON-LD on these pages is the Yoast WebPage graph only (no schema.org/Event).
- Past cards on the list that an adapter must not follow: the booth cards "Cell Bio -
  ASCB" and "NeurIPS" link to www.ascb.org and neurips.cc, and "New York Affiliate
  Symposium (Invite Only)" has no link at all.
- Not recorded: /wp-json/ answers 401 rest_not_logged_in (REST closed to anonymous
  users); /events/feed/ is 404; /feed/?post_type=event is a 527 KB RSS of 36 items
  (33 of them with an /events/ link) whose pubDate is the publication date and which
  carries no event date at all.
