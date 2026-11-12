// The talk list as data: events.json records checked and prepared for display, the
// filters, and how the filters are written to and read from the address bar.
//
// Nothing in this module touches the page, so every rule here can be read on its own.
// Filter states are frozen objects; a change makes a new state.

import { addDays, parseTimestamp } from "./time.js";

/** Time ranges: how many calendar days from today an event may start in (0: no limit). */
export const RANGES = Object.freeze({ today: 1, week: 7, two_weeks: 14, all: 0 });
export const DEFAULT_RANGE = "all";

/** Names of the query string parameters that hold the filters. */
export const PARAMS = Object.freeze({
  query: "q",
  field: "field",
  institution: "inst",
  host: "host",
  range: "range",
  picks: "picks",
});

const MAX_QUERY_LENGTH = 200;
const DEFAULT_MINUTES = 60;
const MINUTE_MS = 60 * 1000;
const COMBINING_MARKS = /[\u0300-\u036f]/g;

export const NO_FILTERS = Object.freeze({
  query: "",
  fields: Object.freeze([]),
  institutions: Object.freeze([]),
  host: "",
  range: DEFAULT_RANGE,
  picksOnly: false,
});

function str(value) {
  return typeof value === "string" ? value : "";
}

function optional(value) {
  const textValue = str(value).trim();
  return textValue === "" ? null : textValue;
}

function strings(value) {
  return Array.isArray(value) ? value.filter((item) => typeof item === "string" && item) : [];
}

/** Lower case without accents, so that "zoe muller" finds "Zo\u00e9 M\u00fcller". */
export function fold(value) {
  return value.normalize("NFKD").replace(COMBINING_MARKS, "").toLowerCase();
}

function prepareSpeaker(raw) {
  if (raw === null || typeof raw !== "object") {
    return null;
  }
  const name = optional(raw.name);
  return name === null
    ? null
    : Object.freeze({ name, affiliation: optional(raw.affiliation), url: str(raw.url) });
}

/** The end of a timed talk: its own end, or the default duration after its start. */
function timedEnd(start, end) {
  return end !== null && end > start
    ? end
    : new Date(start.getTime() + DEFAULT_MINUTES * MINUTE_MS);
}

/**
 * The last calendar day of a talk. An all-day event ends at local midnight after its
 * last day (exclusive) or has no end (one day); it is counted in calendar days, never as
 * 24 hours, because a day of the payload's zone is 23 or 25 hours long when the clocks
 * change.
 */
function lastDayOf(start, end, allDay, zone) {
  if (allDay && end !== null && end > start) {
    return zone.dayKey(new Date(end.getTime() - 1));
  }
  return zone.dayKey(start);
}

/**
 * One record of events.json prepared for display, or null when it lacks what every
 * talk needs (an id, a title and a start time). ``zone`` is the payload's time zone
 * (see time.js); ``fieldIds`` the known field ids.
 */
export function prepareEvent(raw, zone, fieldIds) {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return null;
  }
  const uid = optional(raw.uid);
  const title = optional(raw.title);
  const start = parseTimestamp(raw.start);
  if (uid === null || title === null || start === null) {
    return null;
  }
  const allDay = raw.all_day === true;
  const end = parseTimestamp(raw.end);
  const speakers = (Array.isArray(raw.speakers) ? raw.speakers : [])
    .map(prepareSpeaker)
    .filter((speaker) => speaker !== null);
  const event = {
    uid,
    title,
    speakers,
    series: optional(raw.series),
    host: optional(raw.host_org),
    institution: str(raw.institution),
    type: str(raw.event_type),
    start,
    end: end !== null && end > start ? end : null,
    ends: allDay ? null : timedEnd(start, end),
    allDay,
    startDay: zone.dayKey(start),
    lastDay: lastDayOf(start, end, allDay, zone),
    location: optional(raw.location),
    online: raw.is_online === true,
    streamUrl: str(raw.stream_url),
    abstract: optional(raw.abstract),
    url: str(raw.url),
    fields: fieldIds.filter((field) => strings(raw.fields).includes(field)),
    pick: raw.is_pick === true,
    reasons: strings(raw.picks_reasons),
    status: str(raw.status) || "confirmed",
  };
  const haystack = [
    event.title,
    ...speakers.flatMap((speaker) => [speaker.name, speaker.affiliation ?? ""]),
    event.series ?? "",
    event.host ?? "",
    event.abstract ?? "",
  ].join("\n");
  return Object.freeze({ ...event, haystack: fold(haystack) });
}

