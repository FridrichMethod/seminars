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

/** Whether the last run of a source is known to have missed events. */
function partial(source) {
  return source.read_in_full === false && stateOf(source) !== "disabled";
}

function healthy(source) {
  return stateOf(source) === "ok" && !partial(source);
}

function size(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

/** The name of a source group: the known groups are translated, others shown as given. */
function groupName(group) {
  if (typeof group !== "string" || group === "") {
    return null;
  }
  return children("site.status.groups.").includes(group) ? t("site.status.groups." + group) : group;
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
  const healthySources = active.filter(healthy);
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
        ok: formatNumber(healthySources.length),
        total: formatNumber(active.length),
      }),
    ),
    h("li", {}, llmLine(provider, count(status.llm_stored_answers))),
  ];
}

/** What the LLM of the last run used: a provider, answers stored by earlier runs, or no LLM. */
function llmLine(provider, storedAnswers) {
  if (provider !== "") {
    return t("site.status.llm", { provider });
  }
  return storedAnswers > 0
    ? plural("site.status.llm_stored", storedAnswers)
    : t("site.status.llm_none");
}

/**
 * The error of a failed source as the run wrote it: an HTTP status, an error kind and
 * an address, in English in every language of the page, and marked as such.
 */
function diagnostic(error) {
  return h("code", { lang: "en", translate: "no" }, error);
}

/** What the last run of a source missed: one description per reason, under one term. */
function missedFacts(source) {
  if (!partial(source)) {
    return null;
  }
  const skipped = count(source.skipped_pages);
  const usual = size(source.usual_size);
  const reasons = [
    skipped > 0 ? plural("site.status.skipped", skipped) : null,
    usual === null
      ? null
      : t("site.status.shrank", {
          listed: formatNumber(count(source.listed)),
          usual: formatNumber(usual),
        }),
  ].filter((reason) => reason !== null);
  if (reasons.length === 0) {
    return null;
  }
  return [
    h("dt", {}, t("site.status.columns.missed")),
    ...reasons.map((reason) => h("dd", { class: "missed" }, reason)),
  ];
}

/**
 * Why a disabled source is off: the message of its id under site.status.disabled_reasons
 * in the locale files (a test keeps one for every disabled source of the configuration).
 */
function disabledReason(source) {
  if (stateOf(source) !== "disabled" || typeof source.id !== "string") {
    return null;
  }
  const key = "site.status.disabled_reasons." + source.id;
  return children("site.status.disabled_reasons.").includes(source.id) ? t(key) : null;
}

/** The note of a source whose last run took its smaller listing as the usual size. */
function resizedNote(source) {
  const before = size(source.resized_from);
  if (before === null || stateOf(source) === "disabled") {
    return null;
  }
  return t("site.status.resized", {
    listed: formatNumber(count(source.listed)),
    usual: formatNumber(before),
  });
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
      partial(source) ? h("p", { class: "badge badge-partial" }, t("site.status.partial")) : null,
    ),
    h(
      "dl",
      { class: "facts" },
      fact("site.status.columns.group", groupName(source.group)),
      fact("site.status.columns.disabled_reason", disabledReason(source)),
      fact("site.status.columns.events", formatNumber(count(source.events))),
      fact("site.status.columns.last_run", moment(source.last_run_at, lang) ?? never),
      fact("site.status.columns.last_success", moment(source.last_success_at, lang) ?? never),
      fact("site.status.columns.failures", formatNumber(count(source.consecutive_failures))),
      error === null ? null : fact("site.status.columns.error", diagnostic(error)),
      missedFacts(source),
      fact("site.status.columns.resized", resizedNote(source)),
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
