# custom-sail-events

Stanford AI Lab (SAIL) events page and site feed, recorded 2026-11-11
through the polite client. This directory documents a finding; it is not
enough to write an adapter, because neither holds a dated event.

| File | URL | What it is |
|---|---|---|
| `events-a0b7a4e1.html` | `https://ai.stanford.edu/events/` | the events page: a WordPress page with prose and an embedded Google Calendar |
| `feed-5415b6db.xml` | `https://ai.stanford.edu/feed/` | the site feed that the page links: 10 news posts, no event |

## What was trimmed

The feed is as the site sent it. From the page one line was removed: the
paragraph `Questions: Click Here`, a `mailto:` link with the address of one
staff member (part of the note about a meeting in 2023). The response was
35986 bytes, the fixture is 35898 bytes.

## What the page is

- A plain WordPress page (page id 2356), not the listing of an events plugin.
- The only place with dated events is an `iframe` that embeds a public Google
  Calendar; its address is the `src` attribute of the `iframe` in
  `section.overview`. The page says that the calendar lists talks and reading
  groups of the lab and that the audience of an entry is given in brackets:
  open to the public, Stanford only, or one group only.
- The iCalendar feed of that calendar is on calendar.google.com. The
  robots.txt of that host is `Allow: /$` and `Disallow: /`, so the polite
  client refuses every feed address. Nothing was fetched from it.
- The structured data WordPress offers holds no events:
  - the one JSON-LD block describes the organisation
    (`"@type": "ResearchOrganization"`);
  - `https://ai.stanford.edu/feed/`, the site feed that the page links with
    `rel="alternate"`, is RSS 2.0 with 10 posts, all in the category `News`,
    published between 2024-07-26 and 2025-02-05 (`lastBuildDate` 2025-02-05):
    awards and appointments, no talk and no date of an event. It is recorded
    here as evidence (recorded later the same day, 19:07 UTC);
  - `https://ai.stanford.edu/events/feed/` is the comment feed of the page and
    has no item; the comment feed of the site, `/comments/feed/`, was not
    requested;
  - `https://ai.stanford.edu/wp-json/` lists six namespaces (`wp/v2`,
    `oembed/1.0`, `akismet/v1`, `wp-site-health/v1`, `wp-block-editor/v1`,
    `wp-abilities/v1`) and 134 routes, none of them for events.
    `/wp-json/wp/v2/posts` was not requested. In WordPress it lists the same
    posts as the site feed, so no events are expected there, but that was not
    checked.
- The rest of the page is prose about series: an annual meeting in 2023, the
  Distinguished Speaker Series (its page
  `https://ai.stanford.edu/events/ai-distinguished-speakers-series/` is a
  table whose newest row is dated 2020-01-24), the AI Salon and others.
