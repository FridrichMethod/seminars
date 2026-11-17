# bayarea-slac-experimental

The source reads the Indico category of the SLAC Experimental Seminar Series (adapter
`ics`).

- `category-14-events-ics-*.ics`: recorded 2026-11-17 from
  https://indico.slac.stanford.edu/category/14/events.ics, with the robots.txt of that
  host ("User-agent: * / Disallow: /") set aside as the source's `ignore_robots` says.
  5 talks from 2026-10-20 to 2026-11-24, Tuesdays at 12:30 PT. The DESCRIPTION of a
  talk is "Speakers: Name (Affiliation)", a blank line, the abstract, a line with a
  no-break space, the Zoom address of the series (with its meeting password) and,
  after a blank line, the address of the Indico event; the two talks without a
  speaker yet ("TBA") have "TBA" in place of the speaker line and the abstract. The
  UIDs have the form of an e-mail address
  (`indico-event-<number>@indico.slac.stanford.edu`); they are no addresses
  (tests/test_fixtures_contact_addresses.py).
