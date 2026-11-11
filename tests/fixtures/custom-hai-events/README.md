# custom-hai-events

Stanford HAI events, recorded 2026-11-11 through the polite client. Nothing
was trimmed. The list page and the detail pages of all 10 upcoming events are
recorded, so an offline run that requests every upcoming detail page finds
an answer for each of them.

| File | URL (below `https://hai.stanford.edu`) | What it is |
|---|---|---|
| `events-ba2e38a4.html` | `/events` | the list page: 10 upcoming and the 10 most recent previous events |
| `events-hai-seminar-the-ai-100-43da3f5a.html` | `/events/hai-seminar-the-ai-100` | detail page: seminar with a speaker, a moderator and a room |
| `events-angele-christin-hannah-zeavin-gur-29ce1f27.html` | `/events/angele-christin-hannah-zeavin-gurus-hucksters-entertainers-how-influencers-reshaped-social-media` | detail page: a speaker and a person "In conversation with" |
| `events-2027-conference-on-physics-and-ai-5bb7efe8.html` | `/events/2027-conference-on-physics-and-ai-phystat-2027` | detail page: three-day conference, no people |
| `events-marlowe-ai-data-for-science-with-c0d9bd79.html` | `/events/marlowe-ai-data-for-science-with-mohsen-bayati-1111-430pm-at-coda-e160` | redirect page to `marlowe-research.stanford.edu` |
| `events-marlowe-ai-data-for-science-with-32c5f189.html` | `/events/marlowe-ai-data-for-science-with-andreas-tolias-1118-430pm-at-coda-e160` | redirect page to `marlowe-research.stanford.edu` |
| `events-marlowe-ai-data-for-science-with-86e3044f.html` | `/events/marlowe-ai-data-for-science-with-leonidas-guibas-122-430pm-at-coda-e160` | redirect page to `marlowe-research.stanford.edu` |
| `events-khaoula-chehbouni-best-practices-b9e1089d.html` | `/events/khaoula-chehbouni-best-practices-for-the-use-of-llms-as-judges` | redirect page to a Zoom webinar registration |
| `events-special-seminar-and-fireside-chat-a0fc79e2.html` | `/events/special-seminar-and-fireside-chat-understanding-work-using-ai` | redirect page to `digitaleconomy.stanford.edu` |
| `events-christopher-ong-research-accelera-d9dd3300.html` | `/events/christopher-ong-research-acceleration-the-view-inside-openai` | redirect page to `digitaleconomy.stanford.edu` |
| `events-youngju-jo-data-driven-discovery-c915b628.html` | `/events/youngju-jo-data-driven-discovery-and-control-of-neural-computations` | redirect page to `events.stanford.edu` |

## The list page

A Next.js page (App Router). There is no JSON-LD, no feed and no `__NEXT_DATA__`
block, but the server component payload ("flight" data of React) is in the
page and holds the events as plain JSON objects.

### Joining the payload

- Every inline `<script>` whose text is `self.__next_f.push([1,"..."])`
  carries one JSON string (23 here). Decode each string and join them in
  document order. The one script `(self.__next_f=self.__next_f||[]).push([0])`
  carries no data.
- The joined text is 134695 characters, 134722 bytes in UTF-8. Cut it into
  rows on its UTF-8 bytes, not on its characters: the length of a text row
  counts bytes.

### Cutting it into rows

The payload is not one row per line. Read it from the start:

1. A row starts with `<id>:`, where the id is hexadecimal and can be empty.
   The 43 hint rows of this page (`:HL[...]`, style sheets to preload) have
   an empty id.
2. If `T<hex length>,` follows the colon, the row is a text row: exactly that
   many bytes after the comma belong to it, and the next row starts right
   after them. A text row does not end with a newline.
3. Any other row ends at the next `\n`.

This page has two text rows, both SVG path data of the site logo: `c:Ta55,`
(2645 bytes), followed directly by row `0`, and `22:T30e9,` (12521 bytes),
followed directly by row `e`. Cut this way the page has 46 rows with an id
and 43 hint rows. Splitting at line starts that match `^[0-9a-f]+:` gives
44 rows instead: rows `0` and `e` disappear into the text rows before them,
and every hint row is glued to the end of the row before it. The row with
the events survives that only by chance, so do not split by lines.

After the colon, an upper-case letter marks a row that is not JSON: `I`
(module reference), `H` (hint), `T` (text), `E` (error; the redirect pages
below have one). Every other row is one JSON value; `[`, `{`, `"` and
`null` occur.

### String values

Inside the JSON, strings that start with `$` are not always text:

- `$undefined`: the value is missing.
- `$$...`: the text itself starts with `$`; drop the first `$`.
- `$<hex id>`: the value of the row with that id. React moves every string
  of 1024 characters or more into a text row of its own and puts such a
  reference in its place, so a long `dek` or `title` can arrive as `"$2f"`.
  Here only the two logo paths are moved (`"d":"$c"` in row `0`, `"$22"` in
  row `e`).
- `$<hex id>:<key>:...`: a value inside the row with that id, reached key by
  key. Inside a React element (`["$", type, key, props]`) the key `props`
  means its fourth item. Seven occur here (`$18:metadata`,
  `$e:props:children:0:...`, `$f:props:children:3:...`), all in the
  component tree.
