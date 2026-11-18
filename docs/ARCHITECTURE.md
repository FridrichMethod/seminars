English | [&#20013;&#25991;](ARCHITECTURE.zh-CN.md)

# Architecture

How the project is built: the parts of the package, what one daily run does, how events
keep their identity, how the LLM layer is used, what the GitHub workflows do, and the
limits that are known. How to subscribe and how to run the project is in the
[README](../README.md), how to add a source in [ADDING_A_SOURCE.md](ADDING_A_SOURCE.md),
and how to set up a language model in [LLM_CREDENTIALS.md](LLM_CREDENTIALS.md). The
docstrings of the modules named below hold the complete rules.

## Overview

There is no server. Once a day a GitHub Actions workflow reads the public pages of every
source, turns them into events, commits the result to `data/` and deploys a static site
with the calendar feeds to GitHub Pages. Once a week a second workflow opens a digest
issue from the committed data. git is the database: a run reads the files of the run
before it and writes new ones, and the daily commit holds only what changed.

```
config/            sources, fields, interests, settings, LLM settings
   |
seminars run       fetch -> parse -> normalize -> filter -> merge -> classify
   |               -> scope -> picks -> persist -> health -> write -> check
   v
data/              events.json, uid_map.json, health.json, archive/, llm_cache/,
   |               last_run.json, run_report.json  (committed)
seminars build-site
   v
public/            index.html, status.html, events.json, status.json, feeds/<id>.ics
                   (deployed to GitHub Pages)
```

## Module map

The functional packages (`pipeline/`, `classify/`, `llm/`, `outputs/`, `sources/`) meet
through the protocols in `seminars.contracts`, which also says which module implements
each one; the pipeline loads the implementations through `load_implementation`. Every
contract has a fake in the tests, so each package is tested on its own. The foundation is
imported directly wherever it is needed: `models/`, `config/`, `contracts/`, the HTTP
modules (`http*.py`, `robots.py`), `fixtures.py` and the text modules (`textrules.py`,
`textio.py`, `i18n.py`). So are a few shared helpers:

- `pipeline/collect.py` takes the adapter registry and its errors from `sources/base.py`,
  and `detail_pages_optional` from `sources/generic/common.py`;
- `sources/generic/common.py` takes `MAX_SPEAKERS` from `pipeline/normalize.py`;
- the LLM page extractor (`sources/generic/llm_page.py`) and the LLM tagger
  (`classify/llm_tagger.py`) take `record_skipped_too_long` from `llm/base.py`, and the
  page extractor also the helpers of `llm/extraction.py`, `llm/grounding.py`,
  `llm/privacy_filter.py` and `llm/secret_filter.py`;
- the command line (`cli.py`) imports the pipeline and `github.py`.

| Part | What it holds |
|---|---|
| `cli.py` | the command line, a Typer application |
| `contracts/` | the protocols between packages and the table of their implementations |
| `models/` | the data model: `SourceConfig`, `EventDraft`, `Event`, run report, health, public payloads |
| `config/` | loading and checking `config/` and the environment; the project paths |
| `http.py`, `robots.py` | the polite live client and the offline client (with the `http*.py` modules) |
| `fixtures.py` | recording, checking and serving fixtures (`python -m seminars.fixtures`) |
| `sources/` | the adapter protocol and registry (`sources/base.py`), `generic/` and `custom/` adapters, the format probe |
| `pipeline/` | one run: collect, normalize, privacy, ids, dedup, tagging, diff, store, checks, publish |
| `classify/` | field rules, the event filter, picks, the LLM tagger |
| `llm/` | the providers, the provider chain, the answer cache, grounding, secret and privacy filters |
| `outputs/` | ICS feeds, site data, the weekly digest, health and its issues |
| `github.py` | the issue client of health and the digest |
| `textrules.py` | the charset rule for text and for `data/` |

Configuration lives in `config/`: `settings.yaml` (time zone, the window of 14 days back
and 120 ahead, HTTP limits, de-duplication and health thresholds), `sources.d/` (one file
per group of sources), `taxonomy.yaml` (the seven fields, exclusions, department labels),
`interests.yaml` (the picks score) and `llm.yaml` (providers, models, caps). An unknown key
is an error everywhere. The texts of the site and the digest are in `locales/en.json` and
`locales/zh-CN.json`.

## One run

`seminars run` (`pipeline/run.py`) fixes the time of the run when it starts: the real time
live, the reference time of the recorded fixtures offline. Every stage is handed that
time, and none reads the clock.

1. **Fetch** (`pipeline/collect.py`). The adapter of each enabled source fetches its pages,
   one worker thread per source and at most 8 at a time. The HTTP client keeps the requests
   to one host serial and paced, so the threads only overlap on different hosts. One source
   may take 15 minutes in all. Whatever goes wrong with a source (a refused host, a used-up
   budget, a page that cannot be parsed, a bug in an adapter) is the failure of that source
   alone.
2. **Parse**. The adapter turns the documents into drafts (`EventDraft`). Parsing happens on
   the calling thread, source after source in the order of the configuration, so the result
   does not depend on which fetch finished first. For a source with `llm_fallback: true` or
   `adapter: llm_page`, the LLM page extractor reads the pages when the rules read nothing.
3. **Normalize** (`pipeline/normalize.py`). Times follow the zone of the source, and events
   that start outside the window are dropped. Every text is cleaned (markup, the charset
   rule, length), loses meeting credentials, e-mail addresses and telephone numbers
   (`pipeline/privacy.py`) and credential-shaped strings (`pipeline/secret_scan.py`). The
   speaker line is split into people (`pipeline/speakers.py`); a draft without speakers gets
   the people its title names in a common form (`pipeline/title_speakers.py`). The event gets
   its source key and its uid (`pipeline/ids.py`).
4. **Filter** (`classify/filters.py`). Phrases of `config/taxonomy.yaml` exclude what is no
   academic talk (receptions, office hours, courses and the like), and some department
   labels are always out of scope.
5. **Merge** (`pipeline/dedup.py`). Stored uids are handed on to keys that changed, and the
   listings of one talk become one event.
6. **Classify** (`pipeline/tagging.py`). Fields come from the source and from the rules
   (`classify/rules.py`); the cases the rules cannot decide go to the LLM tagger when there
   is one (`classify/llm_tagger.py`).
7. **Scope**. An event without any field is excluded as `out_of_scope`.
8. **Picks** (`classify/picks.py`). Keywords, series and speakers of `config/interests.yaml`
   add up to a score; an event at or above the threshold is a pick, with its reasons.
9. **Persist** (`pipeline/diff.py`). The events of the run are compared with the stored ones:
   see "Stored events" below.
10. **Health** (`outputs/health.py`). The outcome of each source and LLM provider is added to
    the history, and the issues `source-broken` and `llm-auth` are opened, updated or
    closed.
11. **Write** (`pipeline/store.py`). The data files first, then the feeds and the site data,
    and the two records of the run (`data/run_report.json`, `data/last_run.json`) last.
12. **Check** (`pipeline/checks.py`). The secret scan and the charset rule of `data/` look at
    the files before they are written and at `data/` and `public/` on disk afterwards.

Exit status of `seminars run`, which the update workflow acts on:

| Status | Meaning | What the workflow does |
|---|---|---|
| 0 | the run succeeded, or a scheduled run found today's work done | commit, build and deploy (nothing when skipped) |
| 1 | every source failed, or an output writer failed (`ok: false` recorded) | commit only health and the records of the run, deploy, fail the workflow run |
| 1 | the run could not start or stopped on an error nothing caught | commit and deploy nothing |
| 2 | a usage error of the command line | commit and deploy nothing |
| 3 | an output check found a secret or a character that breaks the charset rule | commit and deploy nothing |

A run never builds on fixture data: when `data/last_run.json` records an offline run, a
live run starts from no stored events and says so in a warning. `seminars run --dry-run`
does everything in a scratch directory and changes nothing.

## Sources

Sources are configured in `config/sources.d/` and fall into four tiers:

| Tier | Group | What | Adapters |
|---|---|---|---|
| 1 | `localist` | Stanford Events (events.stanford.edu), the campus calendar, through its JSON API | `localist` |
| 2 | `stanford-sites` | department sites on Stanford Sites (Drupal) | `drupal_jsonapi`, `rss` |
| 3 | `stanford-custom`, `researchseminars` | labs, centers and series with pages of their own; series on researchseminars.org | `html_list` and custom adapters |
| Bay Area | `bayarea` | SLAC, CZ Biohub and other Bay Area institutes | `rss`, `html_list`, `drupal_jsonapi`, `slac_drupal` |

The extraction method is chosen in this order: a structured API, a feed, JSON-LD in the
page, an HTML list read with CSS selectors, and only then the LLM page extractor. The two
templates of Stanford Sites differ: one offers Drupal JSON:API (`/jsonapi/node/hs_event`),
the other an RSS feed that serves as the index, with the other fields from the list page
or the detail pages.

An adapter has two steps: `fetch` does all the I/O through the `HttpClient` it is given,
and `parse` is a pure function of what was fetched. The generic adapters (`localist`,
`drupal_jsonapi`, `ics`, `rss`, `jsonld`, `html_list`, `llm_page`) are driven by `params` in
the YAML entry; a custom adapter in `sources/custom/` handles one odd site. Every module in
those two packages registers itself, so adding an adapter touches no shared file. An
adapter only fetches from the hosts of its own source (`url`, `urls`, `homepage` and
`params.extra_hosts`), so links on a page cannot steer requests to third parties.

The polite client (`http.py`) honours robots.txt including `Crawl-delay` and waits at least
one second between two requests to a host, measured from the end of the previous one.
Requests to one host are strictly serial, also between processes that share the HTTP cache
directory. A host gets at most 20 requests per run, plus as many for robots.txt. An origin
whose robots.txt cannot be fetched is skipped for the rest of the run. A source that lists
a host under `ignore_robots` gets a view of the shared client
(`PoliteClient.ignoring_robots_on`) that does not follow the `Disallow` rules of that host;
everything else holds there too, and every other source still follows them. The client only
talks to public addresses: a host name is resolved first, and the connection goes only to
the address that was checked.

Offline, the same adapters read recorded fixtures (`tests/fixtures/<source id>/`) by URL.
The tests and `seminars run --offline` use them and never touch the network.

## Stable ids and de-duplication

A calendar program tells events apart by their UID, so a uid never changes once it was
given out (`pipeline/ids.py`). Every draft gets one source key, from the best identity its
source offers:

1. the native id of the source (Localist instance, JSON:API resource, RSS guid, ICS UID):
   `<source id>:<native id>`;
2. the address of its own detail page, when no other draft of its source points at it in
   the run:
   `<source id>:url:<address>`;
3. the local day of the start plus the first speaker, or else the title, or else the time of
   day: `<source id>:day:<date>:<words>`.

`data/uid_map.json` binds every key ever seen to its uid. A known key gives the same uid in
every run; a new key gets a uid derived from the key alone. When a key changes while the
talk stays (a day key after a move or a new speaker spelling, a renamed page), the stored
uid is handed on to the new key, so the uid stays. In the feeds the UID is
`<uid>@seminars.fridrichmethod.github.io`.

Two listings are one talk (`pipeline/dedup.py`) when they share a uid or a key, or when
their starts are at most 15 minutes apart and their titles are similar (a token set ratio
of at least 85), they share a speaker, the title of one names a speaker of the other, or
they point at the same detail page. Guards keep unrelated talks apart: "TBA" never matches "TBA", a
one-word title is not compared, a page that a source is fetched from or that lists a whole
series is no detail page, and two listings of one source that name different speakers are
never one talk. A merged event keeps the uid seen first, the best title (not "TBA", not the
bare name of the series), the longest abstract, all speakers and keys, and the details of
the host's own listing over those of an aggregator (the campus calendar,
researchseminars.org). The result does not depend on the order of the input.

