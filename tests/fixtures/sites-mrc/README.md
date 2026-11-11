# sites-mrc

Stanford Mathematics Research Center, https://mrc.stanford.edu. H&S template with an
open Drupal JSON:API. Recorded on 2026-11-11.

| file | what |
|---|---|
| `hs-event-page1.json` | the daily request: 2 events, no further page |

The request is the daily request of the `drupal_jsonapi` adapter, the same as for
`sites-mathematics` with this host; `tests/fixtures/sites-mathematics/README.md` spells
it out and describes the shape of an answer. The two filter values (the run window) are
volatile parameters.

## This site

- Only public lectures and named lecture series are entered here (2 events in the
  window); the weekly seminars are on `sites-mathematics`.
- Both events have a speaker (`Name (Affiliation)`), a series and the type `Lectures`.
- `field_hs_event_link` and `field_hs_event_map_link` are used for a poster and for a
  "back to the overview" link, not for what their names say.
- `Tadashi Tokieda: Surprises from rubbing the wrong way` is the same lecture as
  `MRC Public Lecture: Tadashi Tokieda` on `sites-mathematics` and on
  events.stanford.edu.

Nothing was changed in the body.
