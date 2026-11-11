# sites-statistics

Stanford Statistics, https://statistics.stanford.edu. H&S template with an open Drupal
JSON:API. Recorded on 2026-11-11.

| file | what |
|---|---|
| `hs-event-page1.json` | the daily request: 10 events, no further page |

The request is the daily request of the `drupal_jsonapi` adapter, the same as for
`sites-mathematics` with this host; `tests/fixtures/sites-mathematics/README.md` spells
it out and describes the shape of an answer. The two filter values (the run window) are
volatile parameters.

## This site

- 9 of 10 events have no end time: `end_value` equals `value` and `duration` is 0.
  `2026-11-03T00:00:00+00:00` is Monday 2 November, 4:00 pm in Stanford.
- One all-day event (`2026 Industrial Affiliates Annual Conference`): `value`
  `2026-11-05T08:00:00+00:00`, `end_value` `2026-11-06T07:59:00+00:00`, `duration` 1439.
- The speaker label reads `Name, Affiliation`, with a comma.
- 4 titles are `Title to come`; their bodies are null.
- No type terms; the series are `Statistics Seminar`, `Probability Seminar`,
  `Special Events`, `Conferences`, `Industrial Affiliates Conference Series`. One event
  has two series.
- The status vocabulary of this site is empty.

Nothing was changed in the body.
