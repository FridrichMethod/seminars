# sites-physics

Stanford Physics, https://physics.stanford.edu. H&S template with an open Drupal
JSON:API. Recorded on 2026-11-11.

| file | what |
|---|---|
| `hs-event-page1.json` | the daily request: 7 events, no further page |
| `hs-event-canceled-sample.json` | NOT a daily request: the 3 latest events with the status term `canceled` |

The request is the daily request of the `drupal_jsonapi` adapter, the same as for
`sites-mathematics` with this host; `tests/fixtures/sites-mathematics/README.md` spells
it out and describes the shape of an answer. The two filter values (the run window) are
volatile parameters.

The sample of cancelled events uses the same `include` and `fields`, but
`page[limit]=3`, `sort=-field_hs_event_date.value,drupal_internal__nid` and
`filter[field_hs_event_status.name]=canceled` instead of the window. Its events are
from 2024 and 2025. No adapter issues this request; it only shows what a cancelled
event looks like.

## This site

- 5 of 7 events are the Applied Physics/Physics Colloquium, imported from
  events.stanford.edu: status term `live`, `field_hs_event_instances` with one entry,
  `field_hs_event_experience` `inperson`, `field_hs_event_sponsor`
  `Applied Physics/Physics Colloquium`, `field_hs_event_link.uri` on
  events.stanford.edu, a category term (`Lecture/Presentation/Talk`) and audience terms.
  Their location has three lines joined with CR LF: building, street address, room.
- The other 2 events are PhD defenses entered on the site (type `PhD Defenses`).
- No speaker and no series relationship in the window. The speaker is in the title:
  `Applied Physics/Physics Colloquium: Jo Dunkley - "The Simons Observatory"`.
- 4 of the 5 colloquia have no end time.
- A cancelled import has the status term `canceled`, and its title starts with
  `Canceled:` (one even reads `Canceled: CANCELED Applied Physics/...`). The cancelled
  events have the series `Colloquium` and the type `Colloquium Event`, which the
  events of the window do not have.

## Changed by hand

The bodies of the 2 defenses show a public Zoom address that carries the passcode of
the meeting. In all 8 places (`body.value` and `body.processed`, as link target and as
link text) the value of the `pwd` parameter was replaced by `REDACTED`. Nothing else
was changed.
