# bayarea-slac-workshops

The source reads five category exports of Indico at SLAC (adapter `ics`), recorded
2026-11-18 from https://indico.slac.stanford.edu/category/<id>/events.ics, with the
robots.txt of that host ("User-agent: * / Disallow: /") set aside as the source's
`ignore_robots` says.

| file | category | what |
|---|---|---|
| `category-170-events-ics-*.ics` | 170 RL4AA | RL4AA'27, 2027-04-20 13:00 to 2027-04-23 17:30 PDT, location `SLAC` |
| `category-171-events-ics-*.ics` | 171 SSRL Workshops | "Re-envisioning X-ray Science in the Age of SI and Automation", 2026-12-09 08:30 to 2026-12-10 17:00 PST, in `53/2-2002 - Berryessa (SLAC)` |
| `category-20-events-ics-*.ics` | 20 QIS Workshop | "Q-NEXT All Hands 2027", 2027-03-16 08:00 to 2027-03-17 17:00 PDT, in `51/1-102 - Kavli Auditorium (SLAC)` |
| `category-16-events-ics-*.ics` | 16 SLAC Summer Institute | no event |
| `category-19-events-ics-*.ics` | 19 ML@SLAC Workshop | no event |

- Times are UTC (`...Z`). A workshop of several days is one VEVENT from the start of
  its first day to the end of its last.
- DESCRIPTION: RL4AA'27 has the description and, after a blank line, the address of
  the Indico event. The SSRL workshop starts with a line "Speakers: Name (Affiliation),
  ..." of eight people, then blank lines, the description and the address. The Q-NEXT
  meeting has the speaker line and the address only.
- RL4AA'27 also has an `IMAGE` property, the other two a `CONTACT`; the adapter reads
  neither.
- The UIDs have the form of an e-mail address
  (`indico-event-<number>@indico.slac.stanford.edu`); they are no addresses
  (tests/test_fixtures_contact_addresses.py).

## What was replaced

The `CONTACT` property of the SSRL workshop named the personal address of an organiser,
and that of the Q-NEXT meeting the address of its organising committee. Both were
replaced by `redacted@slac.stanford.edu`. No VEVENT holds a meeting link or a
password. Nothing else was changed.
