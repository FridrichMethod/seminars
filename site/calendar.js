// One calendar file of chosen talks, cut out of a calendar feed of this site.
//
// The pipeline publishes feeds/all.ics with every talk of events.json and checks it
// (seminars validate). A file of chosen talks is made from that feed: the VEVENT
// components of the talks are copied line for line, folding untouched, into a calendar
// of their own with every VTIMEZONE of the feed and the header properties an import
// needs. Nothing here writes an iCalendar value, so the file says exactly what the feed
// says. The properties that only a subscription reads (the name and description of the
// feed, its refresh interval) are left out: the file is imported once.
//
// A talk of the site (events.json uid, which never holds "@") is the VEVENT whose UID is
// <uid>@<uid domain>. The domain belongs to the pipeline and is never spelled out here:
// the part of the UID before its last "@" is compared, read from the unfolded line.

import { h, siteUrl } from "./dom.js";

// The pattern of FEED_PATH in subscribe.js: a feed of this site, feeds/<id>.ics.
const FEED_PATH = /^feeds\/[a-z0-9]+(?:-[a-z0-9]+)*\.ics$/;
// Header properties of the feed that the file keeps, in the order of the feed; the first
// two are required by RFC 5545, so a feed without them is refused.
const KEPT_PROPERTIES = Object.freeze(["VERSION", "PRODID", "CALSCALE", "METHOD", "X-WR-TIMEZONE"]);
const REQUIRED_PROPERTIES = Object.freeze(["VERSION", "PRODID"]);
const CALENDAR = "VCALENDAR";
const EVENT = "VEVENT";
const TIMEZONE = "VTIMEZONE";
const CALENDAR_TYPE = "text/calendar;charset=utf-8";
const CRLF = "\r\n";
const LINE_BREAK = /\r\n|\n|\r/;
const BYTE_ORDER_MARK = "\uFEFF";
const PROPERTY_NAME = /^[A-Za-z0-9-]+$/;
// The browser reads a saved file from its object URL after the click, at its own pace;
// the URL is given up this long afterwards.
const REVOKE_AFTER_MS = 30 * 1000;
// A feed that has not arrived in full this long after its request is given up, so a
// stalled request fails like any other and can be tried again.
const FETCH_TIMEOUT_MS = 30 * 1000;

/** Whether a physical line continues the line before it (RFC 5545 folding). */
function continues(line) {
  return line.startsWith(" ") || line.startsWith("\t");
}

/**
 * Name (upper case) and value of one unfolded content line, or null when it is none. A
 * colon inside a quoted parameter value does not end the name and its parameters.
 */
function nameAndValue(text) {
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    if (text[index] === '"') {
      quoted = !quoted;
    } else if (text[index] === ":" && !quoted) {
      const name = text.slice(0, index).split(";", 1)[0];
      if (!PROPERTY_NAME.test(name)) {
        return null;
      }
      return { name: name.toUpperCase(), value: text.slice(index + 1) };
    }
  }
  return null;
}

/**
 * The content lines of ``text`` in order: each with its name and unfolded value, the
 * number of its first physical line and the physical lines it is stored in. Blank lines
 * hold nothing and are skipped.
 */
function contentLines(text) {
  const physical = (text.startsWith(BYTE_ORDER_MARK) ? text.slice(1) : text).split(LINE_BREAK);
  const folded = [];
  physical.forEach((line, index) => {
    if (line === "") {
      return;
    }
    if (!continues(line)) {
      folded.push({ number: index + 1, unfolded: line, lines: [line] });
      return;
    }
    const previous = folded[folded.length - 1];
    if (previous === undefined) {
      throw new Error(`line ${index + 1}: continues no line`);
    }
    previous.unfolded += line.slice(1);
    previous.lines.push(line);
  });
  return folded.map(({ number, unfolded, lines }) => {
    const parsed = nameAndValue(unfolded);
    if (parsed === null) {
      throw new Error(`line ${number}: not a content line`);
    }
    return { number, ...parsed, lines };
  });
}

/** The site uid in a UID value: the part before its last "@" (all of it without one). */
function siteUid(value) {
  const at = value.lastIndexOf("@");
  return at === -1 ? value : value.slice(0, at);
}

/**
 * The parts of a calendar that a file of chosen talks is made of, as a frozen
 * ``{ properties, timezones, events }``:
 *
 * - ``properties``: the kept header properties of the calendar in feed order, each as
 *   the physical lines it is stored in (a folded property stays folded);
 * - ``timezones``: every VTIMEZONE as its physical lines, BEGIN to END;
 * - ``events``: every VEVENT in feed order as ``{ uid, lines }``, ``uid`` the site uid
 *   and ``lines`` its physical lines, BEGIN to END, nested components (VALARM) included.
 *
 * Lines may end in CRLF or LF. Text that is not one well-nested VCALENDAR with VERSION
 * and PRODID, or a VEVENT without exactly one UID of its own, throws an Error.
 */
