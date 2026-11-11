# sites-biology

Stanford Biology, https://biology.stanford.edu. H&S template with an open Drupal
JSON:API. Recorded on 2026-11-11.

| file | what |
|---|---|
| `hs-event-page1.json` | the daily request: 8 events, no further page |

The request is the daily request of the `drupal_jsonapi` adapter, the same as for
`sites-mathematics` with this host; `tests/fixtures/sites-mathematics/README.md` spells
it out and describes the shape of an answer. The two filter values (the run window) are
volatile parameters.

## This site

- 6 of 8 events are the Biology Seminar Series, imported from events.stanford.edu:
  status term `live`, one entry in `field_hs_event_instances`,
  `field_hs_event_experience` `inperson`, `field_hs_event_link.uri` on
  events.stanford.edu, a category term (`Class/Seminar`) and an audience term. Their
  location has three lines joined with CR LF.
- The other 2 events are thesis defenses entered on the site.
- No speaker relationship and no type. The title carries the speaker:
  `Biology Seminar Series - Peter Walter, PhD "Targeting the Cell's Stress Pathways for
  Therapeutic Benefit"` (the site writes a typographic apostrophe), and
  `Aaron Behr Dissertation Defense`.
- The series are `Department Seminars` and `Thesis Defenses`.
- 5 of 8 bodies are null: the abstract of an imported seminar stays on
  events.stanford.edu.
- The status vocabulary of this site has the terms `live`, `canceled` and `Published`.

## Changed by hand

The 2 defenses publish how to join their Zoom meeting. `field_hs_event_map_link.uri`
is a Zoom address that carries the passcode (2 places), and the body spells the
passcode out as `Zoom Passcode:...` (4 places, `body.value` and `body.processed`). The
value of the `pwd` parameter and the passcode in the text were replaced by `REDACTED`.
Nothing else was changed.
