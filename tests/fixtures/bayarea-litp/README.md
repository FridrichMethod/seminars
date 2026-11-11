# bayarea-litp

Recorded 2026-11-11 from sitp.stanford.edu, the Leinweber (formerly Stanford) Institute
for Theoretical Physics. Found through the SITP link on theory.slac.stanford.edu; it is
an H&S template site (Drupal 11, `hs-event-*`), robots.txt Crawl-delay: 30, and
/events/all/rss is 404.

| file | what |
|---|---|
| `hs-event-window.json` | the daily request: 14 events, no further page (no `links.next`) |

The request is the daily request of the `drupal_jsonapi` adapter in the form the
template A sources of the stanford-sites group use (`page[limit]=50`, sort by
`field_hs_event_date.value,drupal_internal__nid`, six `include` relationships, the
window filter and sparse `fields`), with one addition: `custm_speaker_affiliation` at
the end of `fields[node--hs_event]`. The two filter values are the run window of
2026-11-11 in Unix seconds (2026-10-28 00:00 to 2027-03-12 00:00 local time) and are
volatile parameters.

## This site

- 9 events of the institute itself, series "LITP Seminars", "LITP Monday Colloquia"
  and "Friday Phenomenology Group Seminars". Each has one speaker
  (`hs_entity--event_collections__speaker`, name only in `label`); the affiliation is
  the event attribute `custm_speaker_affiliation`. Rooms: Varian 355, Varian 312.
- 5 events are imports of the Applied Physics/Physics Colloquium from
  events.stanford.edu: status term `live`, `field_hs_event_link.uri` on
  events.stanford.edu, category and audience terms, no speaker, location in three lines
  joined with CR LF. Three of them carry the series "Applied Physics/Physics
  Colloquia", two no series at all.
- The type term "Colloquia" is set on one Monday colloquium only.
- No e-mail address, telephone number or Zoom address is in the recording (the
  sparse fieldset leaves the contact fields out).

An earlier recording of the 20 latest events (`page[limit]=20`, sorted by date
descending, three relationships, no window) was replaced by this one, because no
adapter issues that request.
