# bayarea-slac-theory

The source reads the Indico category of the SLAC Theory Group (adapter `ics`). The
directory also keeps the pages of the group's own site, which the slac_drupal tests read.

- `category-129-events-ics-*.ics`: recorded 2026-11-17 from
  https://indico.slac.stanford.edu/category/129/events.ics, with the robots.txt of that
  host ("User-agent: * / Disallow: /") set aside as the source's `ignore_robots` says.
  11 talks from 2026-10-21 to 2026-11-25. The DESCRIPTION of a talk is "Speakers: Name
  (Affiliation)", a blank line, the abstract, a blank line and the address of the Indico
  event; the three talks without a title ("TBA") have no abstract, and one talk
  (indico-event-10820) has no speaker line and names its speaker in the title. The UIDs
  have the form of an e-mail address (`indico-event-<number>@indico.slac.stanford.edu`);
  they are no addresses (tests/test_fixtures_contact_addresses.py).
- `events-*.html`, recorded 2026-11-11 from theory.slac.stanford.edu (same Drupal 10
  SLAC theme as the SLAC Colloquium site): "Upcoming Theory Events" shows "No items to
  display." and sends readers to the Indico category above.
- `events-past-events-*.html`: page 1 of 8 of past events (last one 2024-12-04),
  `div.c-card` cards with the speaker as title and the date as text in `.c-card__date`.
  Kept as parser input for the shared SLAC Drupal card layout.
