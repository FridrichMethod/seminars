# sites-genetics

Stanford Genetics, https://med.stanford.edu/genetics.html (genetics.stanford.edu does
not answer). Recorded on 2026-11-11. This site is NOT a source in
`config/sources.d/`: it is a School of Medicine site on Adobe Experience Manager, not
Drupal, and it has no list of its own seminars.

| file | request | what |
|---|---|---|
| `events.html` | `/genetics/events.html` | the page "Upcoming Events" |
| `events-cig.html` | `/genetics/events/CIG.html` | the page of the seminar "Current Issues in Genetics (CIG)" |

## Structure

- `events.html` has two hand-made panels (a certificate course and a "Careers in
  Science" event, both with Eventbrite links) and one list component:
  `div.events-list ul.events li.eventslist-item`, each with `span.compact-date`
  (`November 13, 2026`, a date without a time), `a` around
  `span.compact-event-title`, and sometimes `span.compact-event-subtitle` (a speaker
  line or free text). The 5 items are events of OTHER departments of the school
  (Immunology conference, an Otolaryngology seminar, an ophthalmology conference);
  none is a genetics seminar. A second `div.events-list` on the page is empty.
- `events-cig.html` says only "Fridays, 4:00 to 5:00 PM followed by Happy Hour". It
  has no schedule and no speakers.
- No JSON-LD, no feed, no calendar link on either page.

One file was renamed after recording (`events.html`). Nothing was changed in the
bodies.

## Status of this directory

No source reads this directory: it documents what the site looks like, nothing
more. Should the site ever become a source, it is recorded again under the id of
that source (a source of the custom group has an id starting with `custom-`), and
this directory is removed.