## Stored events

`data/events.json` holds every event of the current window, one per line, sorted by uid.
When a stored event comes back with a change a reader can see, its `sequence` goes up and
`last_changed` becomes the time of the run; both go into the feeds, so subscribed calendars
update the event. An event that the run did not deliver is judged by what is known about
it: one that started before the window goes to `data/archive/<year>.jsonl`; one of a
disabled or removed source leaves at once; one that has started or is cancelled stays as it
is; one whose sources were all read in full counts a missing run, and it is removed once it
has missed 3 such runs without coming back. A source that failed, came back empty or was not read in full says nothing
about its events, so a broken site never deletes talks. When one source of a merged event
fails, the stored event stands in for its missing listing.

## Classification

Fields (`math`, `cs`, `ai-ml`, `chem`, `bio`, `physics`, `med`) come from three places, in
this order: what the source states (`default_fields` and the department labels of
`config/taxonomy.yaml`), the rules (series names and weighted keywords of each field in the
title, the series and the talk part of the abstract), and, for the events the rules cannot
place, the LLM tagger. Terms match whole words after folding accents and case. The abstract
alone never gives a field to an event without source fields, because campus-wide abstracts
mention fields in passing. Tags chosen by whoever posted an event are never facts. An event
without a field is out of scope and stays out of the feeds; the run report lists it.

