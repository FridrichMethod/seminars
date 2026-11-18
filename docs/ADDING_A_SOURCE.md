English | [&#20013;&#25991;](ADDING_A_SOURCE.zh-CN.md)

# Adding a source

A source is one public calendar, feed or page that lists talks. Adding one takes
a YAML entry when its format is one of the generic ones, or a YAML entry plus a
small adapter module when the site is unusual. Either way it also takes a
recording of the source's pages (a fixture) and a test, because the tests and
`seminars run --offline` never touch the network. What a run does with a source after
its adapter is described in [ARCHITECTURE.md](ARCHITECTURE.md).

## Before you start

- Only public pages: nothing behind a login, no mailing lists, and no personal data
  beyond what the public page shows.
- Every request goes through the project's polite HTTP client. It honours
  robots.txt including `Crawl-delay`, waits at least one second between two
  requests to a host, asks one host one request at a time and caps the requests per
  host and run. Never use curl, wget or a browser script on a source site.
- When the owner of the project decides to collect a public page whose robots.txt
  disallows it, the source lists that host under `ignore_robots`, with a comment that
  says why. Only the `Disallow` rules of that host are set aside, and only for that
  source: the `Crawl-delay`, the pause and the request budgets still hold.
- Choose the extraction method in this order: a structured API (Localist, Drupal
  JSON:API), a feed (ICS, RSS), JSON-LD embedded in the pages, an HTML list, and
  only when nothing else works, the LLM page extractor.

## 1. Look at the site

Let the format probe have a first look. It fetches the address once, recognises
Localist, Drupal JSON:API, RSS, ICS and JSON-LD by cheap markers, and prints a
suggested YAML entry:

```
uv run seminars add-source "https://www.example.edu/talks"
```

For more trial requests use `fetch`. It goes through the same polite client, shows
status, final address, content type and size of each answer, and writes no
fixture. With `--save-dir` it also keeps the bodies in a scratch directory, which
git ignores:

```
uv run python -m seminars.fixtures fetch --save-dir .scratch/example \
    "https://www.example.edu/talks" \
    "https://www.example.edu/robots.txt"
```

Some sites ask for 30 seconds between two requests, so put all addresses of one
host into one call and expect it to take a while. Always quote the addresses in
the shell.

## 2. Describe the source in YAML

Sources live in `config/sources.d/`, one file per group. The id of a source is
lower-case words joined by hyphens, starts with the prefix of its group, and also
names its fixture directory `tests/fixtures/<id>/`.

| Group | File | Id prefix | What belongs there |
|---|---|---|---|
| `localist` | `localist.yaml` | `localist-` | Stanford Events, the Localist calendar of the university |
| `stanford-sites` | `stanford-sites.yaml` | `sites-` | department sites built on Stanford Sites (Drupal) |
| `stanford-custom` | `stanford-custom.yaml` | `custom-` | Stanford labs and centers with pages of their own |
| `bayarea` | `bayarea.yaml` | `bayarea-` | SLAC, CZ Biohub and other Bay Area institutes |
| `researchseminars` | `researchseminars.yaml` | `rsorg-` | series listed on researchseminars.org |
| `conferences` | `conferences.yaml` | `conf-` | the registry: conferences and workshops kept by hand |

An entry for a generic adapter:

```yaml
sources:
  - id: sites-chemistry
    name: Stanford Chemistry
    group: stanford-sites
    adapter: drupal_jsonapi
    url: https://chemistry.stanford.edu/jsonapi/node/hs_event
    institution: stanford
    host_org: Stanford Chemistry
    default_fields: [chem]
    params: {page_limit: 50}
    research: {verified: 2026-11-10, sample_events: 12, has_jsonapi: true}
```

- `adapter` names the adapter, see step 3.
- `url` (or a list `urls`) is where the adapter starts. Write the address the site
  ends up at after redirects: the adapter may only fetch from the hosts of `url`,
  `urls` and `homepage`, and a host that a redirect leads to counts as foreign.
  When a source really needs pages of another host (for example detail pages on
  `events.stanford.edu`), list that host under `params.extra_hosts`.
- `institution` is one of `stanford`, `slac`, `arc`, `biohub` and `other`.
- `default_fields` are the fields of every talk of this source (`math`, `cs`,
  `ai-ml`, `chem`, `bio`, `physics`, `med`); leave the list empty for calendars that
  mix all subjects, whose talks are then tagged by the rules.
- `params` holds what only the adapter reads; each adapter documents its keys in the
  docstring of its module.
- `research` records what you found out: the day you checked, how many events the
  sample had, which formats the site offers, its `Crawl-delay`, notes.
- Other keys: `homepage`, `series`, `default_event_type`, `timezone`, `aggregator`,
  `llm_fallback`, `ignore_robots`, `enabled`. The schema, with every key explained, is
  `SourceConfig` in `src/seminars/models/source.py`; unknown keys are an error.
- `ignore_robots` lists hosts of the source (of `url`, `urls`, `homepage` or
  `params.extra_hosts`) whose robots.txt `Disallow` rules this source does not follow;
  see "Before you start". A host of another site is an error.
- `enabled: false` turns a source off and keeps its entry. Turning a source off, or
  removing its entry, removes its future talks at the next run, from the feeds and
  the site alike: its keys are dropped from the stored events, a talk that only it
  listed leaves at once, and one of those that has already started goes to the
  archive. A talk that other sources list as well keeps their keys and is judged by
  them alone.
- A disabled source says why on the status page. Put a comment `# Off: <reason>`
  above `enabled: false`, and the reason for readers under
  `site.status.disabled_reasons.<id>` in `locales/en.json` and `locales/zh-CN.json`.
  A test (`tests/test_locales.py`) wants a reason for every disabled source and for
  no other, so the message goes when the source is turned on again.

## 3. Choose the adapter

### A generic adapter

Most sources need nothing but the YAML entry, because their format is one of these:

| Adapter | Use it for |
|---|---|
| `localist` | the Localist JSON API of events.stanford.edu |
| `drupal_jsonapi` | Drupal JSON:API (event nodes of the Stanford Sites template) |
| `ics` | iCalendar feeds |
| `rss` | RSS and Atom feeds, optionally with the detail page of each item |
| `jsonld` | pages with schema.org `Event` data embedded as JSON-LD |
| `html_list` | HTML lists, read with CSS selectors given in `params` |
| `llm_page` | pages without any structure: fetched only, read by the LLM page extractor |

`llm_page` depends on an LLM credential and is the last resort. For any other
adapter, `llm_fallback: true` lets the LLM step in when the rules read nothing from
the pages of the source or fail on them.

### A custom adapter

When no generic adapter fits, write one module in `src/seminars/sources/custom/`.
Every module there is imported automatically, so no other file changes. The
adapter is named after its module, and the source selects it with
`adapter: <module name>`:

```python
"""Talks of the Example Lab seminar (https://www.example.edu/talks)."""

from __future__ import annotations

from pydantic import ValidationError
from selectolax.lexbor import LexborHTMLParser, LexborNode

from seminars.httpbase import HttpClient
from seminars.models import EventDraft, RawPayload, SourceConfig, TimeWindow
from seminars.sources.base import ParseError, absolute_url, fetch_urls, register_adapter


def _text(node: LexborNode | None) -> str | None:
    return None if node is None else node.text(strip=True) or None


@register_adapter("example_lab")
class ExampleLabAdapter:
    def fetch(self, http: HttpClient, source: SourceConfig, *, window: TimeWindow) -> RawPayload:
        return fetch_urls(http, source, window=window)

    def parse(self, payload: RawPayload, source: SourceConfig) -> list[EventDraft]:
        drafts: list[EventDraft] = []
        for document in payload.documents:
            items = LexborHTMLParser(document.text).css("li.talk")
            if not items:
                raise ParseError(f"{source.id}: no talk list in {document.url}")
            for item in items:
                link = item.css_first("a.title")
                when = item.css_first("time")
                if link is None or when is None:
                    continue  # one malformed item: skip it, keep the rest
                try:
                    drafts.append(
                        EventDraft(
                            source_id=source.id,
                            extraction="html",
                            url=absolute_url(document.final_url, link.attributes.get("href")),
                            title=_text(link),
                            start=when.attributes.get("datetime") or when.text(strip=True),
                            speaker_text=_text(item.css_first(".speaker")),
                            location=_text(item.css_first(".location")),
                        )
                    )
                except ValidationError:
                    continue
        return drafts
```

The rules, in full in the docstring of `src/seminars/sources/base.py`:

- `fetch` does all the I/O, with nothing but the `HttpClient` it is given, and asks
  for the same addresses offline as live. It stays on the hosts of its source:
  `fetch_urls` checks that, and an adapter that follows links checks each one with
  `on_source_host` first.
- `parse` is a pure function of the payload and the source entry. It copies what the
  source says; cleaning text is the job of the pipeline. Use `payload.fetched_at` as
  "now", for example to infer a missing year.
- Times and links are the adapter's job, because only it knows the format. A naive
  time is wall time in the source's time zone; a date without a time makes an
  all-day event, whose `end` is the last day, inclusive. Every link is made
  absolute with `absolute_url(document.final_url, link)`.
- Raise `ParseError` when a response makes no sense at all; skip single malformed
  items. A draft that breaks the rules raises `pydantic.ValidationError` when it is
  built, which counts as a malformed item.
- The class takes no constructor arguments and keeps no state between calls.

## 4. Record fixtures

Record the pages the adapter fetches, with the polite client:

```
uv run python -m seminars.fixtures record --source-id custom-example-lab \
    "https://www.example.edu/talks"
uv run python -m seminars.fixtures check --source-id custom-example-lab
```

This writes `tests/fixtures/<id>/manifest.json` and one body file per address.

- Record exactly the addresses the adapter asks for, all addresses of one host in
  one call. Mark query parameters that change from run to run, such as the dates
  of the time window, with `--volatile-param NAME`.
- A body may be at most 500 KB. Trim a larger one by hand to what the test needs,
  keep it valid, and say what you removed in a `README.md` in the same directory.
- Never edit the manifest by hand, and never copy files from `.scratch/` into a
  fixture directory: record them.
- Fixtures are verbatim snapshots and keep whatever characters the site sent.
- `record` follows the `ignore_robots` of the configured source of `--source-id`, as a
  run does.
- An offline run happens at the latest recording time of all fixtures. A new
  recording moves it, so update `RUN_TIME` in
  `tests/test_pipeline_offline_integration.py` and the talks that test picks.

Every option, and how the client waits for other processes that use the same host,
is described in [tests/fixtures/README.md](../tests/fixtures/README.md).

## 5. Test it

A custom adapter gets a test file `tests/test_custom_<module>.py` that reads the
fixture and checks title, time, speaker and location of real items:

```python
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from seminars.config import load_sources
from seminars.fixtures import OfflineHttpClient
from seminars.models import TimeWindow
from seminars.sources.base import get_adapter

ROOT = Path(__file__).resolve().parents[1]
PACIFIC = ZoneInfo("America/Los_Angeles")
WINDOW = TimeWindow(
    start=datetime(2026, 11, 1, tzinfo=PACIFIC), end=datetime(2027, 3, 1, tzinfo=PACIFIC)
)


def test_the_list_page_gives_title_time_speaker_and_location(
    offline_http: Callable[..., OfflineHttpClient],
) -> None:
    sources = load_sources(ROOT / "config" / "sources.d")
    source = next(s for s in sources if s.id == "custom-example-lab")
    adapter = get_adapter(source.adapter)
    payload = adapter.fetch(offline_http(source.id), source, window=WINDOW)
    first = adapter.parse(payload, source)[0]
    assert first.title == "Learning to fold"
    assert first.start == datetime(2026, 11, 17, 16, 0)
    assert first.speaker_text == "Jane Doe (MIT)"
    assert first.location == "Clark Center S361"
```

A source of a generic adapter is covered by the tests of that adapter; check it
end to end with an offline run of that one source. Once the source is merged, the
live check shows how it does against the real site:

```
uv run seminars run --offline --source custom-example-lab
uv run seminars sources check custom-example-lab
```

Every run records how each source did (`data/health.json`, the status page, the
weekly digest). A source that fails three runs in a row, that listed talks before
and comes back empty three runs in a row, or that is not read in full three runs in
a row (pages it could not fetch, a listing far smaller than usual) gets a
`source-broken` issue, which is closed once the source is read in full again. A
listing far smaller than usual counts as incomplete for up to 28 runs; after that
its smaller size is taken as the usual one, the stored talks it no longer lists
count as missing, and the issue is closed with a comment that says so.

## 6. The charset rule

Everything in this repository is UTF-8 without a byte order mark, with LF line
endings and a final newline, and almost everything is plain printable ASCII:
code, comments, docstrings, YAML, scripts, workflows, tests, commit messages, the
English documents and `locales/en.json`.

- Write `"`, `'`, `-`, `--`, `...`, `->` and `*` instead of curly quotes, dashes,
  the ellipsis character, arrows and bullets. No emoji, no ligatures, no
  no-break or zero-width spaces, no full-width punctuation.
- Non-ASCII text that a test needs, such as an accented name, is written with
  escapes: `"Jos\u00e9"`.
- Only these files may hold other characters: `*.zh-CN.md` and
  `locales/zh-CN.json` (Chinese, but never emoji), `data/**` (names as the sources
  write them, but no emoji, invisible or control characters), and
  `tests/fixtures/**` (verbatim recordings).
- `scripts/check_ascii.py` checks every tracked file. It runs as a pre-commit hook
  and in CI; run it yourself with `python3 scripts/check_ascii.py`.

## 7. Before you open a pull request

Run the checks of the CI workflow:

```
python3 scripts/check_ascii.py
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
```

Commit the YAML entry, the adapter module if there is one, the fixtures and the
test. Do not commit the `data/` that a local run rewrote: the daily update owns it.
Pull requests run the CI workflow only, which has no credentials; the new source
joins the daily update once the change is merged.

## Conferences and workshops without a source

Some meetings are announced months ahead on pages that no source reads: the annual
meeting of a society in San Francisco, a symposium that a center posts once, the
workshops of an institute whose site needs a browser. The registry keeps them by hand.
It is the source `conf-registry` in `config/sources.d/conferences.yaml`, with the
adapter `registry` (`src/seminars/sources/custom/registry.py`, whose docstring
describes every key). Its adapter requests nothing: each meeting is an entry of
`params.meetings`, and its `url` is the page of that file on GitHub, which the status
page links and nothing fetches. The source has `meeting_horizon: true`, so a meeting is
kept up to a year ahead, and `allow_empty: true`, so a registry whose meetings are all
over is no broken source.

Register a meeting only when no enabled source delivers it: search `data/events.json`
and the site for its title first. An entry holds the facts of one edition, each from
the organizer's own page of that edition, and nothing else: no description, no
contact, no address of a person, and never a date projected from an earlier edition.

```yaml
- id: iclr-2027
  title: "ICLR 2027: The Fifteenth International Conference on Learning Representations"
  series: ICLR
  host_org: International Conference on Learning Representations (ICLR)
  start: 2027-04-26
  end: 2027-04-30
  region: bay-area
  location: Moscone Center, San Francisco, CA
  url: https://iclr.cc/Conferences/2027
  fields: [ai-ml, cs]
  event_type: conference
  status: confirmed
  verified: {date: 2026-11-18, url: "https://iclr.cc/Conferences/2027"}
  milestones:
    - {kind: workshop_paper, date: 2027-02-01, label: Suggested workshop paper deadline}
    - kind: notification
      date: 2027-02-26
      time: "23:59"
      zone: AoE
      label: Workshop paper notification deadline
```

- `id` is `<series slug>-<year>`, ending in the year the edition starts in. It is the
  uid of the event (`conf-registry.<id>`), so it is never renamed: a renamed entry is a
  new event, and the old one stays until the third run without it.
- `start` and `end` are local dates, `end` inclusive. Only `region: bay-area` entries
  become events; `world` entries carry their milestones and nothing else.
- `url` is the organizer's page of the edition: the site of the edition, or the page of
  the series or program that announces it (the notes of the entry say so). A listing of
  the same meeting by a source becomes one event with the entry only when it points at
  the same page, starts on the first day, ends on the last and lasts at least 3 hours
  (`pipeline/dedup.py`). Otherwise the meeting shows twice: a listing of one day of a
  longer meeting, a listing of a calendar that links its own page of the event (Localist
  does), and any listing of an entry whose `url` is the front page of a site
  (`https://2027.naacl.org/`), which is no detail page.
- `location` is the venue and the city the page names. While it names none, write
  `To be announced`, which the event shows as no location.
- `status: tentative` marks a date that is only saved, without venue or program.
- `verified` says on which page and on which day the facts were last checked. Check
  every entry again at the start of each term: its facts on the organizer's page, and
  whether a source lists the meeting by now (search `data/events.json` for its title).
  Delete the entry once a source lists the meeting. `tests/test_custom_registry.py`
  fails when an edition that starts within 120 days was checked more than 90 days
  before, or when no Bay Area edition is ahead, and names the entries to look at.
- `milestones` are the dates of the edition that a reader acts on, on or before its last
  day: deadlines, openings, notifications, with the time and zone the page gives (`AoE`,
  `PT`, `MT`, `ET`, `UTC` or an IANA name). A zone without a time is the zone of the
  day, as in a deadline of January 22 `AoE`. The label is a few words of our own. The
  description of the event lists them by date; one that has passed leaves it once the
  window of the run (14 days back) has moved past it.
- YAML reads an unquoted `on`, `off`, `yes` or `no` as true or false and an unquoted
  `23:59` as a number: the day of a check is `date`, a qualifier is `exact` or `by`,
  and a time is written in quotes.

A meeting that is over leaves the registry's listing by itself, so deleting its entry
changes nothing. The deleted entry of a meeting ahead is removed from the feeds at the
third run without it, like a talk that its source no longer lists; an edition that has
already started stays until it ends. Two cases keep a deleted entry longer:

- A run in which the registry lists no meeting at all says nothing about its events, so
  an entry deleted then stays until it is over.
- Every run of a source is compared with the usual size of its listing
  (`seminars.pipeline.collect`). A change that deletes more than half of the listed
  editions, and at least five, looks like a listing that lost part of itself: the
  deleted events stay, a `source-broken` issue opens at the third such run, and both
  last until the 29th run takes the smaller listing as the usual size, which closes the
  issue; the deleted events leave at the 31st run. Delete at most half of the listed
  editions, or fewer than five, in one change, and let six runs pass before the next.

No address of a source or of a registered meeting may be on a site that lists
conferences for a fee (waset.org, 10times.com and the like;
`tests/test_source_denylist.py` checks this).
