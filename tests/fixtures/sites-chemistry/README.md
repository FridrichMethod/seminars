# sites-chemistry

Stanford Chemistry, https://chemistry.stanford.edu. H&S template with an open Drupal
JSON:API. Recorded on 2026-11-11.

| file | what |
|---|---|
| `hs-event-page1.json` | the daily request: 19 events, no further page |

The request is the daily request of the `drupal_jsonapi` adapter, the same as for
`sites-mathematics` with this host; `tests/fixtures/sites-mathematics/README.md` spells
it out and describes the shape of an answer. The two filter values (the run window) are
volatile parameters.

## This site

- No event has a speaker relationship or a type. The title carries everything:
  `Physical Chemistry Seminar: Professor Connor Coley, MIT`,
  `Thesis Defense: Julisia Chau, Lin Group`.
- The title of the talk is in the body: a quoted line, after `About the Seminar` for
  seminars and as the first line for thesis defenses.
- Every event has a series; 3 events have two or three.
- 10 of 19 events are thesis defenses.
- One event has no end time, 3 bodies are null, one location is empty.
- The status vocabulary of this site has the terms `live` and `canceled`, but no event
  of the window uses them.

## Changed by hand

The bodies of 8 events link to a Zoom meeting, and the address carries the passcode of
the meeting. In all 16 places (`body.value` and `body.processed`) the value of the
`pwd` parameter was replaced by `REDACTED`. The parameter itself is still there, so the
fixture still shows that such an address must not be published. Nothing else was
changed.
