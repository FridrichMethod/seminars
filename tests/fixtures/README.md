# Test fixtures

Recorded HTTP responses of the data sources. Adapter tests and
`seminars run --offline` read these files instead of the network, so every
source needs at least one recording. The code behind this convention is
`src/seminars/fixtures.py`.

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
    "https://chemistry.stanford.edu/jsonapi/node/hs_event?page[limit]=50"
```

Options: `--name FILE` chooses the body file name (default: derived from the
URL), `--volatile-param NAME` (repeatable) marks a query parameter as
volatile, `--allow-status CODE` (repeatable) also records an error response.

- The recorder goes through the polite HTTP client: robots.txt is honoured,
  and the pause between two requests to one host is
  `max(1 s, Crawl-delay)`, also across separate invocations (the last request
  time is kept in `.cache/http/`). Expect a 30 s wait per request on hosts
  that ask for `Crawl-delay: 30`.
- Fetch each URL once and then work from the fixture.
- Recording the same URL again replaces its entry and body.
- Always quote the URL in the shell (`?`, `[` and `&` are special).
- Check the result with `uv run python -m seminars.fixtures check`.

## Rules for body files

- Text only (HTML, JSON, XML, ICS). Images and other binary content are
  refused; never store them.
- Bodies are normalized when stored: a byte order mark is removed, CRLF and CR
  become LF, and the file ends with a newline. Parsers must accept LF-only
  input; this matters for ICS, where the standard asks for CRLF.
- At most 500 KB per file. Trim a larger body by hand to what the tests need,
  keep it valid (JSON must still parse) and say what was removed in a
  `README.md` in the same directory.
- Recordings are real content and may contain any Unicode; the charset check
  does not restrict `tests/fixtures/**`.
- Never record pages that need a login, and nothing that contains personal
  data beyond what the public page shows.

## Using fixtures in tests

```python
from seminars.http import offline_client

http = offline_client("sites-chemistry")  # serves tests/fixtures/sites-chemistry/
payload = adapter.fetch(http, source, window=window)
drafts = adapter.parse(payload, source)
```

The `offline_http` pytest fixture in `tests/conftest.py` does the same:
`offline_http("sites-chemistry")`.
