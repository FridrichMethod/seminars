# localist-stanford

Recorded on 2026-11-11 from the Localist API of Stanford Events
(`https://events.stanford.edu/api/2/`). The source entry is in
`config/sources.d/localist.yaml`; its comments describe the requests and the
fields of a record.

## Files

| File | Request | Content |
|---|---|---|
| `events-types-page1.json` to `events-types-page4.json` | `/events?type[]=...` (the five talk-like event types), pages 1 to 4 | 166 records, 50 per page, the last page has `next_page: null` |
| `events-untyped-page1.json` | `/events?exclude_type[]=...` (all 17 event types) | 22 records without any event type, a single page with `next_page: null` |
| `events-filters.json` | `/events/filters` | the event types, subjects and audiences with their ids |
| `departments-filters.json` | `/departments/filters` | the kinds of departments and groups (`/groups/filters` answers the same) |
| `departments-page1.json` to `departments-page8.json` | `/departments?pp=100&page=N` | all 736 departments, trimmed (see below) |
| `groups-page1.json` to `groups-page4.json` | `/groups?pp=100&page=N` | all 400 groups, trimmed (see below) |

The five event pages are exactly what the adapter fetches in one run, so
`offline_client("localist-stanford")` serves a complete run of both queries.
The other files are reference data: no adapter requests them.

## How the event requests are matched

The event pages were recorded for the days 2026-11-09 to 2026-11-18
(`start=2026-11-09&end=2026-11-19`, the end is exclusive) with `pp=50`.
`start`, `end` and `pp` are volatile parameters: their values are ignored
when a request is looked up. The adapter sends the dates of its time window
and `pp=100` and still gets these pages.

- A recorded page therefore holds 50 records although 100 were asked for.
  Follow `page.next_page` until it is `null`; never derive the number of
  pages from the page size.
- `page` is not volatile and must be part of every request, the first one
  included (`page=1`).
- The order and the percent-encoding of the parameters do not matter, but
  the ids of `type[]` and `exclude_type[]` must be the recorded ones.

Because `pp` is volatile, the lookup cannot tell a wrong page size from the
right one. An adapter test should therefore read the URLs it sent from
`OfflineHttpClient.requests` and assert `pp=100` on every request and
`page=1` on the first request of each query.

The source entry and these files can also be checked against each other,
which nothing does automatically yet: every key of `department_fields`
exists (after stripping surrounding spaces) as a name in
`departments-page*.json`, every key of `group_fields` in
`groups-page*.json`, every id in `queries` and `event_types` in
`events-filters.json`, and the `exclude_type_ids` of the untyped query are
exactly the event types of `events-filters.json`.

## What was trimmed or replaced

- `departments-page*.json` and `groups-page*.json`: every item keeps only
  `id`, `name`, `urlname`, `filters` and `localist_url`. Removed from every
  item: `description`, `description_text`, `custom_fields`, `url`, `tags`,
  `photo_id`, `photo_url`, `photo_caption`, `school_id`, `campus_id`,
  `created_at`, `updated_at`. The removed fields hold long descriptions and
  the contact e-mail addresses and telephone numbers of the units, many of
  them personal addresses of staff; no test needs them. The order of the
  items, the `page` object and the escapes of the kept values are as
  recorded.
- The five event pages: meeting passcodes and contact details were
  replaced by placeholders, because this repository is public. In 27 of
  the 188 records (fields `stream_url`, `stream_info`, `description` and
  `description_text`):
  - the value of every `pwd` parameter of a Zoom link is `REDACTED`
    (13 times in 8 records). The parameter itself stays, so these URLs
    still carry a credential as far as a test is concerned:
    `https://stanford.zoom.us/j/<meeting id>?pwd=REDACTED`.
  - every digit of a passcode that is written out in text (`Password:`,
    `Passcode:`, `Password if prompted:`, `(passcode: ...)`) is `0`
    (5 times in 4 records).
  - the local part of every e-mail address is `redacted`
    (`redacted@stanford.edu`; 21 addresses in 19 records, in `mailto:`
    links and in text, one of them percent-encoded inside a link
    protection URL as `redacted%40...`).
  - every digit of the two telephone numbers is `0`.

  Names, meeting ids and everything else are as recorded. Each changed
  value was replaced inside its JSON string, so the escapes (`\u003c`,
  `\u0026`) and the rest of the file are byte for byte the recorded
  answer.
- The two filter files are untouched.