- `$`, `$L<hex>`, `$@<hex>` and `$S...` belong to the component tree (element
  marker, lazy component, promise, symbol) and do not occur in event objects.

No event object of this page holds a `$` value other than `$undefined`, but
an adapter must resolve plain `$<hex id>` references in the fields it reads
anyway.

### The events

- Walk the JSON rows and collect every object that has an `event_start` key:
  20 objects here, all in row `25`. The 10 upcoming ones are the `entries`
  prop of one component, the 10 previous ones are `initialEntries.data` of
  another (with `meta.per_page` 10 and `meta.total` 473). The page links
  `/events?page=2` to `/events?page=4`; they were not requested.
- Keys of an event object: `title`, `href` (`/events/<slug>`), `event_start`,
  `event_end` (string or `null`), `dek` (HTML summary), `tag.label` (`Seminar`,
  `Event`, `Conference`, `Event Promo`), `event_types[].title`,
  `contentMeta.topics[].title`, `promo_url` (the organiser's own page or a
  registration link), `featured_event`, `image`. A missing value is
  `$undefined`. `contentMeta.people` is empty for every event.
- `event_start` and `event_end` look like UTC (`2026-11-11T16:30:00.000000Z`)
  but are wall-clock times in Pacific time: the same event is shown as
  `Nov 11, 2026 4:30 PM - 5:30 PM` in the card, and the detail page of the
  AI100 seminar says `12:00 PM - 1:15 PM` for `2026-11-18T12:00:00.000000Z`.
  Drop the `Z` and read the value as local time.
- Titles are often `Speaker | Title` or `Series | Title`. The slug in `href`
  can be stale: the first event is titled "... with Alex Pentland" under a
  slug that names another speaker.
- The same events are also rendered as cards. CSS class names are CSS-module
  names with a build hash (`ContentItem_root__bsx20`), so match the prefix:
  `div[class*="ContentItem_root__"]` (20 cards), title and link in
  `a[class*="ContentItem_titleLink__"]`, type in
  `span[class*="EventContentMeta_type__"]`, the date in the `span` without a
  class inside `div[class*="EventContentMeta_root__"]` (`Nov 11, 2026`), the
  time in `span[class*="EventContentMeta_time__"]` (`4:30 PM - 5:30 PM`,
  missing for some events), the summary in
  `div[class*="ContentItemDek_root__"]`.
- The payload names the content API of the site (`.../cms/api/...`). The
  robots.txt of the site disallows `/cms/` and `/api/`; it was not requested.

## The detail pages

An event has a detail page of its own only when its `promo_url` is missing.
Of the 10 upcoming events, 3 have one and 7 have a `promo_url`.

### Events with a `promo_url`: redirect pages

`/events/<slug>` answers HTTP 200 with a page of about 108 KB that only
forwards the browser to the `promo_url`:

- `<meta id="__next-page-redirect" http-equiv="refresh" content="1;url=<promo_url>">`
  and `<meta name="robots" content="noindex, nofollow">`;
- an error row in the flight data, `25:E{"digest":"NEXT_REDIRECT;replace;<promo_url>;307;"}`;
- no `DetailHero`, `DetailMeta` or person elements. `<title>` repeats the
  title and `<meta name="description">` the summary.

The targets are on other hosts (`marlowe-research.stanford.edu`,
`digitaleconomy.stanford.edu`, `events.stanford.edu`, `stanford.zoom.us`),
which this source does not list. Such an event is built from the list data
alone. Requesting its detail page costs a request and gains nothing, so skip
it; a detail page that turns out to be a redirect page is treated the same
way.

### Events without a `promo_url`: full pages

Server-rendered HTML; its flight data has no `event_start` object.

- title: `h1[class*="DetailHero_heading__"]`
- label and value pairs: `div[class*="DetailMeta_row__"]`, two `div` children
  each: `Status` (`Upcoming`), `Date`, `Location`, `Topics`.
  - `Date`: `Wednesday, November 18, 2026 12:00 PM - 1:15 PM`, or for an event
    of several days `Monday, May 24, 2027 - Wednesday, May 26, 2027`. The
    list already has both times, so this row is only a cross-check.
  - `Location`: `353 Jane Stanford Way, Stanford, CA, 94305 | Room 119`, or
    `Simonyi Conference Center, 389 Jane Stanford Way, Stanford, CA 94305`.
- people: `[class*="PersonCard_root__"]` with
  `[class*="PersonCard_personName__"]` and `[class*="PersonCard_title__"]`.
  They sit in lists headed by `[class*="PersonList_title__"]`. Headings seen:
  `Speaker`, `Moderator`, `In conversation with`. The speakers are the people
  under `Speaker` (or `Speakers`) and `In conversation with`, whom the title
  names next to the speaker; moderators are not speakers. The conference has
  no person list.
- `[class*="EventDetailButtonSet_root__"] a`: `Attend Virtually`, a Zoom
  webinar registration link (no passcode in it), on two of the three pages.
- description: `section[class*="Article_content__"]`.

## When a detail page cannot be used

A detail request that fails (any `HttpError`; in offline mode a missing
recording raises `FixtureNotFoundError`, which is one), a redirect page, and
a page without `DetailHero` all leave the event
as the list describes it: title, times, summary, type and topics, without
location and speakers. One bad detail page must not drop the event or the
rest of the run.
