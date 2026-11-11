# bayarea-slac-theory

Recorded 2026-11-11 from theory.slac.stanford.edu (same Drupal 10 SLAC theme as the
SLAC Colloquium site).

- `events-*.html`: "Upcoming Theory Events" shows "No items to display." and sends
  readers to the Indico category https://indico.slac.stanford.edu/category/129/ for
  future SLAC Theory seminars. indico.slac.stanford.edu answers robots.txt with
  "User-agent: * / Disallow: /", so that schedule cannot be collected.
- `events-past-events-*.html`: page 1 of 8 of past events (last one 2024-12-04),
  `div.c-card` cards with the speaker as title and the date as text in `.c-card__date`.
  Kept as parser input for the shared SLAC Drupal card layout.
