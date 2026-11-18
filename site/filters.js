// The talk list as data: events.json records checked and prepared for display, the
// filters, and how the filters are written to and read from the address bar.
//
// Nothing in this module touches the page, so every rule here can be read on its own;
// the only outside state it reads is the message catalogs of i18n.js, for the placeholder
// title in every language. Filter states are frozen objects; a change makes a new state.

import { availableLangs, messageIn } from "./i18n.js";
import { addDays, parseTimestamp } from "./time.js";

/** Time ranges: how many calendar days from today an event may start in (0: no limit). */
export const RANGES = Object.freeze({ today: 1, week: 7, two_weeks: 14, all: 0 });
export const DEFAULT_RANGE = "all";

/**
 * Kinds of events: every event, the talks, or the meetings (conferences and workshops
 * over several days or of at least three hours: the flag ``meeting`` of events.json).
 */
export const KINDS = Object.freeze(["all", "talks", "meetings"]);
export const DEFAULT_KIND = "all";

/**
 * The group of the list that holds the events that began before today and have not ended:
 * listed once, above today, instead of at the top of every day they last.
 */
export const ONGOING = "ongoing";

/** Names of the query string parameters that hold the filters. */
export const PARAMS = Object.freeze({
  query: "q",
  field: "field",
  institution: "inst",
  host: "host",
  kind: "kind",
  range: "range",
  picks: "picks",
});

/** The title the pipeline stores for a talk whose title is not announced yet. */
export const TBA_TITLE = "TBA";
const TBA_MESSAGE = "site.event.tba";

/**
 * Picks reasons the page can say in every language: what matched and where (see
 * seminars.models.public.PickReason). Any other reason is shown as its text.
 */
export const REASON_KINDS = Object.freeze(["keyword", "series", "speaker", "penalty"]);
export const REASON_PLACES = Object.freeze(["title", "series", "abstract"]);

const MAX_QUERY_LENGTH = 200;
const DEFAULT_MINUTES = 60;
const MINUTE_MS = 60 * 1000;
const COMBINING_MARKS = /[\u0300-\u036f]/g;
// Typographic apostrophes and primes, double quotes and dashes (the non-breaking
// hyphen and the minus sign among them), folded to the ASCII forms a reader types.
const APOSTROPHES = /[\u2018\u2019\u201a\u201b\u2032\u2035\u02bc]/g;
const QUOTES = /[\u201c\u201d\u201e\u201f\u2033\u2036\u00ab\u00bb]/g;
const DASHES = /[\u2010-\u2015\u2212\ufe58\ufe63\uff0d]/g;
// Lower-case letters without a decomposition, and the letters typed for them.
const LETTERS = Object.freeze({
  "\u00df": "ss", // sharp s
  "\u00e6": "ae",
  "\u0153": "oe",
  "\u00f8": "o", // o with stroke
  "\u0142": "l", // l with stroke
  "\u0111": "d", // d with stroke
  "\u00f0": "d", // eth
  "\u0131": "i", // dotless i
  "\u00fe": "th", // thorn
  "\u0127": "h", // h with stroke
});
const FOLDED_LETTERS = new RegExp(`[${Object.keys(LETTERS).join("")}]`, "g");

