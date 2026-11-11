# rsorg-topos-colloquium

Recorded 2026-11-11 from researchseminars.org with the polite client. Nothing
was trimmed: `series.ics` is the feed as served (in canonical form: LF line
endings, final newline), except for the Zoom credentials described under
"Redacted" below.

| File | Request | What it is |
|---|---|---|
| `series.ics` | `https://researchseminars.org/seminar/ToposInstituteColloquium/ics` | ICS feed of the Topos Institute Colloquium (Berkeley, online): 233 VEVENTs, 24 of them on or after 2026-11-11 |

## What this feed shows

* The feed holds the WHOLE history of the series (2021-02-04 to 2027-06-10),
  not only upcoming talks, and the VEVENTs are not sorted by date. There is
  no query parameter to limit it; the adapter filters by the run window.
* `DTSTART` / `DTEND` are UTC date-times (`20261112T170000Z`), every one of
  them. No `VALUE=DATE` (all-day) values and no `TZID` appear.
* `UID` is `<series id>/<talk counter>` (`ToposInstituteColloquium/232`),
  unique in the feed and stable: the site keeps the counter of a talk when
  it is rescheduled. Counters of deleted talks are simply missing (23 gaps
  between 1 and 256).
* `DTSTAMP` is the time of the request, so it changes on every fetch. It
  carries no information about the talk.
* `SUMMARY` is the SPEAKER, not the title. The title is the first line of
  `DESCRIPTION`, inside an HTML link to the talk page:
  `Title: <a href="https://researchseminars.org/talk/ToposInstituteColloquium/232/">What are (infty\,infty)-categories?</a>`.
  Talks without a title yet (21 upcoming ones) have no `Title:` line at
  all; the site shows them as "TBA".
* Second line: `by <speaker> as part of Topos Institute Colloquium`.
  Speakers of this series have no affiliation. Two talks have several
  speakers joined with `\, ` and ` and ` in one `SUMMARY`.
* Upcoming online talks carry `Interactive livestream: <Zoom URL with pwd=>`
  and `Password hint: ...` lines (both redacted, see below), one talk also
  `View-only livestream: <YouTube URL>`. The same links appear as `URL`
  properties: 23 events have one `URL`, one event has TWO (`URL` is allowed
  once by RFC 5545, so a parser may return a list). Past talks have no
  livestream lines.
* Abstract: either `\n\nAbstract\n<text>` (LaTeX such as `$(\\infty\,n)$`,
  non-ASCII characters; 102 of the 209 abstracts have paragraphs separated
  by empty lines) or the literal `Abstract: TBA` (24 events). `Abstract:
  TBA` has no empty line of its own: in the 21 upcoming talks it directly
  follows the `Password hint:` line.
* `LOCATION` is the talk page URL, not a room.
* No `STATUS`, `CATEGORIES`, `ORGANIZER` or `ATTENDEE` properties.

## Redacted

The feed carries the series' recurring Zoom meeting with its passcode. The
repository is public, so three values were replaced after recording, each by
a dummy of the same length in the same alphabet:

| Value | Where | Replaced by |
|---|---|---|
| meeting id (11 digits) | path `/j/<id>` of the Zoom URL, 48 times | `00000000000` |
| passcode (32 letters and digits) | value of `pwd=`, 48 times | `REDACTEDREDACTEDREDACTEDREDACTED` |
| password hint (5 digits) | `Password hint:` line, 24 times | `00000` |

The 48 Zoom URLs are the 24 `Interactive livestream:` lines in `DESCRIPTION`
and the 24 Zoom `URL` properties. Because every replacement keeps the byte
length, each folded line has its original length and the folds sit where
the site put them; some replaced values run across a fold. Nothing else
changed: all 233 VEVENTs still parse with the same properties, values
differ only in the replaced characters of `DESCRIPTION` and `URL`, `pwd=`
is still in every Zoom URL (the test input for dropping stream URLs
that carry a credential), and the YouTube link of the view-only livestream
is kept as served. A new recording of this URL brings the real values back:
redact it the same way before committing it.
