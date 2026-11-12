# sites-aimi

Stanford Center for Artificial Intelligence in Medicine and Imaging (AIMI),
https://aimi.stanford.edu. Recorded on 2026-11-12 (Pacific time) through the polite
client.

| file | request | what |
|---|---|---|
| `events-all-rss.xml` | `/events/all/rss` | the feed: 7 upcoming events |
| `events.html` | `/events` | the list page: the same 7 events as cards |
| `detail-ibiis-aimi-seminar.html` | `/events/ibiis-aimi-seminar-prateek-prasanna-phd` | detail page of the IBIIS-AIMI Seminar of 2026-11-18 |
| `detail-grand-rounds-tba.html` | `/events/grand-rounds/aimi-grand-rounds-tba-5` | detail page of the AIMI Grand Rounds of 2026-11-24 |
| `detail-grand-rounds-stankovic.html` | `/events/grand-rounds/aimi-grand-rounds-genetic-diagnosis-and-discovery-enabled-large-language-models` | detail page of the AIMI Grand Rounds of 2026-12-15 |

The bodies were renamed after recording (the manifest names the files above).

## The template

Stanford Sites standard template (Drupal 11, CSS classes `su-event-*`). JSON:API is
closed on these sites. `tests/fixtures/sites-bioengineering/README.md` describes the
feed, the cards of the list page and the detail page, and how the three fit together;
the selectors are in `params` of the source in `config/sources.d/stanford-sites.yaml`.

## This site

- The 7 events: the Computational Medicine Research Colloquium of 2026-11-12 (Nigam
  Shah), the AI4MH Seminar and the IBIIS-AIMI Seminar of 2026-11-18, two AIMI Grand
  Rounds (2026-11-24, title TBA; 2026-12-15, Konstantina Stankovic), and two events in
  May 2027 outside the run window (the HAILS course, the AIMI Symposium).
- A title reads `<series>: <talk title> - <speaker>, <degrees>`, or
  `<series>: <speaker>, <degrees>` before the talk title is known, or
  `<series>: TBA`.
- Two cards link to other hosts with `su-link--external`: the colloquium to
  `computationalmedicine.stanford.edu/seminars-and-training/colloquium/` (the page of
  the whole weekly series; it lists past talks and the current one, nothing later)
  and the AI4MH Seminar to a page on `med.stanford.edu`. Their feed links point at
  the site. Neither target is fetched; the external link is the url of the event.
- Only the Grand Rounds cards have a type (`Grand Rounds`). Two cards have an
  alternative location (`Hybrid: In-Person | Virtual`, `Zoom Webinar`); no card has
  an address.
- Detail pages (the three native events in the window): the IBIIS-AIMI page has the
  body `Details TBA`; the Grand Rounds TBA page has a description of the series;
  the Stankovic page has speaker, bio and abstract in the body and a Zoom webinar
  registration link (no passcode).

## What was replaced

`events.html` carried three Drupal contextual-link tokens
(`data-contextual-token="..."`, values the server computes for the page). They were
replaced by `REDACTED`, keeping the attribute. The shared address of the center
(`aimicenter@stanford.edu`) was kept. Nothing else was changed.
