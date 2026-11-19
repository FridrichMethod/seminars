# Changelog

All notable changes of this project are recorded in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the version numbers follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## v0.4.0 - 2026-11-18

### Added

- **Conferences and workshops.** A meeting is a conference, an all-day workshop of two
  days or more, or a workshop with times of day that lasts at least three hours; a
  workshop listed all day on a single day, such as one day of a hackathon, is no meeting
  and is listed as a talk. A source with `meeting_horizon: true` keeps its meetings up
  to 365 days ahead (`meetings.days_ahead`), while talks keep the window of 120 days. An
  event that a calendar types as a seminar, lecture or other becomes a conference or a
  workshop when its title names one (symposium, conference, summit, convening, congress,
  retreat; workshop, boot camp, summer, winter or spring school) and it is all day or
  lasts at least three hours; such a workshop is a meeting only by the rule above. A
  timed meeting of more than a week is kept over its days as an all-day event.
- **Feed `conferences.ics`** with the conferences and workshops. They stay in `all.ics`,
  in the picks and in the field feeds as well.
- **Kind filter** on the web site (all, talks, conferences and workshops), kept in the
  address like the other filters. A meeting in progress is listed once, under Ongoing,
  and the full-text search also reads the location.
- **Conferences and workshops in the weekly digest**, in a section of their own; they
  leave the picks and field tables of the digest.
- **Registry of meetings** (`conf-registry`, `config/sources.d/conferences.yaml`, group
  `conferences`): Bay Area conferences and workshops that no source carries, kept by
  hand with the facts of the organizer's page of each edition (dates, venue and the
  dates of its deadlines, which the event lists). It sends no request. A test fails
  when an edition that starts within 120 days was checked more than 90 days before,
  and another one keeps the sites of conference aggregators out of every source.
- **Workshops of the Simons Institute** (`bayarea-simons-workshops`): workshops, boot
  camps and symposia of the Upcoming tab, a year ahead.
- **SLAC workshops and schools** (`bayarea-slac-workshops`) from the iCalendar feeds of
  five Indico categories, a year ahead; robots.txt of indico.slac.stanford.edu is set
  aside for this source as for the SLAC seminars.
- The AIMI Symposium (`sites-aimi`) and the conferences of HAI (`custom-hai-events`) are
  kept a year ahead.
- **Meetings of the owner's fields as picks**: the series Protein Society, RosettaCon,
  Rosetta Commons, BayLearn, MLSB, MLBio, RECOMB, ISMB, ICLR, Enzyme Engineering and
  Generative and Experimental Perspectives (GEM), and the keywords enzyme engineering and
  biomolecular design. The taxonomy places NeurIPS, ICML and ICLR in ai-ml and RECOMB,
  ISMB, Protein Society and RosettaCon in bio.
- **Deadlines feed `deadlines.ics`**: the dates of the meetings in the registry
  (abstracts, posters, short talks, scholarships, applications, registration), one
  all-day event on the day the organizer's page states, with the stated time and zone in
  the summary (`ICLR 2027: Workshop paper notification deadline (23:59 AoE)`), marked as
  free time. It is kept by hand: registry entries outside the Bay Area (`region: world`)
  carry deadlines only, and no main-track deadline of a machine-learning or
  computer-science conference is entered (those stay with ccfddl). The first seeds are
  ML-bio, comp-bio, protein-design and enzyme meetings of 2027 (Keystone, Gordon, PSB,
  RECOMB, ISMB/ECCB, EMBO-EMBL, RosettaCon) and the SLMath summer graduate schools.
  `data/deadlines.json` keeps each deadline's `SEQUENCE`, which goes up when a fact the
  feed shows changes; an edition may set a `short_title` for its name in the feed. A
  milestone may name the page that states it (`url`), which the feed and the digest link
  for checking its date.
- **Deadlines in the weekly digest**: a section of the deadlines of the 21 days from the
  Monday of the issue. The subscription panel of the site lists the deadlines feed.
- `allow_empty` for sources of the `ics` adapter: a run in which every feed is a
  calendar without an event is healthy.
- The READMEs point readers of ML and CS conference deadlines to the calendar of the
  ccfddl project.

### Changed

