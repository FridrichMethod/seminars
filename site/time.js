// Dates and times in the time zone of the payload, formatted for the active language.
//
// Calendar days are handled as "YYYY-MM-DD" keys of that time zone, so grouping and the
// time range filter do not depend on the zone of the viewer's computer. Formatting uses
// Intl.DateTimeFormat; the special spaces some languages put into times are replaced by
// ordinary ones, so the page shows no invisible characters.

const SPECIAL_SPACES = /[\u00a0\u2007\u2009\u202f]/g;
const DAY_MS = 24 * 60 * 60 * 1000;
const FALLBACK_ZONE = "UTC";

/** ``value`` (an ISO 8601 timestamp) as a Date, or null when it is not one. */
export function parseTimestamp(value) {
  if (typeof value !== "string" || value === "") {
    return null;
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** ``key`` ("YYYY-MM-DD") moved by ``days`` calendar days. */
export function addDays(key, days) {
  const [year, month, day] = key.split("-").map(Number);
  const moved = new Date(Date.UTC(year, month - 1, day) + days * DAY_MS);
  return moved.toISOString().slice(0, 10);
}

function plainSpaces(textValue) {
  return textValue.replace(SPECIAL_SPACES, " ");
}

function validZone(timeZone) {
  try {
    new Intl.DateTimeFormat("en", { timeZone });
    return timeZone;
  } catch {
    return FALLBACK_ZONE;
  }
}

/** Calendar and formatting helpers for one IANA time zone (``undefined``: the viewer's). */
export function zone(timeZone) {
  const name = timeZone === undefined ? undefined : validZone(String(timeZone));
  const dayParts = new Intl.DateTimeFormat("en-US", {
    timeZone: name,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  const format = (date, lang, options) =>
    plainSpaces(new Intl.DateTimeFormat(lang, { timeZone: name, ...options }).format(date));

  return Object.freeze({
    name,
    /** The calendar day of ``date`` in this zone, as "YYYY-MM-DD". */
    dayKey(date) {
      const parts = Object.fromEntries(
        dayParts.formatToParts(date).map((part) => [part.type, part.value]),
      );
      return `${parts.year}-${parts.month}-${parts.day}`;
    },
    /** A day heading such as "Thursday, November 12"; the year is shown when it differs. */
    formatDay(key, lang, todayKey) {
      const [year, month, day] = key.split("-").map(Number);
      const noon = new Date(Date.UTC(year, month - 1, day, 12));
      const options = { timeZone: "UTC", weekday: "long", month: "long", day: "numeric" };
      if (todayKey === undefined || todayKey.slice(0, 4) !== key.slice(0, 4)) {
        options.year = "numeric";
      }
      return plainSpaces(new Intl.DateTimeFormat(lang, options).format(noon));
    },
    /** A time of day such as "4:00 PM". */
    formatTime(date, lang) {
      return format(date, lang, { hour: "numeric", minute: "2-digit" });
    },
    /** A date and time with the zone name, such as "Nov 10, 2026, 4:30 AM PST". */
    formatMoment(date, lang) {
      return format(date, lang, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        timeZoneName: "short",
      });
    },
  });
}
