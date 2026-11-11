# sites-mathematics

Stanford Mathematics, https://mathematics.stanford.edu. H&S template (Drupal 11) with an
open Drupal JSON:API. Recorded on 2026-11-11.

| file | what |
|---|---|
| `hs-event-page1.json` | the daily request, first page: 50 events |
| `hs-event-page2.json` | `links.next.href` of page 1: 6 events, no further page |

The request is the one the `drupal_jsonapi` adapter issues in a daily run. It is built
from `params` of the source in `config/sources.d/stanford-sites.yaml` (line breaks
added here; the exact URL is the key in `manifest.json`):

    GET https://mathematics.stanford.edu/jsonapi/node/hs_event
        ?page[limit]=50
        &sort=field_hs_event_date.value,drupal_internal__nid
        &include=field_hs_event_speaker,field_hs_event_series,field_hs_event_type,
                 field_hs_event_status,field_hs_event_category,field_hs_event_audience
        &filter[start][condition][path]=field_hs_event_date.value
        &filter[start][condition][operator]=>=
        &filter[start][condition][value]=1793170800
        &filter[end][condition][path]=field_hs_event_date.value
        &filter[end][condition][operator]=<
        &filter[end][condition][value]=1804838400
        &fields[node--hs_event]=title,path,body,status,changed,field_hs_event_date,
                 field_hs_event_instances,field_hs_event_location,field_hs_event_map_link,
                 field_hs_event_link,field_hs_event_stream,field_hs_event_stream_info,
                 field_hs_event_experience,field_hs_event_sponsor,field_hs_event_admission,
                 field_hs_event_registration,field_hs_event_speaker,field_hs_event_series,
                 field_hs_event_type,field_hs_event_status,field_hs_event_category,
                 field_hs_event_audience
        &fields[hs_entity--event_collections__speaker]=label
        &fields[node--hs_event_series]=title,path
        &fields[taxonomy_term--hs_event_type]=name
        &fields[taxonomy_term--hs_event_status]=name
        &fields[taxonomy_term--hs_event_category]=name
        &fields[taxonomy_term--hs_event_audience]=name

The two filter values are the run window of 2026-11-11 in Unix seconds
(2026-10-28T00:00-07:00 and 2027-03-12T00:00-08:00, that is 14 days back and 120 days
ahead). They are recorded as volatile parameters, so a request with other values is
answered from the same file. The date field is a Smart Date field; the filter was
verified with Unix seconds as the value, and an ISO 8601 value was not tried.

## Shape of an answer

- `data[]`: one `node--hs_event` per event. `id` is a UUID.
- `data[].attributes`: `title`, `path.alias` (the detail page, relative to the site),
  `body.value` (HTML, may be null), `field_hs_event_date`, `field_hs_event_location`
  (plain text), `status` (published), `changed`, and the fields that are filled only on
  events imported from events.stanford.edu: `field_hs_event_instances`,
  `field_hs_event_link`, `field_hs_event_experience`, `field_hs_event_sponsor`.
- `field_hs_event_date`: `{value, end_value, duration, rrule, rrule_index, timezone}`.
  `value` and `end_value` are UTC with `+00:00`. `duration` is in minutes. `end_value`
  equal to `value` (duration 0) means the event has no end time. Duration 1439 from
  local midnight is an all-day event. `rrule` is null on every event; `timezone` is an
  empty string or null and says nothing.
- `data[].relationships.<field>.data`: a list of `{type, id}` (speaker, series, type,
  category, audience), or one `{type, id}` or null (status). The resources are in
  `included[]`, matched on `type` and `id`:
  - `hs_entity--event_collections__speaker`: `attributes.label`, one string that holds
    the name and the affiliation.
  - `node--hs_event_series`: `attributes.title`, `attributes.path.alias`.
  - `taxonomy_term--hs_event_type`, `--hs_event_status`, `--hs_event_category`,
    `--hs_event_audience`: `attributes.name`.
- `links.next.href` is there when more events follow; it carries the whole query and
  `page[offset]`.
- Without the `fields[...]` parameters one event takes about 12 KB (a `metatag`
  attribute of 4 KB on every resource), so 50 events would be far over 500 KB.

## This site

- 56 events in the window: weekly seminars of 18 series, one event per talk.
- 52 events have a speaker; the label reads `Name (Affiliation)`. A few labels are a
  bare name, one is lower case (`saehoon`).
- 53 events have a series, 55 a type (`Seminar`, once `Lectures`).
- 6 titles are `TBA` or end in `TBA`; 9 bodies are null.
- 4 events have no end time.
- One event is an import from events.stanford.edu (`MRC Public Lecture: Tadashi
  Tokieda`): status term `live`, one entry in `field_hs_event_instances`,
  `field_hs_event_experience` `inperson`, `field_hs_event_link.uri` on
  events.stanford.edu, a category term and audience terms. `sites-mrc` lists the same
  lecture under another title.

## Looked at, not recorded

- No event of the site has a recurrence rule: a filter on
  `field_hs_event_date.rrule IS NOT NULL` returns nothing. The same holds for
  chemistry, physics, biology and statistics.
- The status vocabulary of this site has one term, `live`. Events entered on the site
  have no status; a cancellation is written into the title. Titles found with a filter
  `title CONTAINS cancel` or `postpone`: `(CANCELED) Gluing small black holes along
  timelike geodesics`, `Gluing small black holes along timelike geodesics (POSTPONED)`,
  `Topology (Cancelled)`, `Cancelled due to COVID19 - Sharp Boundary Trace Theory ...`.
  The same filter also finds `Better than square-root cancellation ...`, which is not
  cancelled, so match the whole word.
- `/jsonapi` lists the resource types; there is no resource for recurrence rules.

Nothing was changed in the bodies.