/** Prepared events sorted by start time and id; records that cannot be shown are left out. */
export function prepareEvents(records, zone, fieldIds) {
  return (Array.isArray(records) ? records : [])
    .map((raw) => prepareEvent(raw, zone, fieldIds))
    .filter((event) => event !== null)
    .sort((a, b) => a.start - b.start || (a.uid < b.uid ? -1 : a.uid > b.uid ? 1 : 0));
}

/**
 * True when ``event`` has not ended at ``now``; ``today`` is the day key of ``now``. An
 * all-day event lasts until its last day is over, a timed talk until its end time.
 */
export function isUpcoming(event, now, today) {
  return event.allDay ? event.lastDay >= today : event.ends > now;
}

/** The search words of a query, folded like the text they are looked for in. */
export function searchTerms(query) {
  return fold(query).split(/\s+/).filter(Boolean);
}

/** True when ``event`` passes every filter; ``today`` is the current day key. */
export function matches(event, filters, today) {
  if (filters.picksOnly && !event.pick) {
    return false;
  }
  if (filters.fields.length && !event.fields.some((field) => filters.fields.includes(field))) {
    return false;
  }
  if (filters.institutions.length && !filters.institutions.includes(event.institution)) {
    return false;
  }
  if (filters.host && event.host !== filters.host) {
    return false;
  }
  const days = RANGES[filters.range];
  if (days && event.startDay >= addDays(today, days)) {
    return false;
  }
  return searchTerms(filters.query).every((term) => event.haystack.includes(term));
}

/** Events grouped by day (a talk that began before today is listed under today). */
export function groupByDay(events, today) {
  const groups = new Map();
  for (const event of events) {
    const key = event.startDay < today ? today : event.startDay;
    groups.set(key, [...(groups.get(key) ?? []), event]);
  }
  return groups;
}

/** True when no filter is set. */
export function isUnfiltered(filters) {
  return (
    filters.query.trim() === "" &&
    filters.fields.length === 0 &&
    filters.institutions.length === 0 &&
    filters.host === "" &&
    filters.range === DEFAULT_RANGE &&
    !filters.picksOnly
  );
}

/**
 * Choices that keep every value: the filters of an address before the talks are known.
 * ``null`` in place of a list of choices means that any value is kept.
 */
export const ANY_CHOICES = Object.freeze({ fields: null, institutions: null, hosts: null });

function chosen(values, allowed) {
  const kept = allowed === null ? values : allowed.filter((value) => values.includes(value));
  return Object.freeze([...new Set(kept)]);
}

/**
 * ``filters`` with every value that is not among ``choices`` left out. ``choices`` lists
 * the values that exist (``fields``, ``institutions``, ``hosts``; see ANY_CHOICES).
 */
export function withinChoices(filters, choices) {
  const { host, range } = filters;
  return Object.freeze({
    query: filters.query.slice(0, MAX_QUERY_LENGTH),
    fields: chosen(strings(filters.fields), choices.fields),
    institutions: chosen(strings(filters.institutions), choices.institutions),
    host: choices.hosts === null || choices.hosts.includes(host) ? host : "",
    range: Object.hasOwn(RANGES, range) ? range : DEFAULT_RANGE,
    picksOnly: filters.picksOnly,
  });
}

/** The filters written in a query string; values that are not among ``choices`` are ignored. */
export function filtersFromQuery(search, choices) {
  const params = new URLSearchParams(search);
  return withinChoices(
    {
      query: params.get(PARAMS.query) ?? "",
      fields: params.getAll(PARAMS.field),
      institutions: params.getAll(PARAMS.institution),
      host: params.get(PARAMS.host) ?? "",
      range: params.get(PARAMS.range) ?? "",
      picksOnly: params.get(PARAMS.picks) === "1",
    },
    choices,
  );
}

/** ``search`` with the filter parameters replaced by ``filters``; other parameters stay. */
export function queryFromFilters(filters, search) {
  const params = new URLSearchParams(search);
  for (const name of Object.values(PARAMS)) {
    params.delete(name);
  }
  if (filters.query.trim() !== "") {
    params.set(PARAMS.query, filters.query);
  }
  for (const field of filters.fields) {
    params.append(PARAMS.field, field);
  }
  for (const institution of filters.institutions) {
    params.append(PARAMS.institution, institution);
  }
  if (filters.host !== "") {
    params.set(PARAMS.host, filters.host);
  }
  if (filters.range !== DEFAULT_RANGE) {
    params.set(PARAMS.range, filters.range);
  }
  if (filters.picksOnly) {
    params.set(PARAMS.picks, "1");
  }
  return params;
}

/** ``values`` with ``value`` added or removed, in the order of ``allowed``. */
export function toggled(values, value, allowed) {
  const next = values.includes(value)
    ? values.filter((item) => item !== value)
    : [...values, value];
  return chosen(next, allowed);
}
