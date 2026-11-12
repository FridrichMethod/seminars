# custom-biox-seminars

Stanford Bio-X, recorded 2026-11-11 (Pacific time) through the polite
client. The source is disabled.

| File | URL | What it is |
|---|---|---|
| `event-type-seminar-d9b9cd10.html` | `https://biox.stanford.edu/event-type/seminar` | HTTP 403: a Cloudflare challenge page ("Just a moment...") instead of the listing |

The site answers every page with this challenge, which needs JavaScript and
cookies: `/event-type/seminar`, `/`, `/events`, `/events/all/rss` and
`/jsonapi/node/stanford_event` all returned HTTP 403 with the same page
(only the first is recorded). robots.txt was answered normally
(`Crawl-delay: 10`). The recording is kept so that an offline run of the
source fails the way a live one does (an HTTP 403 for its URL).

## What was replaced

The challenge page carries values that Cloudflare issued for this one
request. They were replaced by `REDACTED`, keeping the keys:

- the script nonce (4 times: the `nonce` attribute, the CSP header meta,
  `cN` and `a.nonce`);
- the ray id (`cRay` and the `ray=` parameter of the challenge script URL);
- the tokens `cH`, `md` and `mdrd`;
- the values of `__cf_chl_tk`, `__cf_chl_f_tk` and `__cf_chl_rt_tk`.

`cITimeS` (the time of the request) and the rest of the page are as
received. None of these values is a credential of this project. The tokens
belong to the challenge itself; the ray id only identifies the response and
was replaced as a precaution, together with them. Pages that Cloudflare
serves without a challenge keep their ray id as received (for example the
recordings of custom-csb-events).
