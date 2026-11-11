# Test fixtures

Recorded HTTP responses of the data sources. Adapter tests and
`seminars run --offline` read these files instead of the network, so every
source needs at least one recording. The code behind this convention is
`src/seminars/fixtures.py`. Its command line (`record`, `fetch`, `check`)
is also the only way to send a request to a source site by hand.

## Layout

```
tests/fixtures/
  <source_id>/            one directory per source, named exactly like its id
    manifest.json         maps each recorded request URL to a body file
    <file>                one response body per URL (text only)
    README.md             optional notes: what was trimmed, oddities of the source
  _site/                  sample public payloads for the web site (not a source)
```

Directories whose name starts with `_` are not sources and are ignored by the
fixture loader.

## manifest.json

```json
{
  "schema_version": 1,
  "source_id": "sites-chemistry",
  "entries": {
    "https://chemistry.stanford.edu/jsonapi/node/hs_event?page[limit]=50": {
      "file": "hs-event-page1.json",
      "status": 200,
      "content_type": "application/vnd.api+json",
      "fetched_at": "2026-11-10T21:30:00Z",
      "final_url": null,
      "volatile_params": []
    }
  }
}
```

- The key is the request URL exactly as the adapter issues it.
- `file`: name of the body file in the same directory.
- `status`, `content_type`: as answered by the server (`content_type` without
  parameters, lower case).
- `fetched_at`: when the response was recorded (UTC). In offline mode it
  becomes `FetchedDocument.fetched_at`, the reference time for parsing.
- `final_url`: the URL after redirects, `null` when there was no redirect.
- `volatile_params`: names of query parameters whose values change from run
  to run (window dates). Their values are ignored when a request is matched.

Do not edit the manifest by hand unless you rename or delete a body file.

## How a request is matched in offline mode

1. The exact URL string is looked up first.
2. Otherwise URLs are compared in canonical form: scheme and host lower-cased,
   path percent-decoded, fragment dropped, query parameters sorted. The order
   and the percent-encoding of query parameters therefore do not matter.
3. For an entry with `volatile_params`, the values of those parameters are
   ignored (the parameters themselves must still be present).

A URL that matches nothing raises `FixtureNotFoundError`; offline mode never
falls back to the network. Response headers are not recorded, and
`FetchedDocument` has none, so an adapter must not depend on them.

Offline runs must not use the real clock, otherwise the recorded events
would slowly fall out of the time window. `seminars.fixtures.reference_time`
returns the latest `fetched_at` of all fixtures; use it as "now".

## Recording

```
uv run python -m seminars.fixtures record --source-id sites-chemistry \
    "https://chemistry.stanford.edu/jsonapi/node/hs_event?page[limit]=50" \
    "https://chemistry.stanford.edu/jsonapi/node/hs_event?page[limit]=50&page[offset]=50"
```

Options: `--name FILE` chooses the body file name (default: derived from the
URL; only with a single URL; a plain name that ends in `.html`, `.ics`,
`.json`, `.md`, `.txt` or `.xml`), `--volatile-param NAME` (repeatable) marks a
query parameter as volatile, `--allow-status CODE` (repeatable) also records
an error response. Options apply to every URL of the call. A URL that fails
is reported and the remaining ones are still recorded; the exit status is 1
when any failed.

`--source-id` takes a source id (lower-case letters, digits and single
hyphens, at most 64 characters), never a path: `../x` and `a/b` are refused,
here and in `check --source-id`.

- The recorder goes through the polite HTTP client: robots.txt is honoured,
  and the pause between two requests to one host is `max(1 s, Crawl-delay)`.
  Expect a 30 s wait per request on hosts that ask for `Crawl-delay: 30`.
  The delay counts for the whole host: when the robots.txt of its https
  site asks for 30 s, requests to its http site wait 30 s as well.
- Pass all URLs of one host to a single call. They are fetched one after
  another, which is the simplest way to stay serial.
- The pause also holds between separate calls, one after another or at the
  same time: the last request time of every host and a per-host lock are
  kept in the HTTP cache directory, and a call waits while another one is
  using the host. This works only between calls that use the same cache
  directory.