Names in the department list can start or end with a space and one name is
used by two departments (`Center for Conservation Biology`).

## Cases in the event pages

Records are named by the id of their instance
(`events[].event.event_instances[0].event_instance.id`); "types-N" is
`events-types-pageN.json`.

| Case | Instance id | File | Details |
|---|---|---|---|
| All-day, `end` null | 54064922323927 | types-2 | 2026-11-15, an abstract deadline of the Department of Surgery, no location |
| All-day, virtual, two departments | 54225562985263 | types-3 | 2026-11-18, School of Medicine and Stanford Center for Continuing Medical Education |
| All-day, one instance per day | 53922734592729 | types-1 | event 53922734404263 has four instances in the pages (9, 11, 16 and 18 November) |
| Timed, `end` null | 54426626599231 | types-1 | Applied Physics/Physics Colloquium, 2026-11-10 15:30 |
| Hybrid | 51288621937120 | types-1 | Stanford Cancer Institute, 2026-11-10 16:00 to 17:00 |
| Virtual, no location | 53809595903126 | types-1 | Stanford Center for Continuing Medical Education, 2026-11-10 12:00 |
| PhD Defense, in scope, `end` null | 54410800155205 | types-2 | Bioengineering, 2026-11-13 13:00 |
| PhD Defense, out of scope | 54313888154550, 54321881859006 | types-2, types-3 | Civil and Environmental Engineering |
| PhD Defense, non-ASCII name in the title | 54294379845960 | types-3 | Emmett Interdisciplinary Program in Environment & Resources |
| Two departments with different fields | 54047808526631 | types-1 | Wu Tsai Neurosciences Institute and the institute for human-centered AI |
| Three departments, none mapped | 54037513552790 | types-1 | Planetary Science and Exploration Seminar, hybrid |
| Two event types | 53700432901792 | types-1 | Class/Seminar and Lecture/Presentation/Talk, Department of Microbiology and Immunology |
| Out of scope, humanities | 54250837829502 | types-1 | three departments of the humanities |
| Out of scope, policy | 54004138576452, 54391392225789 | types-1 | politics of Turkey, elections in Brazil |
| Cancelled, only visible in the title | 53741880932247, 53741881111471 | types-1, types-3 | title starts with `CANCELED:`, `status` is still `live` |
| `stream_url` with a meeting password | 54004063149104 | types-2 | virtual; also 54365779206181, 54437236133852 (types-3) and 54435582365420 (types-4) |
| Meeting password in `stream_info` (HTML) | 54445649547777, 54435561022415, 54420408876947, 54435582365420 | types-2, types-3, types-3, types-4 | Zoom link with `pwd=` (54445649547777, 54420408876947), `pwd=` as plain text after the link (54435561022415), and a written-out `Password:` / `Passcode:` / `Password if prompted:` (54445649547777, 54435561022415, 54435582365420) |
| Meeting password in the abstract text | 54420408876947, 54294379845960 | types-3 | Zoom link with `pwd=` in `description` and `description_text` of 54420408876947 and in `description` of 54294379845960; `(passcode: ...)` in both fields of 54294379845960 |
| Contact e-mail address and telephone number in the abstract text | 54287642110827 | types-2 | `description` and `description_text`; 18 more records with an address |
| `stream_url` without a password | 54307369440521 | types-1 | hybrid; this record also has the key `groups` |
| No key `departments` | 53809587119189 | types-1 | four more in types-2 and types-3 |
| Single department, in scope | 53984485261396, 54251541115699 | types-1, types-2 | Department of Biology, Department of Mathematics |
| No event type, in scope | 53728348826147, 53727552937890 | untyped-1 | lecture and conference of the Department of Surgery, both hybrid |
| No event type, in scope, nine hours long | 53985402088649 | untyped-1 | a symposium of the Wu Tsai Human Performance Alliance, 09:00 to 18:00, not flagged `all_day` |
| No event type, out of scope, recurring | event 54321173858822 | untyped-1 | a library book display with ten instances |

Counts over all five pages (188 records, 173 distinct events): 7 all-day
records, 25 timed records without an end, 35 hybrid, 42 virtual, 4 PhD
defenses, 100 records without `url`, 24 with `stream_url`.

Not in these pages, but seen in other answers of the API during the
research: an all-day record whose `end` is 23:59 of the same day instead of
null, and titles with HTML entities (`&#039;`, `&amp;`).
