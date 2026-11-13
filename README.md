English | [&#20013;&#25991;](README.zh-CN.md)

# seminars

Academic seminars, talks and colloquia at Stanford, SLAC, CZ Biohub and other
nearby institutes, collected every day from public web pages and published in
three forms:

- **calendar feeds** (ICS) that calendar apps subscribe to: one per field, one of
  personal picks and one with everything;
- a **static web site** with the upcoming talks, filters by field and source, and
  full-text search;
- a **weekly digest**, a GitHub issue opened every Monday morning (Pacific time).

Everything runs on GitHub Actions and GitHub Pages; there is no server. The data is
kept in this repository (`data/`), and every daily update is one commit.

## What you get

### Calendar feeds

Nine feeds. A talk can be in several field feeds; `all` holds every talk that belongs
to at least one field.

| Feed | Contents | Subscribe |
|---|---|---|
| `all` | Every talk collected from Stanford, SLAC, CZ Biohub and nearby institutes | [all.ics](https://fridrichmethod.github.io/seminars/feeds/all.ics) |
| `picks` | Talks that match the personal interest rules (`config/interests.yaml`) | [picks.ics](https://fridrichmethod.github.io/seminars/feeds/picks.ics) |
| `math` | Pure mathematics, applied mathematics and statistics | [math.ics](https://fridrichmethod.github.io/seminars/feeds/math.ics) |
| `cs` | Computer science | [cs.ics](https://fridrichmethod.github.io/seminars/feeds/cs.ics) |
| `ai-ml` | Artificial intelligence and machine learning | [ai-ml.ics](https://fridrichmethod.github.io/seminars/feeds/ai-ml.ics) |
| `chem` | Chemistry and chemical engineering | [chem.ics](https://fridrichmethod.github.io/seminars/feeds/chem.ics) |
| `bio` | Biology, bioengineering, structural biology and genetics | [bio.ics](https://fridrichmethod.github.io/seminars/feeds/bio.ics) |
| `physics` | Physics and applied physics | [physics.ics](https://fridrichmethod.github.io/seminars/feeds/physics.ics) |
| `med` | Medicine and biomedical research | [med.ics](https://fridrichmethod.github.io/seminars/feeds/med.ics) |

Each feed is the file `feeds/<feed>.ics` of the web site; the table links its
`https://` address, and the section Subscribe lists its `webcal://` address. Every
event keeps a stable identifier, so a changed title or time updates the event in your
calendar instead of adding a second one. Talks that a source lists without an id or a
page of their own are recognised by their day, people and title. When such a talk moves
and changes its title or speakers in the same update, moves by more than 60 days, or two
similar talks of one speaker change at once, your calendar can show it twice for up to
two days.

### Web site

The site at
[https://fridrichmethod.github.io/seminars/](https://fridrichmethod.github.io/seminars/)
lists the upcoming talks in time order, with filters by field and by source and a
full-text search. It is available in English and Chinese. Every listed talk has a
checkbox, and the ticked talks can be saved as one calendar file: see the section
Download selected talks. Its
[status page](https://fridrichmethod.github.io/seminars/status.html) shows when the last
update ran and how each source did.

### Weekly digest

Every Monday morning a new issue with the label `digest` lists the talks of that week
(Monday to Sunday) and what was added since the previous digest; the previous digest
issue is closed.

## Subscribe

Use the address of a feed with `webcal://` in place of `https://`; most calendar apps
then subscribe instead of importing once. GitHub does not turn `webcal://` addresses
into links, so here they are to copy:

```
webcal://fridrichmethod.github.io/seminars/feeds/all.ics
webcal://fridrichmethod.github.io/seminars/feeds/picks.ics
webcal://fridrichmethod.github.io/seminars/feeds/math.ics
webcal://fridrichmethod.github.io/seminars/feeds/cs.ics
webcal://fridrichmethod.github.io/seminars/feeds/ai-ml.ics
webcal://fridrichmethod.github.io/seminars/feeds/chem.ics
webcal://fridrichmethod.github.io/seminars/feeds/bio.ics
webcal://fridrichmethod.github.io/seminars/feeds/physics.ics
webcal://fridrichmethod.github.io/seminars/feeds/med.ics
```

A talk that belongs to several fields is in the feed of each of them, and a calendar
app shows a talk once per feed: subscribed to two of those feeds, it shows the talk
twice. To see every talk once, subscribe to `all` alone (or to `picks` alone). A feed
keeps a talk until 14 days after it took place and then drops it, so the talk also
leaves your calendar.

### Apple Calendar

1. In Calendar on a Mac, choose File > New Calendar Subscription.
2. Paste the `webcal://` address of the feed and click Subscribe.
3. Set Location to iCloud, so that your iPhone and iPad get the calendar too, and
   choose how often it refreshes; once a day is enough, because the data changes
   once a day.

### Google Calendar

Open Other calendars > From URL on calendar.google.com and paste the `https://`
address of the feed. Google refreshes subscribed calendars on its own schedule,
which can take several hours.

### Other calendar apps

Any app that can subscribe to an ICS address works. Importing the file once also
works, but then later changes do not arrive.

## Download selected talks

The web site can also save chosen talks as one calendar file to import once. Tick the
talks you want in the list, or click Select all shown to tick every talk that the list
shows with the current filters and search. The bar above the list says how many talks
are selected and how many of them the filters hide. While talks are selected and the bar
is scrolled away, a strip at the bottom of the list keeps their count and the download
button in reach. The selection stays while you filter, search or switch the language;
Clear selection empties it, and so does reloading the page. A selected talk that has
ended leaves the selection the next time the list is updated. Download selected (.ics)
saves the file `seminars-selected.ics` with every selected talk, those that the filters
hide included. Its events are those of the feed `all`, copied unchanged, with the same
UIDs. When the page cannot read the feed, or the feed holds none of the selected talks,
it saves no file and says so; when some selected talks are missing from the feed, the
file holds the others and the page says how many were left out.

The file is a copy made once: when a talk later moves to another time or is cancelled,
a calendar that imported the file does not learn of it. A subscription (see Subscribe)
stays current; the note of the bar that says so links to the subscription panel of the
site.

To import the file:

- Apple Calendar: open the file, or choose File > Import, and choose the calendar that
  gets the talks.
- Google Calendar on the web: open Settings > Import & export, choose Import, select the
  file and the calendar, and click Import.
- Outlook: open the file, or import it as a calendar.

## Coverage

The numbers below are measured on the first public run, which started on 2026-11-12 at
23:06 Pacific time. "Next four weeks" counts the talks that had not ended by then and
start within 28 days of it. The status page of the web site always shows the current
state of every source.

### Sources and their state

32 of the 42 configured sources are enabled. In the first public run 29 of them delivered
talks, 1 was empty and 2 failed, and 348 talks were stored. A talk that several sources
list counts once for each of them in this table. "Extraction" is how the run read a
source: api from a JSON interface of the site, feed from its RSS or iCalendar feed, html
from the page itself by rules; for a source that failed or was empty, it is the method of
its adapter.

| Source | Group | State | Next four weeks | Extraction |
|---|---|---|---:|---|
| Stanford Events (`localist-stanford`) | `localist` | ok | 43 | api |
| Stanford AIMI (`sites-aimi`) | `stanford-sites` | ok | 3 | feed |
| Stanford Biochemistry (`sites-biochemistry`) | `stanford-sites` | ok | 3 | feed |
| Stanford Biology (`sites-biology`) | `stanford-sites` | ok | 4 | api |
| Stanford Chemical Engineering (`sites-cheme`) | `stanford-sites` | ok | 2 | feed |
| Stanford ChEM-H (`sites-chemh`) | `stanford-sites` | ok | 4 | feed |
| Stanford Chemistry (`sites-chemistry`) | `stanford-sites` | ok | 6 | api |
| Stanford ICME (`sites-icme`) | `stanford-sites` | ok | 2 | feed |
| Stanford Mathematics (`sites-mathematics`) | `stanford-sites` | ok | 20 | api |
| Stanford Mathematics Research Center (`sites-mrc`) | `stanford-sites` | ok | 0 | api |
| Stanford Materials Science and Engineering (`sites-mse`) | `stanford-sites` | ok | 2 | feed |
| Stanford Physics (`sites-physics`) | `stanford-sites` | ok | 2 | api |
| Stanford Statistics (`sites-statistics`) | `stanford-sites` | ok | 6 | api |
| Stanford SystemX Alliance (`sites-systemx`) | `stanford-sites` | ok | 1 | feed |
| AI + Data for Science Seminar (`custom-ai4science`) | `stanford-custom` | ok | 2 | html |
| Stanford Chemical and Systems Biology Events (`custom-csb-events`) | `stanford-custom` | ok | 2 | html |
| Stanford Workshop in Biomedical Data Science (`custom-dbds-workshop`) | `stanford-custom` | failed: robots.txt timed out from GitHub | 0 | html |
| Stanford HAI Events (`custom-hai-events`) | `stanford-custom` | ok | 3 | html |
| Stanford ISL Colloquium (`custom-isl-colloquium`) | `stanford-custom` | ok | 2 | html |
| Stanford MLSys Seminar (`custom-mlsys-seminar`) | `stanford-custom` | empty: every talk on its page is past | 0 | html |
| Stanford NLP Seminar (`custom-nlp-seminar`) | `stanford-custom` | failed: robots.txt timed out from GitHub | 0 | html |
| Stanford RAIN/OR Seminar (`custom-rain-seminar`) | `stanford-custom` | ok | 3 | html |
| Stanford Synthetic Biology Events (`custom-synbio-events`) | `stanford-custom` | ok | 1 | html |
| Stanford CS Theory Lunch (`custom-theory-lunch`) | `stanford-custom` | ok | 3 | html |
| Topos Institute Colloquium (`rsorg-topos-colloquium`) | `researchseminars` | ok | 3 | feed |
| Biohub events (`bayarea-biohub`) | `bayarea` | ok | 1 | html |
| Condensed Matter Physics Seminars (GLAM) (`bayarea-glam`) | `bayarea` | ok | 3 | feed |
| KIPAC Seminars and Events (`bayarea-kipac`) | `bayarea` | ok | 12 | feed |
| Leinweber Institute for Theoretical Physics (LITP, formerly SITP) seminars (`bayarea-litp`) | `bayarea` | ok | 4 | api |
| SLAC Photon Science Seminar Series (Stanford PULSE Institute) (`bayarea-pulse`) | `bayarea` | ok | 1 | feed |
| Q-FARM Seminars and Events (`bayarea-qfarm`) | `bayarea` | ok | 5 | feed |
| SLAC Colloquium Series (`bayarea-slac-colloquium`) | `bayarea` | ok | 3 | html |

### Talks per feed

"In the feed" counts every talk that the feed held after the run: those of the last 14
days and of the next 120.

| Feed | Next four weeks | In the feed |
|---|---:|---:|
| `all` | 133 | 348 |
| `picks` | 14 | 31 |
| `math` | 35 | 92 |
| `cs` | 20 | 49 |
| `ai-ml` | 24 | 62 |
| `chem` | 16 | 37 |
| `bio` | 33 | 95 |
| `physics` | 38 | 95 |
| `med` | 31 | 86 |

### Known gaps

- Not collected: Bio-X answers automated requests with HTTP 403, and the Information
  Theory Forum is turned off (nearly dormant; its list gives no year). The SAIL events
  and the Theory Seminar are published only on Google Calendar, whose robots.txt forbids
  fetching the feeds. Arc Institute has no public seminar calendar. Talks outside the
  seven fields (for example earth and planetary sciences) are out of scope.
- The DBDS workshop and the NLP seminar may fail from GitHub's runners, which cannot
  always reach their robots.txt; both work from Stanford's network. The status page
  shows such a failure, and three in a row open a `source-broken` issue.
- The MLSys seminar comes back empty while its page announces no new talk.
- Speakers and affiliations read from free text favour precision: a missing speaker is
  preferred to a wrong one.
- Without an LLM, fields are tagged by rules only: of the talks in the seven fields that
  the campus calendar lists, the rules recognise about 75 percent, and with the LLM
  tagger about 96 percent. A run sends the LLM at most 60 requests, so an empty cache
  of its answers fills over a few runs.
- A talk that is deleted and comes back starts again at `SEQUENCE:0`.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) describes these limits and the others in
detail.

## How the daily update works

### Schedule

The workflow `.github/workflows/update.yml` runs every day at 04:23 Pacific time, and
once more at 10:23 as a catch-up, because GitHub sometimes delays or drops scheduled
runs. A scheduled run first looks at `data/last_run.json`: when today's run (Pacific
date) already succeeded, it stops and changes nothing. The effect is one update a
day. The schedule follows daylight saving time.

### Steps of one run

1. Fetch every source politely: robots.txt is honoured, including `Crawl-delay`, each
   host is asked one request at a time, and the number of requests per host is
   capped.
2. Turn what each source says into events, remove duplicates (the same talk listed by
   two sources) and drop what is not an academic talk.
3. Tag each event with its fields, by rules and, when a credential is configured, with
   the help of an LLM; score the picks.
4. Write `data/`, the nine feeds and the data of the web site, then check the output:
   a secret scan and the charset rule of `data/`. When a check fails, the run ends
   with exit status 3 and nothing is committed or deployed.
5. Validate the data and the feeds (`seminars validate`), commit `data/` as
   `github-actions[bot]` with the message `chore(data): daily update YYYY-MM-DD`,
   build the site (`seminars build-site`) and deploy it to GitHub Pages.

A source that fails does not stop the run. After three bad runs in a row an issue
with the label `source-broken` is opened, and it is closed again once the source
works. Problems with LLM credentials open an issue with the label `llm-auth`.

### The weekly digest

The workflow `.github/workflows/digest.yml` runs on Monday at 07:07 Pacific time,
with a catch-up on Tuesday at the same time. It reads the committed data, fetches
nothing and uses no LLM. It never opens two digests for the same week.

### LLM help (optional)

An LLM is used for two things only: extracting events from a page that the rules
cannot read, and tagging fields. Everything works without one. In GitHub Actions
the providers are tried in this order, and only those with a credential take part:
`claude-cli` (a Claude subscription token), `anthropic`, `openai`, `openai-compat`
(API keys). The credentials are repository secrets (`CLAUDE_CODE_OAUTH_TOKEN`,
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `LLM_API_KEY`), and the order and the models
are repository variables (`LLM_PROVIDERS`, `LLM_MODEL_<PROVIDER>`, `LLM_BASE_URL`).
An API provider takes part only when it also has a model, from its variable or from
`config/llm.yaml`. Changing a credential or a plan needs no code change: see
[docs/LLM_CREDENTIALS.md](docs/LLM_CREDENTIALS.md). A manual run of the update
workflow with the mode `llm-smoke` checks every configured provider and changes
nothing. When the Claude Code CLI cannot be installed during a run (for example
because npm is down), the workflow only warns, and the other providers still take
part.

Only the step that runs the pipeline and the smoke step receive the LLM credentials. The workflows
that see a credential never run for pull requests, so code from a fork cannot reach
them.

### If GitHub disables the schedule

GitHub disables the scheduled workflows of a public repository after 60 days without
activity. The daily data commit should prevent that, but GitHub does not promise
that it counts. If you get the notice, open the Actions tab, choose the workflow and
click Enable workflow.

## Run it locally

### Requirements

- [uv](https://docs.astral.sh/uv/); it installs the Python version of
  `.python-version` and exactly the dependencies of `uv.lock`.
- git, and bash for the scripts.

### Commands

```
uv sync --locked
uv run seminars run --offline       # recorded pages from tests/fixtures/, no network
uv run seminars run --no-llm        # the real sites, rules only
uv run seminars build-site          # assembles public/
uv run seminars validate            # checks data/ and public/
uv run python -m http.server --directory public 8000
uv run seminars digest --dry-run    # prints the digest instead of opening an issue
```

A live run rewrites `data/` in your checkout. An offline run writes there only while
`data/` holds no live run; the committed `data/` is a live run, so in a fresh clone the
offline run works in a scratch directory and says so. A live run is polite and therefore
slow: some sites ask for 30 seconds between two requests. `uv run seminars --help` lists
every command and option.

### Checks

These are the checks of the CI workflow (`.github/workflows/ci.yml`); run them before
you push:

```
python3 scripts/check_ascii.py
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
```

The tests never touch the network. The browser smoke tests of the web site are
deselected by default; run them with:

```
uv sync --locked --extra browser
uv run --extra browser playwright install chromium
uv run --extra browser pytest -m browser
```

## Add a source

Most sources need nothing but a few lines of YAML; odd sites get a small adapter
module. [docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md) explains both, how to
record test fixtures with the polite client, and the charset rule.

## Repository layout

```
config/            settings, LLM settings, fields, interests, sources.d/ (one file per group)
data/              the committed data: events, stable ids, health, last run, LLM cache, archive
docs/              documentation in English and Chinese
locales/           every text of the web site and the digest (en, zh-CN)
scripts/           check_ascii.py, set-llm-credential.sh
site/              the static web site (no build step)
src/seminars/      the package: sources/, pipeline/, classify/, llm/, outputs/, cli.py
tests/             tests, and fixtures/ with recorded pages of every source
.github/workflows/ ci.yml, update.yml, digest.yml
```

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how the project is built, what one run
  does, and the known limits.
- [docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md): how to add a source.
- [docs/LLM_CREDENTIALS.md](docs/LLM_CREDENTIALS.md): the LLM providers and their
  credentials.
- [CHANGELOG.md](CHANGELOG.md): what each release contains.

## License and data

The code is licensed under the MIT license, see [LICENSE](LICENSE). The event
information belongs to the organizers who published it; every event links to its
original page. Everything in `data/` is public, like the rest of this repository.
Only public pages are read: nothing behind a login, no mailing lists and no personal
data beyond what the public pages show.
