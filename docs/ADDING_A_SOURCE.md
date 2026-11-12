English | [&#20013;&#25991;](ADDING_A_SOURCE.zh-CN.md)

# Adding a source

A source is one public calendar, feed or page that lists talks. Adding one takes
a YAML entry when its format is one of the generic ones, or a YAML entry plus a
small adapter module when the site is unusual. Either way it also takes a
recording of the source's pages (a fixture) and a test, because the tests and
`seminars run --offline` never touch the network.

## Before you start

- Only public pages: nothing behind a login, no mailing lists, and no personal data
  beyond what the public page shows.
- Every request goes through the project's polite HTTP client. It honours
  robots.txt including `Crawl-delay`, waits at least one second between two
  requests to a host, asks one host one request at a time and caps the requests per
  host and run. Never use curl, wget or a browser script on a source site.
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
  `llm_fallback`, `enabled`. The schema, with every key explained, is `SourceConfig`
  in `src/seminars/models/source.py`; unknown keys are an error.
- `enabled: false` turns a source off and keeps its entry. Turning a source off, or
  removing its entry, removes its future talks at the next run, from the feeds and
  the site alike.

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
