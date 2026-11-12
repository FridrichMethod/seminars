# sites-mse

Stanford Materials Science and Engineering, https://mse.stanford.edu. Recorded on
2026-11-12 (Pacific time) through the polite client.

| file | request | what |
|---|---|---|
| `events-all-rss.xml` | `/events/all/rss` | the feed: 4 upcoming events |
| `events.html` | `/events` | the list page: the same 4 events as cards |
| `detail-native.html` | `/events/mse-colloquium/mse-colloquium-robert-hovden` | the detail page of the colloquium of 2026-11-18 |

The bodies were renamed after recording (the manifest names the files above).

## The template

Stanford Sites standard template (Drupal 11, CSS classes `su-event-*`). JSON:API is
closed on these sites. `tests/fixtures/sites-bioengineering/README.md` describes the
feed, the cards of the list page and the detail page, and how the three fit together;
the selectors are in `params` of the source in `config/sources.d/stanford-sites.yaml`.

## This site

- The 4 events: the MSE Colloquium on Wednesdays at 11:30 (2026-11-18 Robert Hovden,
  2026-12-02 Yanjie Shao, 2027-01-13 Max Saccone) and the William D. Nix Lecture of
  2027-04-02 (type `Lecture / Reading`, subheadline `Distinguished Speaker Subra
  Suresh`), outside the run window.
- A colloquium is titled `MSE Colloquium: <speaker>` and has the type
  `MSE Colloquium`; the site never shows a talk title. Before the speaker is known the
  title is `MSE Colloquium: Speaker TBA` and the page address ends in
  `mse-colloquium-speaker-tba-<n>`. Once the speaker is announced, the title and the
  address change and the old address redirects to the new one, while the feed guid
  stays: the colloquium of 2026-12-02 had the address `.../mse-colloquium-speaker-tba-10`
  earlier on 2026-11-12 and `.../mse-colloquium-yanjie-shao` at the recording, both
  with guid `97cf11be-6cc1-4034-85bd-0f898c6c55a0`. The source key of an rss draft is
  the guid, so the rename does not change the uid.
- Every card has an address (`p.address` with `<br>` between the lines), no
  alternative location.
- The detail page of a colloquium (`detail-native.html`) repeats the card: title,
  date, time, address, audience. It has no body. The source therefore does not fetch
  detail pages (`detail_page.fetch: never`); the page is kept as the evidence.

Nothing was changed in the bodies. The pages show no personal address or number.
