# Changelog

All notable changes of this project are recorded in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the version numbers follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