- Calls that want the same host take turns, one request each. A call that
  waits stands in the queue of the host (`hosts/<host>.queue` in the cache
  directory), and the call that has just made a request lines up behind
  it. A second call therefore gets its first request in after at most one
  more request of the first, however many URLs that one still has: with a
  Crawl-delay of 30 s, two calls alternate every 30 s.
- The cache directory is `.cache/http/` of the main checkout. A linked git
  worktree (made with `git worktree add`) has no directory of its own: it
  finds the main checkout through its `.git` file and uses
  `<main checkout>/.cache/http/`. Worktrees that record at the same time
  therefore keep the Crawl-delay between them without any setup.
- Every call prints the directory it uses before its first request, on
  stderr: `HTTP cache directory: /path/to/.cache/http`. Look at that line
  when in doubt: two calls protect a host from each other only when they
  print the same path.
- `record` and `fetch` do not run without that directory. When it cannot
  be created or a lock cannot be taken in it (a read-only checkout, a file
  in its place, a file system without locks), the call prints
  `error: the HTTP cache directory cannot be used (...)` and ends with
  status 1 before any request. Make the directory writable or set
  `SEMINARS_HTTP_CACHE_DIR`; do not work around it.
- Separate clones of the repository do not know about each other, and
  neither does a worktree of a bare repository, which has no main checkout.
  There, export one common absolute path in all of them before recording:
  `export SEMINARS_HTTP_CACHE_DIR=/path/to/shared/http-cache`. The variable
  wins over the default everywhere.
- The per-host lock needs a POSIX system. On Windows, never run two calls
  for the same host at the same time.
- Nothing waits forever. One request with its whole answer is given up
  after `http.max_exchange_seconds` (60 s), and one URL with everything it
  takes (robots.txt, redirects, retries, the pauses, the wait for another
  call that uses the host) after `http.max_get_seconds` (600 s). Such a URL
  is reported as failed and the remaining ones are still fetched. When
  several calls queue up for a host with a long Crawl-delay, the ones at
  the back can run into the second limit; run them again afterwards.
- Only public sites are contacted. A URL with a user name or password, a
  host on this machine or in a private network, and a name that resolves
  to such an address are refused, also as the target of a redirect.
- Fetch each URL once and then work from the fixture. For trial requests
  that should not become fixtures, use `fetch` (next section).
- Recording the same URL again replaces its entry and body.
- Always quote the URL in the shell (`?`, `[` and `&` are special).
- Check the result with `uv run python -m seminars.fixtures check`.

## Looking at a source without recording

Researching a source takes many trial requests: finding the right page,
trying query parameters, telling which template a site uses. Use `fetch` for
them:

```
uv run python -m seminars.fixtures fetch --save-dir .scratch/chemistry \
    "https://chemistry.stanford.edu/events" \
    "https://chemistry.stanford.edu/jsonapi/node/hs_event?page[limit]=5"
```

For every URL it prints the status, the URL after redirects, the content
type and the size of the decoded body in bytes:

```
fetched https://chemistry.stanford.edu/events
  status: 200
  final URL: https://chemistry.stanford.edu/events
  content type: text/html
  bytes: 48211
  saved: /path/to/repo/.scratch/chemistry/chemistry-stanford-edu-events-1a2b3c4d.html
```

- `fetch` uses the same polite client and the same cache directory as
  `record`. robots.txt, Crawl-delay, the fixed User-Agent, the request
  budget per host and the public-hosts-only rule all apply, and the URLs of
  one call are fetched one after another. What "Recording" says about
  waiting and about the cache directory holds here as well.
- It never writes a fixture or a manifest. Without `--save-dir` it writes
  nothing but the HTTP cache.
- `--save-dir DIR` also writes each body to a file in DIR and prints the
  path. The body is the decoded text in canonical form, as a fixture would
  hold it. The file name is built from the host, the path and a digest of
  the URL, so no URL can make it leave DIR. A relative DIR counts from the
  project root. Use `.scratch/<topic>`: `/.scratch/` is ignored by git.
