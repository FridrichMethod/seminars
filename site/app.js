// The talk list (index.html): filters, the list grouped by day and the subscription panel.

import { h, link, replaceChildren } from "./dom.js";
import {
  NO_FILTERS,
  RANGES,
  filtersFromQuery,
  groupByDay,
  isUnfiltered,
  isUpcoming,
  matches,
  prepareEvents,
  queryFromFilters,
  toggled,
} from "./filters.js";
import { children, currentLang, parts, plural, t } from "./i18n.js";
import { byId, fact, fetchJson, notice, replaceQuery, reportError, startPage } from "./page.js";
import { renderSubscribe } from "./subscribe.js";
import { parseTimestamp, zone } from "./time.js";

const ui = {
  updated: byId("updated"),
  timezone: byId("timezone"),
  search: byId("search"),
  fields: byId("field-chips"),
  institutions: byId("institution-chips"),
  host: byId("host"),
  range: byId("range"),
  picks: byId("picks"),
  clear: byId("clear"),
  count: byId("count"),
  events: byId("events"),
  steps: byId("apple-steps"),
  feeds: byId("feeds"),
};

// The page state; every change replaces the object.
let view = Object.freeze({ model: null, failed: false, filters: NO_FILTERS });

/** The payload of events.json prepared for the page. */
function prepareModel(payload, now) {
  const tz = zone(typeof payload.timezone === "string" ? payload.timezone : "UTC");
  const knownFields = children("fields.");
  const fieldIds = (Array.isArray(payload.fields) ? payload.fields : []).filter((id) =>
    knownFields.includes(id),
  );
  const fields = fieldIds.length ? fieldIds : knownFields;
  const events = prepareEvents(payload.events, tz, fields);
  const today = tz.dayKey(now);
  const upcoming = events.filter((event) => isUpcoming(event, now, today));
  const present = (values) => new Set(upcoming.flatMap(values));
  const institutions = present((event) => [event.institution]);
  const hosts = [...present((event) => (event.host === null ? [] : [event.host]))];
  return Object.freeze({
    zone: tz,
    generatedAt: parseTimestamp(payload.generated_at),
    feeds: payload.feeds,
    events,
    choices: Object.freeze({
      fields,
      institutions: children("site.institutions.").filter((id) => institutions.has(id)),
      hosts: hosts.sort((a, b) => a.localeCompare(b)),
    }),
  });
}

function setFilters(filters) {
  view = Object.freeze({ ...view, filters: Object.freeze(filters) });
  replaceQuery(queryFromFilters(view.filters, window.location.search));
  syncControls();
  renderList();
}

function chip(label, value, selected, onToggle) {
  return h(
    "button",
    {
      type: "button",
      class: "chip",
      "data-value": value,
      "aria-pressed": String(selected),
      on: { click: () => onToggle(value) },
    },
    label,
  );
}

function option(value, label) {
  return h("option", { value }, label);
}

function buildControls() {
  const { model, filters } = view;
  const choices = model?.choices ?? { fields: [], institutions: [], hosts: [] };
  replaceChildren(
    ui.fields,
    choices.fields.map((id) =>
      chip(t("fields." + id), id, filters.fields.includes(id), (value) =>
        setFilters({ ...view.filters, fields: toggled(view.filters.fields, value, choices.fields) }),
      ),
    ),
  );
  replaceChildren(
    ui.institutions,
    choices.institutions.map((id) =>
      chip(t("site.institutions." + id), id, filters.institutions.includes(id), (value) =>
        setFilters({
          ...view.filters,
          institutions: toggled(view.filters.institutions, value, choices.institutions),
        }),
      ),
    ),
  );
  replaceChildren(
    ui.host,
    option("", t("site.filters.host_all")),
    choices.hosts.map((host) => option(host, host)),
  );
  replaceChildren(
    ui.range,
    Object.keys(RANGES).map((id) => option(id, t("site.filters.range_options." + id))),
  );
}

function syncControls() {
  const { filters } = view;
  if (ui.search.value !== filters.query) {
    ui.search.value = filters.query;
  }
  for (const [container, selected] of [
    [ui.fields, filters.fields],
    [ui.institutions, filters.institutions],
  ]) {
    for (const button of container.querySelectorAll("button.chip")) {
      button.setAttribute("aria-pressed", String(selected.includes(button.dataset.value)));
    }
  }
  ui.host.value = filters.host;
  ui.range.value = filters.range;
  ui.picks.checked = filters.picksOnly;
  ui.clear.disabled = isUnfiltered(filters);
}

function connectControls() {
  ui.search.addEventListener("input", () => setFilters({ ...view.filters, query: ui.search.value }));
  ui.host.addEventListener("change", () => setFilters({ ...view.filters, host: ui.host.value }));
  ui.range.addEventListener("change", () => setFilters({ ...view.filters, range: ui.range.value }));
  ui.picks.addEventListener("change", () =>
    setFilters({ ...view.filters, picksOnly: ui.picks.checked }),
  );
  ui.clear.addEventListener("click", () => {
    setFilters(NO_FILTERS);
    ui.search.focus();
  });
}

function when(event, tz, lang) {
  if (event.allDay) {
    return t("site.event.all_day");
  }
  const start = tz.formatTime(event.start, lang);
  if (event.end === null) {
    return start;
  }
  const sameDay = tz.dayKey(event.end) === event.startDay;
  const end = sameDay ? tz.formatTime(event.end, lang) : tz.formatMoment(event.end, lang);
  return t("site.event.time_range", { start, end });
}

