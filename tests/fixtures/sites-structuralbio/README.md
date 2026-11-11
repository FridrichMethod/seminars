# sites-structuralbio

Stanford Structural Biology, https://med.stanford.edu/structuralbio.html
(structuralbio.stanford.edu redirects there). Recorded on 2026-11-11. This site is NOT
a source in `config/sources.d/`: it is a School of Medicine site on Adobe Experience
Manager, not Drupal, and it has no page of events or seminars at all.

| file | request | what |
|---|---|---|
| `index.html` | `/structuralbio.html` | the home page of the department |

## Structure

- The navigation of the department (`nav#navigationLocal`) has Faculty, Education,
  Research, a committee page and About Us. Nothing in it leads to events, seminars or a
  calendar. The only "Events" link of the page is the one in the header of the school,
  `//med.stanford.edu/about/events.html`, which is not about this department.
- The page has no list component for events (`div.events-list`, as on
  `sites-genetics`), no JSON-LD, no feed and no calendar link.
- `/structuralbio/events.html` answered 404 when it was tried on 2026-11-10.
- One card on the list page of `sites-chemh` links to a PDF poster whose file name
  reads "Structural Biology & Molecular Biophysics Seminar Series". A page of that
  series could not be found.

Nothing was changed in the body.

## Status of this directory

No source reads this directory: it documents what the site looks like, nothing
more. Should the site ever become a source, it is recorded again under the id of
that source (a source of the custom group has an id starting with `custom-`), and
this directory is removed.
