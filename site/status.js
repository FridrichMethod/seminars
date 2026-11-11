// The source status page (status.html): the last run and the state of every source.
//
// status.json carries no time zone, so its times are shown in the viewer's own zone,
// with the zone name.

import { h, link, replaceChildren } from "./dom.js";
import { children, currentLang, formatNumber, parts, plural, t } from "./i18n.js";
import { byId, fact, fetchJson, notice, reportError, startPage } from "./page.js";
import { parseTimestamp, zone } from "./time.js";

const ui = {
  updated: byId("updated"),
  summary: byId("summary"),
  sources: byId("sources"),
};

const local = zone(undefined);

// The page state; every change replaces the object.
let view = Object.freeze({ status: null, failed: false });

function count(value) {
  return Number.isInteger(value) && value >= 0 ? value : 0;
}

function moment(value, lang) {
  const date = parseTimestamp(value);
  return date === null ? null : h("time", { datetime: date.toISOString() }, local.formatMoment(date, lang));
}

function stateOf(source) {
  return children("site.status.states.").includes(source.state) ? source.state : "unknown";
}

function sourcesOf(status) {
  return (Array.isArray(status.sources) ? status.sources : []).filter(
    (source) => source !== null && typeof source === "object" && typeof source.name === "string",
  );
}

function summaryLines(status, lang) {
  const lastRun = moment(status.last_run_at, lang);
  const sources = sourcesOf(status);
  const active = sources.filter((source) => stateOf(source) !== "disabled");
  const healthy = active.filter((source) => stateOf(source) === "ok");
  const outcome =
    status.last_run_ok === true
      ? t("site.status.last_run_ok")
      : status.last_run_ok === false
        ? t("site.status.last_run_failed")
        : null;
  const provider = typeof status.llm_provider === "string" ? status.llm_provider : "";
  return [
    lastRun === null
      ? h("li", {}, t("site.status.no_run"))
      : h("li", { class: "last-run" }, parts("site.status.last_run", { time: lastRun })),
    outcome === null
      ? null
      : h("li", { class: status.last_run_ok ? "run-ok" : "run-failed" }, outcome),
    h("li", {}, plural("site.status.events", count(status.event_count))),
    h(
      "li",
      {},
      t("site.status.summary", {
        ok: formatNumber(healthy.length),
        total: formatNumber(active.length),
      }),
    ),
    h(
      "li",
      {},
      provider === "" ? t("site.status.llm_none") : t("site.status.llm", { provider }),
    ),
  ];
}

function sourceCard(source, lang) {
  const state = stateOf(source);
  const never = t("site.status.never");
  const error = typeof source.error === "string" && source.error !== "" ? source.error : null;
  return h(
    "article",
    { class: `source state-${state}`, "data-source": String(source.id ?? "") },
    h(
      "div",
      { class: "source-head" },
      h("h2", { class: "source-name" }, link(source.url, {}, source.name)),
      h("p", { class: `badge badge-state state-${state}` }, t("site.status.states." + state)),
    ),
    h(
      "dl",
      { class: "facts" },
      fact("site.status.columns.group", typeof source.group === "string" ? source.group : null),
      fact("site.status.columns.events", formatNumber(count(source.events))),
      fact("site.status.columns.last_run", moment(source.last_run_at, lang) ?? never),
      fact("site.status.columns.last_success", moment(source.last_success_at, lang) ?? never),
      fact("site.status.columns.failures", formatNumber(count(source.consecutive_failures))),
      error === null ? null : fact("site.status.columns.error", h("code", {}, error)),
    ),
  );
}

function render() {
  const { status, failed } = view;
  if (failed) {
    replaceChildren(ui.updated);
    replaceChildren(ui.summary);
    replaceChildren(ui.sources, notice("site.status.load_error", { alert: true }));
    return;
  }
  if (status === null) {
    replaceChildren(ui.sources, notice("site.meta.loading"));
    return;
  }
  const lang = currentLang();
  const generated = moment(status.generated_at, lang);
  replaceChildren(ui.updated, generated === null ? null : parts("site.meta.updated", { time: generated }));
  replaceChildren(ui.summary, summaryLines(status, lang));
  const sources = sourcesOf(status);
  replaceChildren(
    ui.sources,
    sources.length === 0
      ? notice("site.status.empty")
      : sources.map((source) => sourceCard(source, lang)),
  );
}

async function main() {
  // Handled at once, so a failed request is never an unhandled rejection.
  const loading = fetchJson("status.json").then(
    (status) => ({ status, error: null }),
    (error) => ({ status: null, error }),
  );
  const refresh = await startPage(render);
  const { status, error } = await loading;
  if (error === null) {
    view = Object.freeze({ status, failed: false });
  } else {
    reportError(error);
    view = Object.freeze({ status: null, failed: true });
  }
  refresh();
}

main().catch(reportError);