- Inside the project, `.scratch` and the directories below it are the only
  place `--save-dir` accepts. A body is whatever a site sent, so `data/`,
  `public/`, `config/`, `tests/fixtures` and every other directory of the
  project are refused, also through a symbolic link. A directory outside
  the project is used as it is.
- `--allow-status CODE` (repeatable) shows an error response instead of
  failing on it.
- Nothing a site sent is printed raw. In the output of `fetch`, `record`
  and `check`, every character outside printable ASCII appears as an
  escape such as `\x1b` or `\n`, so a line of output is always one line.
  A Content-Type that is not a plain `type/subtype` is shown as `unknown`.
- A URL that fails is reported on stderr and the remaining ones are still
  fetched; the exit status is 1 when any failed.
- Once you know which URLs an adapter needs, record them with `record`.
  Do not copy files from the scratch directory into a fixture directory:
  the manifest would not list them.

Never use curl, wget or any other direct network tool for source research.
They skip robots.txt and the Crawl-delay, do not wait for other processes
that use the same host, and hide who is asking. `fetch` and `record` are the
only ways to send a request to a source site by hand.

## Rules for body files

- Text only (HTML, JSON, XML, ICS). Images and other binary content are
  refused; never store them.
- Data files only. A fixture directory holds `manifest.json`, the bodies
  and notes, and every file name in it ends in `.html`, `.ics`, `.json`,
  `.md`, `.txt` or `.xml`. A body is whatever a site sent, so it must
  never be a file that gets executed: pytest would import a `conftest.py`
  or a `test_*.py`. `record` refuses any other name, `check` reports any
  other file, and pytest does not look for tests below `tests/fixtures`
  (`norecursedirs` in `pyproject.toml`). Never put a Python file there.
- Bodies are stored in canonical form: no byte order mark, LF line endings
  (CRLF and CR are converted) and a final newline. Every HTTP client hands out
  document text in this same form, live as well as offline, so an adapter sees
  identical text in both modes. Parsers must accept LF-only input; this
  matters for ICS, where the standard asks for CRLF.
- At most 500 KB per file. Trim a larger body by hand to what the tests need,
  keep it valid (JSON must still parse) and say what was removed in a
  `README.md` in the same directory.
- Recordings are verbatim page snapshots and may contain any Unicode, emoji
  and invisible characters included; the charset check does not restrict
  `tests/fixtures/**`. Do not clean them up: they are test input and are
  never published, and the cleaning code is tested against them. Everything
  derived from them (`data/**`, feeds, site data, digests) must be free of
  emoji and decorative characters, and `data/**` is checked for that.
- Never record pages that need a login, and nothing that contains personal
  data beyond what the public page shows.

Expected output is not a fixture. Golden files (`tests/golden/`) fall under
the strict charset rule: printable ASCII and LF only. Golden `.ics` files
are therefore stored with LF, a test replaces CRLF with LF in the produced
feed before comparing, and cases with non-ASCII text are built in the test
code with escapes instead of being stored. The docstring of `IcsFeedWriter`
in `seminars.contracts` has the details.

## Using fixtures in tests

```python
from seminars.http import offline_client

http = offline_client("sites-chemistry")  # serves tests/fixtures/sites-chemistry/
payload = adapter.fetch(http, source, window=window)
drafts = adapter.parse(payload, source)
```

The `offline_http` pytest fixture in `tests/conftest.py` does the same:
`offline_http("sites-chemistry")`.

## Sample payloads for the web site (`_site/`)

`_site/events.json` and `_site/status.json` are examples of the two files the
static site reads (`public/events.json` and `public/status.json`; schemas:
`PublicEventsPayload` and `PublicStatusPayload` in `seminars.models`). They
are generated by `tests/site_sample.py` from the models and checked by
`tests/test_public_sample.py`; regenerate them with
`uv run python tests/site_sample.py`, never edit them by hand.

The sample deliberately contains hostile and awkward content: HTML and script
injection in titles, abstracts, speaker names, locations and status messages,
an all-day event, cancelled and tentative events, online and hybrid events, a
"TBA" title, a missing end time, non-ASCII names, picks with reasons, a past
event and all seven fields.