function badges(event) {
  const items = [
    event.pick && ["pick", t("site.event.pick")],
    event.status === "cancelled" && ["cancelled", t("site.event.cancelled")],
    event.status === "tentative" && ["tentative", t("site.event.tentative")],
    event.online && ["online", t("site.event.online")],
    children("site.event_types.").includes(event.type) && [
      "type",
      t("site.event_types." + event.type),
    ],
    children("site.institutions.").includes(event.institution) && [
      "institution",
      t("site.institutions." + event.institution),
    ],
    ...event.fields.map((id) => ["field", t("fields." + id)]),
  ].filter(Boolean);
  return h(
    "ul",
    { class: "badges", "aria-label": t("site.event.labels") },
    items.map(([kind, label]) => h("li", { class: `badge badge-${kind}` }, label)),
  );
}

function speakers(event) {
  if (event.speakers.length === 0) {
    return null;
  }
  return h(
    "ul",
    { class: "speakers", "aria-label": t("site.event.speakers") },
    event.speakers.map((speaker) => {
      const name = link(speaker.url, { class: "speaker-name" }, speaker.name);
      return h(
        "li",
        {},
        speaker.affiliation === null
          ? name
          : parts("site.event.speaker_affiliation", { name, affiliation: speaker.affiliation }),
      );
    }),
  );
}

function reasons(event) {
  if (!event.pick || event.reasons.length === 0) {
    return null;
  }
  return h(
    "div",
    { class: "reasons" },
    h("p", { class: "reasons-label" }, t("site.event.picks_reasons")),
    h("ul", {}, event.reasons.map((reason) => h("li", {}, reason))),
  );
}

function renderEvent(event, tz, lang, today) {
  const classes = ["event", event.status === "cancelled" && "is-cancelled", event.pick && "is-pick"];
  const lastDay =
    event.allDay && event.lastDay !== event.startDay
      ? h(
          "p",
          { class: "event-until" },
          t("site.event.until", { date: tz.formatDay(event.lastDay, lang, today) }),
        )
      : null;
  const stream = link(event.streamUrl, {}, t("site.event.livestream"));
  return h(
    "article",
    { class: classes.filter(Boolean).join(" "), "data-uid": event.uid },
    h(
      "p",
      { class: "event-when" },
      h("time", { datetime: event.start.toISOString() }, when(event, tz, lang)),
    ),
    h(
      "div",
      { class: "event-main" },
      h("h3", { class: "event-title" }, link(event.url, {}, event.title)),
      lastDay,
      badges(event),
      speakers(event),
      h(
        "dl",
        { class: "facts" },
        fact("site.event.series", event.series),
        fact("site.event.host", event.host),
        fact("site.event.location", event.location),
      ),
      stream.tagName === "A" ? h("p", { class: "stream" }, stream) : null,
      event.abstract === null
        ? null
        : h(
            "details",
            { class: "abstract" },
            h("summary", {}, t("site.event.abstract")),
            h("p", {}, event.abstract),
          ),
      reasons(event),
    ),
  );
}

function renderList() {
  const { model, failed, filters } = view;
  if (failed) {
    replaceChildren(ui.count);
    replaceChildren(ui.events, notice("site.results.load_error", { alert: true }));
    return;
  }
  if (model === null) {
    replaceChildren(ui.events, notice("site.meta.loading"));
    return;
  }
  const now = new Date();
  const lang = currentLang();
  const tz = model.zone;
  const today = tz.dayKey(now);
  const shown = model.events.filter(
    (event) => isUpcoming(event, now, today) && matches(event, filters, today),
  );
  replaceChildren(ui.count, plural("site.results.count", shown.length));
  if (shown.length === 0) {
    replaceChildren(ui.events, notice("site.results.empty"));
    return;
  }
  const days = [...groupByDay(shown, today)].map(([day, events]) =>
    h(
      "section",
      { class: "day", "data-day": day },
      h("h2", { class: "day-heading" }, tz.formatDay(day, lang, today)),
      events.map((event) => renderEvent(event, tz, lang, today)),
    ),
  );
  replaceChildren(ui.events, days);
}

function renderMeta() {
  const { model } = view;
  if (model === null) {
    replaceChildren(ui.updated);
    replaceChildren(ui.timezone);
    return;
  }
  const lang = currentLang();
  replaceChildren(
    ui.updated,
    model.generatedAt === null
      ? null
      : parts("site.meta.updated", {
          time: h(
            "time",
            { datetime: model.generatedAt.toISOString() },
            model.zone.formatMoment(model.generatedAt, lang),
          ),
        }),
  );
  replaceChildren(ui.timezone, t("site.meta.timezone", { timezone: model.zone.name }));
}

function render() {
  renderMeta();
  buildControls();
  syncControls();
  renderList();
  renderSubscribe(view.model?.feeds ?? [], { steps: ui.steps, list: ui.feeds });
}

async function main() {
  // Handled at once, so a failed request is never an unhandled rejection.
  const loading = fetchJson("events.json").then(
    (payload) => ({ payload, error: null }),
    (error) => ({ payload: null, error }),
  );
  const refresh = await startPage(render);
  connectControls();
  try {
    const { payload, error } = await loading;
    if (error !== null) {
      throw error;
    }
    const model = prepareModel(payload, new Date());
    view = Object.freeze({
      model,
      failed: false,
      filters: filtersFromQuery(window.location.search, model.choices),
    });
  } catch (error) {
    reportError(error);
    view = Object.freeze({ ...view, failed: true });
  }
  refresh();
}

main().catch(reportError);