Picks are a score of `config/interests.yaml`: keywords weighted by where they appear, a bonus
for listed series and speakers, penalties for excluded terms. The reasons are stored with
the event and shown in the feeds, on the site and in the digest.

## LLM layer

An LLM is used for two jobs only, and the pipeline runs completely without one: tagging the
fields of the events that the rules cannot decide, and reading pages without structure.

**The provider chain** (`llm/resolve.py`, `llm/chain.py`). The default order is `claude-cli`
(a Claude subscription token), `codex-cli` (the local ChatGPT login; only when named in
`LLM_PROVIDERS`, never in GitHub Actions, and only for tagging), `anthropic`, `openai`,
`openai-compat` (API keys). A provider joins when it is configured: its credential is set
and, for an API, a model is named (for `openai-compat` also its address). `LLM_PROVIDERS`
reorders and selects. A request goes to the first provider that is still usable; a
provider that fails (credential, quota, rate limit, timeout, network) is passed over for
the rest of the run, and after the last one the run goes on with the rules only. The first request of a run is the probe of the chain; a run
that sends nothing sends one probe, so an expired credential shows in health on a quiet day
too. A run sends at most `max_calls_per_run` requests (60) and no prompt longer than
`max_input_chars`.

**Tagging** (`classify/llm_tagger.py`). Events whose source states fields are asked in
batches; an event without them is asked alone, so no other event shares the request that
decides its fields. The answer is a closed vocabulary (field ids and a yes or no for "is
this a talk"), validated against a schema, so page text cannot make it say anything else.

**Page reading** (`sources/generic/llm_page.py`). The page is reduced to its visible text with
numbered link markers and sent in pieces between marker lines that the page cannot spell.
Every value of the answer must occur in the page text (`llm/grounding.py`), and what is kept
is the text of the page, never the words of the model; links are marker numbers, dates and
times must appear in the text.

**Stored answers** (`llm/cache.py`). Every answer that is used is stored in
`data/llm_cache/<2 hex digits>/<key>.json`. The key is made of the prompt version, the task
and the input text; neither the model nor the provider is part of it, so a new credential
or model does not ask again. A file holds the answer and the UTC day it was last used; that
day is rewritten only when it is older than 30 days, and an entry unused for
`cache_ttl_days` (180) is pruned. Without any provider, the stored answers still apply and
nothing new is asked. Stored answers of page reading hold no private data and no secret: the
text is scrubbed before the answer is grounded, and an entry that cannot be cleaned is
replaced by an empty placeholder.

## Outputs

- **Feeds** (`outputs/ics.py`): ten calendars, `all`, `picks`, one per field and
  `conferences`. `conferences` holds the meetings (`Event.is_meeting`: a conference, or a
  workshop over several days or of at least three hours), which are in `all` and in their
  field feeds as well, so the download of selected talks reads `all` alone. A timed
  event is written in the zone of the settings with one VTIMEZONE sub-component per change
  of the offset; a cancelled talk stays with `STATUS:CANCELLED`. The bytes depend only on
  the events, so a calendar sees a change only when an event changed. `seminars validate`
  checks every feed with a validator of its own.
- **Site** (`site/`, `outputs/site_data.py`): static HTML and JavaScript without a build
  step, reading `events.json` and `status.json`; English and Chinese from `locales/`. All
  text from the web is escaped. `seminars build-site` assembles `public/` from the committed
  data alone.
- **Download of selected talks** (`site/calendar.js`): the talks a reader ticks in the list
  are page state held in memory, never written to the address or to storage. The selection
  bar above the list is a region landmark; while talks are selected and the bar is
  scrolled up out of view (an IntersectionObserver whose area reaches far below the
  viewport, so a bar still below the screen counts as on screen), a strip at the bottom of
  the list keeps a second download button in reach and shows a plain-text copy of the
  status of the bar (the count, and how many selected talks the filters hide) and of the
  note on a download that is being prepared, failed or left talks out (the status and the
  note of the bar stay the ones that are announced). The note of the strip opens above its
  count and its button, so the button stays where it was pressed: at the bottom of the
  screen the strip grows upward over the list, and below the last talk the page scrolls by
  the height of the note. The note of the bar on the one-time
  copy links to the subscription panel; the link moves the focus to the heading of the
  panel and adds a history entry for the same address, so Back returns to the list, and
  Back and Forward show the filters of the address of each entry. A page opened at
  `#subscribe` (the link opened in another tab) shows the panel the same way once the list
  is drawn, and the fragment then leaves the address. On download the page
  fetches `feeds/all.ics` from its own site (the path of the feed `all` in
  `events.json`) and copies the VEVENT blocks of the selected talks verbatim, in the order
  of the feed, into one calendar, together with every VTIMEZONE block and the basic header
  of the feed (`VERSION`, `PRODID` and, when present, `CALSCALE`, `METHOD` and
  `X-WR-TIMEZONE`), but without the header properties that only a subscription needs
  (`X-WR-CALNAME`, `X-WR-CALDESC`, `REFRESH-INTERVAL`, `X-PUBLISHED-TTL`). A talk is the
  VEVENT whose UID, up to its last `@`, is the uid of the talk. The calendar is saved
  through a Blob as `seminars-selected.ics`; selected talks missing from the feed are left
  out and counted, and a feed that cannot be fetched or read, or that holds none of the
  selected talks, gives no file. No ICS is written in JavaScript, and neither the backend
  nor `events.json` has anything of its own for the download: the file is made from the
  feed that `outputs/ics.py` writes.
- **Digest** (`outputs/digest.py`): the talks of the week (Monday to Sunday), the picks first,
  then one table per field, the talks new since the previous issue and the state of the
  sources, with every label in English and Chinese. No state is kept: "new" is measured from
  the time the previous digest issue was opened, and an issue with the title of this week
  is looked for first.
- **Health** (`outputs/health.py`): the last 30 runs of each source and provider in
  `data/health.json`. A source that failed 3 runs in a row, that delivered talks and then
  came back empty 3 runs in a row, or that was not read in full 3 runs in a row gets a
  `source-broken` issue, closed again when it recovers. An LLM provider that failed 2 runs
  in a row gets an `llm-auth` issue that names the secret to replace.

## Privacy and secrets

`data/` and the site are public. Normalization therefore removes meeting passwords and
passcodes (in links and written out), e-mail addresses and telephone numbers from every
text, keeps a meeting link without its credential, and drops a stream address that holds
one. The rules lean towards removing too much. A secret scan (GitHub, Anthropic and OpenAI
tokens, bearer tokens, JSON web tokens, private keys, and the exact value of every
credential of the process) redacts what a page shows and stops a run that is about to
publish one, with exit status 3. Recorded fixtures follow the same rules before their first
commit (`tests/fixtures/README.md`).

## GitHub workflows

Every action is pinned to the commit SHA of a release, and the Claude Code CLI to the
version in `config/llm.yaml`. No workflow that sees a credential runs for a pull request.

**`ci.yml`**: on pushes to `main`, pull requests and by hand. Read-only token, no credential,
the checkout keeps no token. Jobs:

- `checks`: `uv sync --locked`, `scripts/check_ascii.py`, ruff, the formatter, pyright, the
  tests with coverage (offline), actionlint;
- `browser`: the Playwright smoke tests of the site in Chromium.

**`update.yml`**: every day at 04:23 Pacific time, a catch-up at 10:23, and by hand with the
mode `full` or `llm-smoke`. A scheduled run passes `--skip-if-done-today`, so the catch-up
does nothing after a successful run of the same day. All runs share one concurrency group,
so two never run at the same time. A run in progress is never cancelled; GitHub keeps one
waiting run per group, so a newer run replaces one that is still waiting. The top-level
token is read-only, and each job gets only what it needs:

| Job | Permissions | What it does |
|---|---|---|
| `fetch` | `contents: read`, `issues: write` | runs the pipeline with the LLM credentials, validates, uploads `data/` as an artifact |
| `commit` | `contents: write` | commits that artifact as `github-actions[bot]`; installs nothing and runs only git |
| `build` | read only | builds the site from the new tip of `main` and validates it |
| `deploy` | `pages: write`, `id-token: write` | deploys the site to GitHub Pages |
| `outage` | read only | fails the workflow run after a run that recorded itself as failed |
| `llm-smoke` | `contents: read` | probes every configured provider; commits and deploys nothing |

The LLM credentials reach only the step that runs the pipeline and the smoke step. The
project is installed by a step of its own before a step that holds a credential, and the
job that can push runs no project code.

**`digest.yml`**: Monday at 07:07 Pacific time, a catch-up on Tuesday, and by hand (with
`dry_run` to print the digest instead). One job with `contents: read` and `issues: write`;
it reads the committed data, fetches nothing and uses no LLM.

## Known limits

- **Sources that are not collected.** Bio-X answers every automated request with HTTP 403
  (a browser challenge), so its source is turned off. The Information Theory Forum is
  turned off: it is nearly dormant, and its list gives no year. The SAIL events calendar
  has had no talk since 2023, and the Theory Seminar does not meet regularly (the other
  calendars on its page are those of series that are collected from their own pages).
  The SLAC Advanced Instrumentation Seminars and the Stanford algebraic geometry seminar
  are dormant, and SUNCAT lists only internal group meetings. The status page says why
  each source is off. Arc Institute has no public seminar calendar. The department sites of
  Bioengineering and Computer Science are off because their events are imports from the
  campus calendar, which is collected anyway (and, for Computer Science, advising
  sessions). Talks outside the seven fields (for example
  earth and planetary sciences, humanities) are out of scope by design.
- **Hosts that GitHub's runners may not reach.** The robots.txt of the DBDS workshop and of the
  NLP seminar can time out from GitHub's runners while it answers from Stanford's network.
  An unreachable robots.txt makes the source fail for that run; after 3 such runs the source
  gets a `source-broken` issue.
- **Speakers and affiliations from free text** favour precision: a missing speaker is
  preferred to a wrong one. The forms that are read, and the narrow cases that give no
  speaker or a wrong one, are listed in the docstring of `pipeline/title_speakers.py` and in
  the comment of `sites-systemx` in `config/sources.d/stanford-sites.yaml`.
- **Field tagging without an LLM** is rules only. Of the talks in the seven fields that the
  campus calendar lists, the rules recognise about 75 percent; with the LLM tagger the
  measured share was about 96 percent. A talk the rules cannot place is out of scope until
  an LLM tags it.
- **The LLM cap.** A run sends at most `max_calls_per_run` (60) requests, and every event the
  rules cannot place takes a request of its own, so an empty cache fills over a few runs.
- **Duplicates of talks without an id.** A talk that its source lists without a native id or
  a page of its own is recognised by its day, people and title. When it moves and changes
  its title or speakers in the same update, moves by more than 60 days, or two similar talks
  of one speaker change at once, a calendar can show it twice for up to two days.
- **A talk that comes back.** A talk that left the feeds and returns keeps its uid but starts
  again at `SEQUENCE:0`; calendar programs refresh a subscribed event by its UID.
- **Heuristics with documented edges.** The privacy rules remove some text that is no address
  (a module path at a `.org` host) and keep a few that may be one; the de-duplication rules
  can merge two talks of one source at one time for a run while one of them names no
  speaker yet. The docstrings of `pipeline/privacy.py` and `pipeline/dedup.py` list the
  cases. The secret scan takes a generic token only when it looks generated, so a long
  token of letters only can pass; the credentials of the process itself are matched
  exactly.
- **Networks with a proxy.** The live client ignores proxy and certificate settings of the
  environment and connects directly, so a live run needs a network without a mandatory
  proxy.
