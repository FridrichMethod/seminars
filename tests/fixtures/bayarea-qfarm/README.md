# bayarea-qfarm

Recorded 2026-11-11 from qfarm.stanford.edu (Q-FARM, SoE variant of the Stanford Sites
template, soe_profile; Crawl-delay: 30).

- `events-*.html`: the list (20 cards, "Load More Events" links ?page=1). For "Q-FARM
  Seminars" the `h3` is "Speaker [Affiliation]" and the subheadline the talk title (it
  may be missing). The all-day workshop of Nov 19-20 has date-only `time[datetime]`
  values ("2026-11-19").
- `events-all-rss-*.xml`: 10 items with an empty `<description>`. The all-day workshop
  shows `<pubDate>` 00:00, so the RSS cannot tell all-day from midnight; the list can.
- The RSS also lists a stray node /events/alicia-kollar-university-maryland at 11:00
  next to the real /events/q-farm-seminars/alicia-kollar-university-maryland at 11:30;
  the list shows only the second one.
- Two speakers on one card are joined with " & ": "Tian Xie [Stanford University] &
  Yugo Onishi [Stanford University]". The 2 "Conference / Workshop" cards carry their
  real title in the `h3`, not a speaker.
- Page 0 runs to 2027-06-02, so the "Load More Events" pager (?page=1) is not needed
  for a 120-day window and was not recorded.