export function parseCalendar(text) {
  if (typeof text !== "string") {
    throw new TypeError("a calendar is text");
  }
  const properties = [];
  const names = new Set();
  const timezones = [];
  const events = [];
  const open = []; // names of the open components, the calendar first
  let part = null; // the open component of the calendar: { name, number, lines, uids }
  let closed = false;
  for (const line of contentLines(text)) {
    const component = line.name === "BEGIN" || line.name === "END" ? line.value.toUpperCase() : "";
    if (closed) {
      throw new Error(`line ${line.number}: content after END:${CALENDAR}`);
    }
    if (open.length === 0) {
      if (line.name !== "BEGIN" || component !== CALENDAR) {
        throw new Error(`line ${line.number}: a calendar starts with BEGIN:${CALENDAR}`);
      }
      open.push(component);
      continue;
    }
    if (line.name === "BEGIN") {
      open.push(component);
      if (open.length === 2) {
        part = { name: component, number: line.number, lines: [], uids: [] };
      }
    } else if (line.name === "END") {
      const begun = open.pop();
      if (component !== begun) {
        throw new Error(`line ${line.number}: END:${component} does not close BEGIN:${begun}`);
      }
      if (open.length === 0) {
        closed = true;
        continue;
      }
    }
    if (part === null) {
      if (KEPT_PROPERTIES.includes(line.name)) {
        properties.push(Object.freeze(line.lines));
        names.add(line.name);
      }
      continue;
    }
    part.lines.push(...line.lines);
    if (line.name === "UID" && open.length === 2) {
      part.uids.push(line.value);
    }
    if (open.length === 1) {
      collect(part, { timezones, events });
      part = null;
    }
  }
  if (!closed) {
    throw new Error(
      open.length === 0
        ? `the text holds no ${CALENDAR}`
        : `BEGIN:${open[open.length - 1]} is never closed`,
    );
  }
  for (const name of REQUIRED_PROPERTIES) {
    if (!names.has(name)) {
      throw new Error(`the calendar has no ${name}`);
    }
  }
  return Object.freeze({
    properties: Object.freeze(properties),
    timezones: Object.freeze(timezones),
    events: Object.freeze(events),
  });
}

/** File a closed component of the calendar: time zones and events are kept, others not. */
function collect(part, { timezones, events }) {
  const lines = Object.freeze(part.lines);
  if (part.name === TIMEZONE) {
    timezones.push(lines);
  } else if (part.name === EVENT) {
    const uid = part.uids.length === 1 ? siteUid(part.uids[0]) : "";
    if (uid === "") {
      throw new Error(`line ${part.number}: a ${EVENT} needs exactly one UID`);
    }
    events.push(Object.freeze({ uid, lines }));
  }
}

/**
 * A calendar of the talks with the site uids ``uids`` (any iterable) out of ``parsed``
 * (a result of parseCalendar), as a frozen ``{ text, included, missing }``:
 *
 * - ``text``: BEGIN:VCALENDAR, the kept properties, every time zone, the VEVENTs of the
 *   talks in feed order and END:VCALENDAR, every line ending in CRLF; null when none of
 *   the talks is in the calendar;
 * - ``included``: the uids written, in feed order;
 * - ``missing``: the uids asked for that the calendar does not hold, in request order.
 *
 * A uid asked for twice counts once.
 */
export function calendarFor(parsed, uids) {
  const requested = [...new Set(uids)];
  const wanted = new Set(requested);
  const chosen = parsed.events.filter((event) => wanted.has(event.uid));
  const found = new Set(chosen.map((event) => event.uid));
  const lines = [
    `BEGIN:${CALENDAR}`,
    ...parsed.properties.flat(),
    ...parsed.timezones.flat(),
    ...chosen.flatMap((event) => event.lines),
    `END:${CALENDAR}`,
  ];
  return Object.freeze({
    text: chosen.length === 0 ? null : lines.join(CRLF) + CRLF,
    included: Object.freeze([...found]),
    missing: Object.freeze(requested.filter((uid) => !found.has(uid))),
  });
}

/**
 * The text of the calendar feed at ``path``, which has to be a feed of this site
 * (feeds/<id>.ics), revalidated with the server rather than taken from the HTTP cache.
 * Anything else, an answer that is not OK and a feed that has not arrived in full within
 * FETCH_TIMEOUT_MS (a timer of the page, which the tests can move) reject.
 */
export async function fetchCalendar(path) {
  if (typeof path !== "string" || !FEED_PATH.test(path)) {
    throw new Error("only a feed of this site is fetched: feeds/<id>.ics");
  }
  const controller = new AbortController();
  const timer = window.setTimeout(
    () => controller.abort(new Error(`${path}: no answer within ${FETCH_TIMEOUT_MS} ms`)),
    FETCH_TIMEOUT_MS,
  );
  try {
    const response = await fetch(siteUrl(path), { cache: "no-cache", signal: controller.signal });
    if (!response.ok) {
      throw new Error(`${path}: HTTP status ${response.status}`);
    }
    return await response.text();
  } finally {
    window.clearTimeout(timer);
  }
}

/**
 * Offer ``text`` as a file named ``filename`` of media type ``type`` for download,
 * through a temporary link to an object URL. The URL is revoked a while after the click.
 * Without a file name the link would open the file instead, so it is required.
 */
export function saveFile(text, filename, type = CALENDAR_TYPE) {
  if (typeof filename !== "string" || filename === "") {
    throw new TypeError("a saved file needs a name");
  }
  const address = URL.createObjectURL(new Blob([text], { type }));
  const anchor = h("a", { download: filename, hidden: true });
  anchor.href = address;
  document.body.append(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(address), REVOKE_AFTER_MS);
  }
}
