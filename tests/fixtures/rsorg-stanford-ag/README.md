# rsorg-stanford-ag

Recorded 2026-11-11 from researchseminars.org with the polite client. Nothing
was trimmed.

| File | Request | What it is |
|---|---|---|
| `series.ics` | `https://researchseminars.org/seminar/agstanford/ics` | ICS feed of the Stanford algebraic geometry seminar: 148 VEVENTs from 2020-04-17 to 2025-03-14, none upcoming |
| `search-series-stanford.json` | `https://researchseminars.org/api/0/search/series?institutions=["Stanford"]` (percent-encoded) | API search for series whose institution list is exactly `["Stanford"]`: 9 series |

The series is dormant on researchseminars.org (last talk 2025-03-14), so the
source is disabled. Its feed is kept because it shows what the Topos feed
does not: speakers with affiliations and talks with a room.

## What the feed shows

* Same layout as `rsorg-topos-colloquium` (see its README): UTC `DTSTART` /
  `DTEND`, `UID` = `agstanford/<counter>`, `SUMMARY` = speaker,
  `Title: <a href=...>...</a>` and `by ... as part of ...` in
  `DESCRIPTION`, `LOCATION` = talk page, `DTSTAMP` = time of the request.
* `SUMMARY` and the `by` line carry the affiliation in parentheses:
  `Christopher Hacon (University of Utah)`. The affiliation may itself
  contain `and` or an escaped comma: `Remy van Dobben de Bruyn (Princeton
  and IAS)`, `Hannah Larson (Berkeley\, Clay Mathematical Institute)`.
* 55 talks have a room line: `Lecture held in 383-N.` or `Lecture held in
  Room 383-N.` (the sentence period is added by the site). The room is free
  text: also `380-W (unusual room!)`, `384-I (unusual date and location)`
  and `TBA`. Several of these lines are folded inside the room or after
  `held`, so read them only after unfolding.
* 72 of the 148 abstracts have paragraphs separated by empty lines, and two
  of them contain a line that starts with `by `: the by-line of the
  description must be found by its position, not by its first word.
* Durations are 60 minutes, three talks 90 minutes. No all-day values, no
  `URL` properties (livestream links are only shown for future talks).
* A rescheduled talk is not in the feed under its old time: the site keeps
  the old copy under a negative counter, which the feed leaves out, and the
  new time under the original counter (seen in the API: counter -59 of
  this series, title "(postponed due to illness) ...").

## The search response

`search-series-stanford.json` is not requested by the adapter. It documents
the series search of the API (`/api/0/search/series`) for whoever extends
the source list:

* GET parameters are column names; each value is JSON. A text array column
  such as `institutions` or `topics` is compared for EXACT equality:
  `institutions=["Stanford"]` and `institutions="Stanford"` give the same 9
  series, and a series listed under several institutions (for example
  `["Cal", "Stanford", "Maryland"]`) is not found. Operators such as
  `{"$contains": "Stanford"}` work only in a POST body
  (`{"query": {...}}`); in a GET they silently give zero results.
* `institutions.shortname` is not a column; the API answers 400
  (`unknown_column`).
* The response lists series records only (name, shortname, institutions,
  topics, timezone, time slots, homepage, comments); it contains no talks
  and no e-mail addresses.