export const NO_FILTERS = Object.freeze({
  query: "",
  fields: Object.freeze([]),
  institutions: Object.freeze([]),
  host: "",
  kind: DEFAULT_KIND,
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

/**
 * Lower case without accents, typographic punctuation or letters a keyboard lacks, so
 * that "zoe muller" finds "Zo\u00e9 M\u00fcller", "Cell's" finds "Cell\u2019s",
 * "McKeithen-Mead" finds it written with an en dash, and "Lukasz" finds "\u0141ukasz".
 */
export function fold(value) {
  return value
    .normalize("NFKD")
    .replace(COMBINING_MARKS, "")
    .toLowerCase()
    .replace(FOLDED_LETTERS, (letter) => LETTERS[letter])
    .replace(APOSTROPHES, "'")
    .replace(QUOTES, '"')
    .replace(DASHES, "-");
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

function otherReason(text) {
  return Object.freeze({ kind: "other", term: text, place: null, points: null });
}

/**
 * One picks reason of events.json, or null when it has no text. A reason of an unknown
 * kind, or whose parts do not fit together, becomes an "other" reason: its text as it is.
 */
export function prepareReason(raw) {
  if (typeof raw === "string") {
    const text = optional(raw);
    return text === null ? null : otherReason(text);
  }
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return null;
  }
  const term = optional(raw.term);
  if (term === null) {
    return null;
  }
  const { kind, place, points } = raw;
  const keyword = kind === "keyword";
  const fits =
    REASON_KINDS.includes(kind) &&
    typeof points === "number" &&
    Number.isFinite(points) &&
    (keyword ? REASON_PLACES.includes(place) : place === null || place === undefined);
  if (!fits) {
    return otherReason(term);
  }
  return Object.freeze({ kind, term, place: keyword ? place : null, points });
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
 * The placeholder title of a talk without a title in every language the page has loaded.
 * A card shows the one of the page's language, so the search text of such a talk holds
 * them all: a reader finds it by the word on the card, whatever the language.
 */
export function placeholderTitles() {
  return availableLangs().map((lang) => messageIn(lang, TBA_MESSAGE));
}

/**
 * One record of events.json prepared for display, or null when it lacks what every
 * talk needs (an id, a title and a start time). ``zone`` is the payload's time zone
 * (see time.js); ``fieldIds`` the known field ids; ``placeholders`` the placeholder
 * titles that the search text of a talk without a title holds besides the stored one.
 */
export function prepareEvent(raw, zone, fieldIds, placeholders = placeholderTitles()) {
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
  const reasons = (Array.isArray(raw.picks_reasons) ? raw.picks_reasons : [])
    .map(prepareReason)
    .filter((reason) => reason !== null);
  const event = {
    uid,
    title,
    tba: title === TBA_TITLE,
    speakers,
    series: optional(raw.series),
    host: optional(raw.host_org),
    institution: str(raw.institution),
    type: str(raw.event_type),
    meeting: raw.meeting === true,
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
    reasons,
    status: str(raw.status) || "confirmed",
  };
  const haystack = [
    event.title,
    ...(event.tba ? placeholders : []),
    ...speakers.flatMap((speaker) => [speaker.name, speaker.affiliation ?? ""]),
    event.series ?? "",
    event.host ?? "",
    event.location ?? "",
    event.abstract ?? "",
  ].join("\n");
  return Object.freeze({ ...event, haystack: fold(haystack) });
}

/** Prepared events sorted by start time and id; records that cannot be shown are left out. */
export function prepareEvents(records, zone, fieldIds, placeholders = placeholderTitles()) {
  return (Array.isArray(records) ? records : [])
    .map((raw) => prepareEvent(raw, zone, fieldIds, placeholders))
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
  if (filters.kind === "talks" && event.meeting) {
    return false;
  }
  if (filters.kind === "meetings" && !event.meeting) {
    return false;
  }
  const days = RANGES[filters.range];
  if (days && event.startDay >= addDays(today, days)) {
    return false;
  }
  return searchTerms(filters.query).every((term) => event.haystack.includes(term));
}

/**
 * Events grouped by the day they start. The events that began before today (the list
 * holds only those that have not ended, such as a conference in progress) form the group
 * ONGOING, which comes first.
 */
export function groupByDay(events, today) {
  const groups = new Map([[ONGOING, []]]);
  for (const event of events) {
    const key = event.startDay < today ? ONGOING : event.startDay;
    groups.set(key, [...(groups.get(key) ?? []), event]);
  }
  if (groups.get(ONGOING).length === 0) {
    groups.delete(ONGOING);
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
    filters.kind === DEFAULT_KIND &&
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
  const { host, kind, range } = filters;
  return Object.freeze({
    query: filters.query.slice(0, MAX_QUERY_LENGTH),
    fields: chosen(strings(filters.fields), choices.fields),
    institutions: chosen(strings(filters.institutions), choices.institutions),
    host: choices.hosts === null || choices.hosts.includes(host) ? host : "",
    kind: KINDS.includes(kind) ? kind : DEFAULT_KIND,
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
      kind: params.get(PARAMS.kind) ?? "",
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
  if (filters.kind !== DEFAULT_KIND) {
    params.set(PARAMS.kind, filters.kind);
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