- An all-day event is marked as free time in every feed (`TRANSP:TRANSPARENT` and
  `X-MICROSOFT-CDO-BUSYSTATUS:FREE`), so a conference of a week does not block the
  calendar.
- Events are kept in the window, and archived, by their end: a workshop that began
  before the window and still runs stays.
- Merging of listings: a meeting joins another listing on its title only when the
  titles are equal (so ICML 2027 and ICLR 2027 stay apart, and a symposium stays apart
  from a talk that is part of it); an all-day meeting joins the listing of one of its
  days that links the same page and gives its hours; a merged event is a meeting when
  one of its listings is. Neither the front page of a site nor the page of a series
  that a meeting links is used as a key.
- The descriptions of the picks and field feeds say that they hold meetings too.
- The web site speaks of events where it lists talks, conferences and workshops
  together: the title of the page, the navigation, the heading and the count of the
  list, the label of the filters and the count of the status page; its tagline names
  conferences and workshops.
- `seminars validate` requires `public/feeds/conferences.ics` and
  `public/feeds/deadlines.ics`, and checks the feed of the deadlines by its own rules as
  well.
- Free-form times that name an offset from UTC that no time zone has ("UTC-13") or an
  offset and a zone that differ ("4 PM UTC-8 (EST)") make the event invalid; dateutil
  read the first as 13 hours ahead of UTC and the second as Eastern time.

### Fixed

- Free-form times: "AoE" and "Anywhere on Earth" are read as UTC-12 instead of making
  the event invalid, "UTC-8" and "GMT+1" are read with the sign people write (dateutil
  read them the other way round, 24 hours off at UTC-12), and CET, CEST, BST, EET and
  EEST are read as their regions. A date of an RSS or Atom feed with such a zone is read
  the same way; it had been read as the time of the source's zone, and "11:59 pm" as
  11:59.
- The booth of an exhibitor at a meeting ("Biohub at NeurIPS 2026", Booth 72) is left
  out as no talk; it had been listed in the bio feed.
- `html_list` dates of a range (dates read without `date_format`): a first day without a
  year takes the year that the last day states, or the year before when its month comes
  later ("Dec. 28 - Jan. 3, 2027" starts on 2026-12-28), and a last day without a year
  the year that the first day states, or the year after when its month comes earlier
  ("Dec. 28, 2026 - Jan. 3" ends on 2027-01-03).

## v0.3.0 - 2026-11-16

### Added

- **Why a source is off.** The status page shows the reason of every disabled source
  ("Why it is off"), in English and Chinese, from `site.status.disabled_reasons.<id>` in
  the locale files; a test requires a reason for every disabled source and for no other.
- **`ignore_robots`** in a source entry: hosts of that source whose robots.txt
  `Disallow` rules it does not follow. The `Crawl-delay`, the pause between requests and
  the request budgets still apply, and other sources on the same host still follow
  robots.txt. `seminars sources check` and `python -m seminars.fixtures record` follow
  it as a run does.
- **SLAC seminars from Indico**: the SLAC Theory Group seminars (`bayarea-slac-theory`)
  are collected again and the SLAC Experimental Seminar Series
  (`bayarea-slac-experimental`) is new, both from the iCalendar feed of their Indico
  category, with speaker, affiliation, room and abstract (the Zoom address with its
  meeting password left out); robots.txt of indico.slac.stanford.edu is set aside for
  these two sources.
- **Calendar of Chemical and Systems Biology** (`custom-csb-calendar`): the department's
  public Google Calendar, with the Science Circle talks (speaker and lab), the special
  seminars before the department posts them, and the speakers named in the titles;
  robots.txt of calendar.google.com is set aside for this source. Talks that the
  department's page and ChEM-H also list merge with them. Birthday, promotion and
  thesis celebrations, decoration parties, department-wide receptions and
  Friendsgiving are left out as social events.

### Changed

- The SAIL events and the Theory Seminar stay off for what their calendars hold, not
  for robots.txt: the SAIL calendar has had no talk since 2023, and of the four
  calendars of the Theory Seminar two are dormant and two belong to series that are
  collected from their own pages.

## v0.2.0 - 2026-11-14

### Added

- **Download of selected talks** on the web site: every listed talk has a checkbox, and a
  bar above the list offers Select all shown, Clear selection and Download selected
  (.ics). While talks are selected and the bar is scrolled up out of view, a strip at the
  bottom of the list keeps the download button in reach and repeats what the bar says
  about the selection, the talks the filters hide included, and the note on a download
  that is being prepared, failed or left talks out; the note of the bar on the one-time
  copy links to the subscription panel. The download saves
  `seminars-selected.ics`, a calendar for a one-time import with the selected talks, also
  those that the filters hide; `site/calendar.js` copies their events unchanged, with the
  same UIDs, from `feeds/all.ics`. The selection is held in memory only. The README says
  how to import the file in Apple Calendar, Google Calendar and Outlook, and that a
  subscription, unlike the file, receives later changes.

## v0.1.0 - 2026-11-13

The first release: academic seminars of Stanford, SLAC, CZ Biohub and other nearby
institutes, collected every day from public pages and published as calendar feeds, a
static web site and a weekly digest. How it is built is described in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

### Added

- **Calendar feeds.** Nine ICS feeds at
  `https://fridrichmethod.github.io/seminars/feeds/<id>.ics`: `all`, `picks` and one per
  field (`math`, `cs`, `ai-ml`, `chem`, `bio`, `physics`, `med`). Every event keeps a
  stable UID across title, time and speaker changes and raises its `SEQUENCE` when a
  reader-visible value changes; cancelled talks stay with `STATUS:CANCELLED`; a talk
  leaves the feeds 14 days after it took place. Every feed is checked by
  `seminars validate`.
- **Web site** on GitHub Pages: the upcoming talks in time order, filters by field and
  source, full-text search, English and Chinese, and a status page with the last run and
  the state of every source. Static files, no build step.
- **Weekly digest**: a GitHub issue with the label `digest` every Monday morning (Pacific
  time) with the picks, one table per field, the talks new since the previous digest and
  the state of the sources, in English and Chinese. It is idempotent per week and closes
  the previous digest.
- **Sources**: the Stanford Events calendar (Localist), department sites on Stanford Sites
  (Drupal JSON:API and RSS), labs, centers and series with pages of their own,
  researchseminars.org, SLAC, CZ Biohub and other Bay Area institutes. Generic adapters
  `localist`, `drupal_jsonapi`, `ics`, `rss`, `jsonld`, `html_list` and `llm_page`, custom
  adapters for single sites, and a source format probe (`seminars add-source`). Adding a
  generic source takes a YAML entry ([docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md)).
- **Polite HTTP client**: robots.txt and `Crawl-delay` honoured, one request at a time per
  host (also across processes), a request budget per host and run, public addresses only,
  bounded response sizes and times. Recorded fixtures of every source for offline runs
  (`seminars run --offline`) and tests.
- **Pipeline** (`seminars run`): normalization of times and text, removal of meeting
  credentials, e-mail addresses and telephone numbers, de-duplication across sources,
  stable ids kept in `data/uid_map.json`, change tracking with an archive, health history
  with `source-broken` issues, and output checks (secret scan and charset rule) that stop
  a run from committing anything they flag.
- **Classification**: field rules from `config/taxonomy.yaml`, filters for what is no
  academic talk, and picks scored from `config/interests.yaml` with their reasons.
- **Optional LLM layer** for field tagging and for pages without structure, with a
  provider chain of `claude-cli`, `codex-cli` (local only), `anthropic`, `openai` and
  `openai-compat`, a cap on requests per run, stored answers in `data/llm_cache/`,
  grounding of extracted text in the page, `llm-auth` issues, `seminars llm-smoke` and
  `scripts/set-llm-credential.sh` for changing credentials without code changes
  ([docs/LLM_CREDENTIALS.md](docs/LLM_CREDENTIALS.md)). Everything works without it.
- **GitHub workflows**: `ci.yml` (charset rule, ruff, pyright, tests with coverage,
  actionlint, browser smoke tests), `update.yml` (daily fetch, commit, build and deploy in
  separate jobs with the least permissions each, a catch-up run, and an `llm-smoke` mode)
  and `digest.yml` (weekly digest). Every action is pinned to a commit SHA.
- **Command line**: `seminars run`, `seminars validate`, `seminars build-site`,
  `seminars digest`, `seminars llm-smoke`, `seminars sources` and `seminars add-source`.
- **Documentation** in English and Chinese: the README, the source guide, the LLM
  credentials guide and the architecture.
